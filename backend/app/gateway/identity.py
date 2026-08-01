"""Resolving a connected peer to a cluster, and issuing the credential that lets it.

This is where "the certificate is the identity" stops being a slogan. Three
things happen here and nowhere else:

**Enrolment.** A single-use token, bound to a cluster when it was issued, is
spent for a certificate naming that cluster. The registering agent supplies a
public key and nothing else that matters — its CSR's subject is discarded, and
`RegistrationRequest.cluster_id` is checked against the token's binding rather
than believed.

**Renewal.** An agent already holding a certificate asks for the next one over
mTLS, authenticated by the certificate it already has. No token, no human, no
re-enrolment across a thousand clusters. It renews *as itself*: the cluster it
gets is read off its peer certificate, so renewal cannot be used to rename.

**Resolution.** Every `Connect` stream is placed by reading the peer
certificate the TLS stack already validated, and checking its serial against
the revocation list.

Everything here is blocking (a signature, a database round trip). The gateway
calls it from `asyncio.to_thread`, the same way the investigation runner treats
the analyzer and the history service.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import grpc
from loguru import logger

from app.security.ca import CertificateAuthority, CertificateAuthorityError
from app.security.enrolment import EnrolmentStore
from app.security.identity import (
    AgentIdentity,
    IdentityError,
    identity_from_pem,
    valid_cluster_id,
)


class RegistrationRefused(Exception):
    """Registration will not be granted, with a reason safe to return."""

    def __init__(self, detail: str, code: grpc.StatusCode = grpc.StatusCode.PERMISSION_DENIED):
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass(frozen=True, slots=True)
class GrantedCertificate:
    certificate_pem: bytes
    ca_bundle_pem: bytes
    expires_at: datetime
    cluster_id: str
    serial: str


def peer_certificate_pem(context) -> bytes | None:
    """The PEM of the client certificate, or None if the peer presented none.

    gRPC surfaces this only when the listener was configured to require a
    client certificate, which is precisely the distinction between the two
    listeners: the enrolment one never has it, the gateway one always does.
    """
    try:
        auth = context.auth_context()
    except Exception:  # pragma: no cover - defensive; grpc raises on odd states
        return None
    values = auth.get("x509_pem_cert") or []
    if not values:
        return None
    value = values[0]
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


class AgentIdentityService:
    """The gateway's certificate authority, enrolment ledger and revocation list."""

    def __init__(
        self,
        authority: CertificateAuthority,
        store: EnrolmentStore,
        *,
        leaf_lifetime: timedelta,
        gateway_endpoint: str = "",
    ) -> None:
        self._authority = authority
        self._store = store
        self._leaf_lifetime = leaf_lifetime
        self._gateway_endpoint = gateway_endpoint
        self._revoked: set[str] = set()
        self._lock = threading.Lock()
        self.refresh_revocations()

    @property
    def authority(self) -> CertificateAuthority:
        return self._authority

    @property
    def trust_domain(self) -> str:
        return self._authority.trust_domain

    @property
    def ca_bundle_pem(self) -> bytes:
        return self._authority.ca_bundle_pem()

    @property
    def gateway_endpoint(self) -> str:
        return self._gateway_endpoint

    # --- revocation --------------------------------------------------------

    def refresh_revocations(self) -> set[str]:
        """Re-read the revocation list. Cheap enough to do on every connect."""
        serials = self._store.revoked_serials()
        with self._lock:
            self._revoked = serials
        return serials

    def is_revoked(self, serial: str) -> bool:
        with self._lock:
            return serial in self._revoked

    def revoked_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._revoked)

    # --- resolution --------------------------------------------------------

    def resolve(self, context) -> AgentIdentity:
        """Which cluster this peer is, on the evidence of its certificate.

        Raises `IdentityError` when the peer presented nothing, presented
        something unplaceable, or presented a certificate that has been
        revoked. All three are refusals, and the caller turns them into an
        aborted stream.
        """
        pem = peer_certificate_pem(context)
        if pem is None:
            raise IdentityError(
                "No client certificate was presented. An agent must enrol "
                "(Register) before it can connect."
            )

        identity = identity_from_pem(pem, self.trust_domain)

        if self.is_revoked(identity.serial):
            raise IdentityError(
                f"Certificate {identity.serial} for cluster {identity.cluster_id} has been revoked."
            )
        return identity

    # --- issuance ----------------------------------------------------------

    def register(self, request, context) -> GrantedCertificate:
        """Serve one `Register` call, as enrolment or as renewal.

        Which one it is is decided by what the transport proves, never by what
        the request claims: a peer certificate means renewal, its absence means
        the bootstrap token has to carry the whole weight.
        """
        peer = peer_certificate_pem(context)
        if peer is not None:
            return self._renew(request, peer)
        return self._enrol(request)

    def _enrol(self, request) -> GrantedCertificate:
        token = request.bootstrap_token
        if not token:
            raise RegistrationRefused(
                "A bootstrap token is required to enrol.",
                grpc.StatusCode.UNAUTHENTICATED,
            )

        # Spent before anything is issued, and spent atomically. Two agents
        # racing the same token produce one certificate, not two.
        cluster_id = self._store.spend_token(token)
        if cluster_id is None:
            # Unknown, already spent and expired are one answer on purpose:
            # the caller is unauthenticated and has not earned the difference.
            raise RegistrationRefused(
                "That bootstrap token is not valid. Tokens are single-use and "
                "short-lived; issue a new one with `agentctl issue-token`.",
                grpc.StatusCode.UNAUTHENTICATED,
            )

        claimed = request.cluster_id
        if claimed and claimed != cluster_id:
            # The token decides the name. Saying so out loud rather than
            # silently overriding, because an agent installed with the wrong
            # cluster id should be fixed, not quietly renamed.
            raise RegistrationRefused(
                f"This token enrols cluster {cluster_id!r}, but the agent asked "
                f"to register as {claimed!r}. The token decides the name; fix "
                f"the agent's --cluster flag or issue a token for {claimed!r}."
            )

        # The tenant comes from whoever minted the token, carried on the
        # ambient scope through to the certificate. The enrolling agent has no
        # say in it, exactly as it has no say in its cluster id.
        from app.tenancy import current_tenant

        granted = self._issue(
            request.certificate_signing_request, cluster_id, tenant=current_tenant()
        )
        logger.info(
            "Enrolled cluster {cluster} as certificate {serial}, valid until {expiry}",
            cluster=cluster_id,
            serial=granted.serial,
            expiry=granted.expires_at.isoformat(),
        )
        return granted

    def _renew(self, request, peer_pem: bytes) -> GrantedCertificate:
        try:
            current = identity_from_pem(peer_pem, self.trust_domain)
        except IdentityError as exc:
            raise RegistrationRefused(str(exc), grpc.StatusCode.UNAUTHENTICATED) from exc

        if self.is_revoked(current.serial):
            # Otherwise revocation would be a speed bump: a revoked agent could
            # renew itself back into the fleet before anyone noticed.
            raise RegistrationRefused(
                f"Certificate {current.serial} has been revoked and cannot be renewed."
            )

        claimed = request.cluster_id
        if claimed and claimed != current.cluster_id:
            raise RegistrationRefused(
                f"Certificate {current.serial} names cluster {current.cluster_id!r}; "
                f"a renewal cannot rename it to {claimed!r}."
            )

        if request.bootstrap_token:
            # Not fatal — the credential that mattered was the certificate —
            # but an agent sending both is confused, and the token would
            # otherwise be silently wasted.
            logger.warning(
                "Cluster {cluster} sent a bootstrap token on a renewal; ignoring it. "
                "Renewal is authenticated by the current certificate.",
                cluster=current.cluster_id,
            )

        # Renewal keeps the tenant the certificate already carries, for the
        # same reason it keeps the cluster: a renewal is not a re-enrolment.
        granted = self._issue(
            request.certificate_signing_request, current.cluster_id, tenant=current.tenant
        )
        logger.info(
            "Renewed cluster {cluster}: {old} → {new}, valid until {expiry}. The old "
            "certificate stays valid until it expires, which is the overlap window "
            "that keeps in-flight collections alive.",
            cluster=current.cluster_id,
            old=current.serial,
            new=granted.serial,
            expiry=granted.expires_at.isoformat(),
        )
        return granted

    def _issue(self, csr_pem: bytes, cluster_id: str, tenant: str = "") -> GrantedCertificate:
        if not csr_pem:
            raise RegistrationRefused(
                "A certificate signing request is required. The agent generates "
                "its own key and sends only the public half.",
                grpc.StatusCode.INVALID_ARGUMENT,
            )
        if not valid_cluster_id(cluster_id):
            raise RegistrationRefused(
                f"{cluster_id!r} is not a usable cluster id.",
                grpc.StatusCode.INVALID_ARGUMENT,
            )

        try:
            issued = self._authority.issue_from_csr(
                csr_pem, cluster_id, self._leaf_lifetime, tenant=tenant
            )
        except CertificateAuthorityError as exc:
            raise RegistrationRefused(str(exc), grpc.StatusCode.INVALID_ARGUMENT) from exc

        self._store.record_certificate(issued.serial, cluster_id, issued.not_after)

        return GrantedCertificate(
            certificate_pem=issued.certificate_pem,
            ca_bundle_pem=self.ca_bundle_pem,
            expires_at=issued.not_after,
            cluster_id=cluster_id,
            serial=issued.serial,
        )

    # --- administration ----------------------------------------------------

    def revoke(self, serial: str, reason: str = "") -> bool:
        revoked = self._store.revoke_certificate(serial, reason)
        self.refresh_revocations()
        return revoked

    def revoke_cluster(self, cluster_id: str, reason: str = "") -> int:
        count = self._store.revoke_cluster(cluster_id, reason)
        self.refresh_revocations()
        return count


def expiring_soon(identity: AgentIdentity, within: timedelta) -> bool:
    """Whether a resolved identity is close enough to expiry to be worth saying."""
    if identity.expires_at is None:
        return False
    return identity.expires_at - datetime.now(UTC) <= within
