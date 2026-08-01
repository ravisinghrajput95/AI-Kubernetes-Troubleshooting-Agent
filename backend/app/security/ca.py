"""The certificate authority the platform issues agent identities from.

Scope, stated plainly because the failure mode of overclaiming here is a
security hole: this is a **development CA**. It generates a P-256 signing key,
writes it to disk at 0600, and signs leaf certificates from it. That is the
right shape for a single deployment and for tests, and it is not what a
regulated customer should run — a production platform wants an external issuer
(Vault, a corporate PKI, SPIRE) holding the key in an HSM.

The design accommodates that without a rewrite: everything above this module
depends on `issue()` returning a PEM chain, and nothing depends on the key being
local. Swapping the implementation is a constructor change.

Two properties matter more than any of the above:

- **The CSR contributes its public key and nothing else.** Subject, SANs and
  extensions in the request are discarded, and the leaf is built from the
  cluster id the caller has already been authorised for. An agent that could
  name itself in its CSR could name another cluster, which would make the whole
  exercise decorative.
- **The CSR's self-signature is verified**, which is what proves the requester
  holds the private key for the public key it is asking to have certified.
  Without it, an attacker could have a certificate issued for someone else's
  key — useless for impersonation, but it turns the CA into a signing oracle.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from loguru import logger

from app.security.identity import format_serial, require_cluster_id, spiffe_id

# P-256 throughout: small certificates, fast handshakes, and supported by Go's
# standard library and every TLS stack a customer might terminate on.
CURVE = ec.SECP256R1()

# The development CA's own life. Long, because rotating it means re-enrolling
# every agent in the fleet, and this key is expected to be replaced by an
# external issuer long before it expires.
CA_LIFETIME = timedelta(days=3650)

# How long an issued agent certificate is valid, per ADR-005.
DEFAULT_LEAF_LIFETIME = timedelta(days=90)

# Clock skew allowance. A leaf issued at T is valid from T minus this, so an
# agent whose clock runs a minute fast does not reject its own new certificate.
BACKDATE = timedelta(minutes=5)


class CertificateAuthorityError(Exception):
    """The CA could not issue, load or create what was asked of it."""


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    """One signed leaf, and the facts the platform records about it."""

    certificate_pem: bytes
    serial: str
    cluster_id: str
    not_before: datetime
    not_after: datetime


def _write_private(path: Path, data: bytes) -> None:
    """Write key material so only the owner can read it, without a window.

    Opened with the mode set at creation rather than chmod-ed afterwards: the
    gap between `open()` and `chmod()` is a real window in which a private key
    is world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


