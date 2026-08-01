"""What a certificate says an agent is.

The one rule this module exists to enforce: **the certificate is the identity**.
An agent tells the platform its name exactly once — in the registration request,
where a single-use token has already bound that name — and never again. On every
subsequent connection the name is read out of the certificate the TLS stack
validated, and `AgentHello.cluster_id` is checked against it rather than
believed.

Identity is carried as a URI SAN in SPIFFE form:

    spiffe://<trust-domain>/cluster/<cluster-id>

ADR-005 says customers already running SPIRE should be able to bring their own
identity rather than adopt a second scheme. Naming the subject the way SPIFFE
names it is the cheap half of that promise; the expensive half (accepting an
SVID issued by someone else's SPIRE) is not M4b, but the format leaves room for
it. The Common Name is set to the same cluster id for the benefit of anyone
reading `openssl x509`, and is never trusted.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtensionOID, NameOID

SPIFFE_SCHEME = "spiffe"

# The path component under the trust domain. Kept explicit so a future
# `spiffe://.../user/...` cannot be mistaken for a cluster.
CLUSTER_PATH = "cluster"

# What a cluster id may contain.
#
# This is a security boundary, not tidiness: the id becomes a URI path segment,
# a certificate subject, a database key and a log field. A value that can carry
# `/`, whitespace or a control character could name one cluster in the
# certificate and read as another everywhere else.
CLUSTER_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,126}$")


class IdentityError(Exception):
    """A peer's certificate does not name an agent this platform can place."""


def valid_cluster_id(cluster_id: str) -> bool:
    return bool(CLUSTER_ID.match(cluster_id))


def require_cluster_id(cluster_id: str) -> str:
    if not valid_cluster_id(cluster_id):
        raise IdentityError(
            f"{cluster_id!r} is not a usable cluster id: letters, digits, dot, "
            f"dash and underscore only, up to 127 characters."
        )
    return cluster_id


def spiffe_id(trust_domain: str, cluster_id: str) -> str:
    """The SPIFFE URI naming one cluster's agent."""
    return f"{SPIFFE_SCHEME}://{trust_domain}/{CLUSTER_PATH}/{require_cluster_id(cluster_id)}"


def parse_spiffe_id(uri: str, trust_domain: str) -> str:
    """The cluster id inside a SPIFFE URI, or raise.

    Every component is checked. A URI from another trust domain is refused
    rather than accepted-and-namespaced: this platform issues its own
    certificates, and a validly-signed certificate naming a foreign trust
    domain means something is wrong that should be looked at, not routed.
    """
    prefix = f"{SPIFFE_SCHEME}://{trust_domain}/{CLUSTER_PATH}/"
    if not uri.startswith(prefix):
        raise IdentityError(f"{uri!r} does not name a cluster in trust domain {trust_domain!r}.")
    return require_cluster_id(uri[len(prefix) :])


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Who the platform believes it is talking to, and on what evidence."""

    cluster_id: str
    # Lowercase hex, the certificate's serial. The handle revocation uses.
    serial: str = ""
    # "certificate" when TLS proved it; "declared" on the plaintext
    # development path, where there is nothing to prove it with.
    source: str = "certificate"
    expires_at: datetime | None = None

    @property
    def verified(self) -> bool:
        """True when this identity was proved rather than asserted."""
        return self.source == "certificate"

    def describe(self) -> str:
        if not self.verified:
            return f"{self.cluster_id} (declared, unverified)"
        return f"{self.cluster_id} (certificate {self.serial})"


def format_serial(serial: int) -> str:
    """A certificate serial as the platform stores and logs it."""
    return f"{serial:x}"


def identity_from_certificate(certificate: x509.Certificate, trust_domain: str) -> AgentIdentity:
    """Read an agent identity out of a validated peer certificate.

    The certificate is assumed already validated against the CA by the TLS
    stack. What happens here is placement, not authentication: deciding *which*
    agent a proven-genuine certificate belongs to.
    """
    try:
        san = certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
    except x509.ExtensionNotFound as exc:
        raise IdentityError(
            "The peer certificate carries no subject alternative name, so it names no cluster."
        ) from exc

    uris = san.get_values_for_type(x509.UniformResourceIdentifier)  # type: ignore[attr-defined]
    if not uris:
        raise IdentityError("The peer certificate carries no SPIFFE URI.")

    # Exactly one, deliberately. A certificate naming two clusters has no
    # single answer to "whose evidence is this?", and picking the first would
    # be a guess.
    if len(uris) > 1:
        raise IdentityError(f"The peer certificate names {len(uris)} identities; an agent has one.")

    cluster_id = parse_spiffe_id(uris[0], trust_domain)
    return AgentIdentity(
        cluster_id=cluster_id,
        serial=format_serial(certificate.serial_number),
        source="certificate",
        expires_at=certificate.not_valid_after_utc,
    )


def identity_from_pem(pem: bytes | str, trust_domain: str) -> AgentIdentity:
    """As `identity_from_certificate`, from the PEM gRPC hands us."""
    data = pem.encode("utf-8") if isinstance(pem, str) else pem
    try:
        certificate = x509.load_pem_x509_certificate(data)
    except Exception as exc:
        raise IdentityError("The peer certificate could not be parsed.") from exc
    return identity_from_certificate(certificate, trust_domain)


def common_name(certificate: x509.Certificate) -> str:
    """The subject CN, for logs only. Never an identity."""
    attributes = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attributes[0].value) if attributes else ""


def certificate_pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(Encoding.PEM)


def expired(certificate: x509.Certificate, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    return not (certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc)