class CertificateAuthority:
    """Signs agent and gateway certificates for one trust domain."""

    def __init__(
        self,
        certificate: x509.Certificate,
        key: ec.EllipticCurvePrivateKey,
        trust_domain: str,
    ) -> None:
        self._certificate = certificate
        self._key = key
        self._trust_domain = trust_domain

    # --- construction ------------------------------------------------------

    @classmethod
    def load_or_create(
        cls,
        certificate_path: Path,
        key_path: Path,
        trust_domain: str,
    ) -> "CertificateAuthority":
        """Load the CA from disk, generating a development one if absent.

        Generating rather than refusing is a deliberate trade, and it is why
        the log line says what it says: mTLS being the default is worth more
        than making an agent connection impossible until someone provisions a
        PKI. What is *not* acceptable is generating one silently, so this is
        the loudest line the gateway prints.
        """
        if certificate_path.exists() and key_path.exists():
            return cls.load(certificate_path, key_path, trust_domain)

        if certificate_path.exists() or key_path.exists():
            raise CertificateAuthorityError(
                f"Exactly one half of the CA is present ({certificate_path} / "
                f"{key_path}). Refusing to generate the other half over it — "
                f"remove the orphan, or supply both."
            )

        authority = cls.create(trust_domain)
        _write_private(key_path, authority.private_key_pem())
        certificate_path.parent.mkdir(parents=True, exist_ok=True)
        certificate_path.write_bytes(authority.ca_bundle_pem())
        logger.warning(
            "Generated a DEVELOPMENT certificate authority for trust domain "
            "{domain} at {path}. It is fine for local use and for CI; for any "
            "shared deployment, supply AGENT_CA_CERT_FILE and AGENT_CA_KEY_FILE "
            "from a CA you actually control.",
            domain=trust_domain,
            path=certificate_path,
        )
        return authority

    @classmethod
    def create(cls, trust_domain: str) -> "CertificateAuthority":
        key = ec.generate_private_key(CURVE)
        now = datetime.now(UTC)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, f"{trust_domain} agent CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, trust_domain),
            ]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - BACKDATE)
            .not_valid_after(now + CA_LIFETIME)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        return cls(certificate, key, trust_domain)

    @classmethod
    def load(
        cls, certificate_path: Path, key_path: Path, trust_domain: str
    ) -> "CertificateAuthority":
        try:
            certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except Exception as exc:
            raise CertificateAuthorityError(
                f"Could not load the agent CA from {certificate_path} / {key_path}: {exc}"
            ) from exc

        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise CertificateAuthorityError(
                "The agent CA key must be an EC private key; agents and the "
                "gateway both expect P-256."
            )
        return cls(certificate, key, trust_domain)

    # --- material ----------------------------------------------------------

    @property
    def trust_domain(self) -> str:
        return self._trust_domain

    def ca_bundle_pem(self) -> bytes:
        """What a peer needs to verify certificates this CA issued."""
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    def private_key_pem(self) -> bytes:
        return self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    # --- issuance ----------------------------------------------------------

    def issue_from_csr(
        self,
        csr_pem: bytes,
        cluster_id: str,
        lifetime: timedelta = DEFAULT_LEAF_LIFETIME,
    ) -> IssuedCertificate:
        """Certify the public key in `csr_pem` as belonging to `cluster_id`.

        Everything the CSR says about *who it is* is thrown away. The only
        thing taken from it is the public key, and the only thing that decides
        the name is the `cluster_id` the caller has already been authorised
        for.
        """
        require_cluster_id(cluster_id)

        try:
            csr = x509.load_pem_x509_csr(csr_pem)
        except Exception as exc:
            raise CertificateAuthorityError(
                "The certificate signing request could not be parsed."
            ) from exc

        # Proof of possession: without this the CA will sign a public key the
        # requester does not hold the other half of.
        if not csr.is_signature_valid:
            raise CertificateAuthorityError(
                "The certificate signing request is not correctly self-signed, "
                "so the requester has not proved it holds the private key."
            )

        # A closed set of acceptable keys, for the same reason `ReadVerb` is a
        # closed enum: the platform decides what it will certify, rather than
        # certifying whatever turns up.
        public_key = csr.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise CertificateAuthorityError(
                "Agent keys must be EC P-256; the request asked for something else."
            )

        now = datetime.now(UTC)
        not_before = now - BACKDATE
        not_after = now + lifetime

        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cluster_id)]))
            .issuer_name(self._certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            # Client auth only. An agent certificate must not be usable to
            # stand up a server that other agents would then trust.
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=True,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(spiffe_id(self._trust_domain, cluster_id))]
                ),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )

        return IssuedCertificate(
            certificate_pem=certificate.public_bytes(serialization.Encoding.PEM),
            serial=format_serial(certificate.serial_number),
            cluster_id=cluster_id,
            not_before=not_before,
            not_after=not_after,
        )

    def issue_server_certificate(
        self,
        dns_names: list[str],
        ip_addresses: list[str],
        lifetime: timedelta = timedelta(days=365),
    ) -> tuple[bytes, bytes]:
        """A TLS server certificate for the gateway, and its key.

        Generated in memory at startup rather than persisted: agents pin the
        **CA**, not this leaf, so regenerating it per boot costs nothing and
        removes a file that would otherwise need rotating. Each gateway replica
        gets its own, all chaining to the same CA.
        """
        import ipaddress

        key = ec.generate_private_key(CURVE)
        now = datetime.now(UTC)

        alternatives: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
        for address in ip_addresses:
            try:
                alternatives.append(x509.IPAddress(ipaddress.ip_address(address)))
            except ValueError:
                logger.warning("Ignoring {address}: not an IP address", address=address)

        if not alternatives:
            raise CertificateAuthorityError(
                "A gateway certificate needs at least one name agents can dial it by."
            )

        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(
                            NameOID.COMMON_NAME, dns_names[0] if dns_names else "gateway"
                        )
                    ]
                )
            )
            .issuer_name(self._certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - BACKDATE)
            .not_valid_after(now + lifetime)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=True
            )
            .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
            .sign(self._key, hashes.SHA256())
        )

        return (
            certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
