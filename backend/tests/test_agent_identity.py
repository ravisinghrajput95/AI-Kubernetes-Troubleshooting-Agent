"""The identity model itself: the CA, the CSR, and single-use enrolment.

Hermetic. Every certificate here is built in-process under `tmp_path`, so
`python -m pytest` still needs no cluster, no agent binary and no CA on disk.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from app.security.ca import CertificateAuthority, CertificateAuthorityError
from app.security.enrolment import FileEnrolmentStore, hash_token
from app.security.identity import (
    IdentityError,
    identity_from_pem,
    parse_spiffe_id,
    spiffe_id,
    valid_cluster_id,
)

TRUST_DOMAIN = "test.local"


def make_csr(
    subject_name: str = "whatever",
    key=None,
    uris: list[str] | None = None,
) -> tuple[bytes, ec.EllipticCurvePrivateKey]:
    """A CSR, optionally lying about who it is."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_name)])
    )
    if uris:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri) for uri in uris]),
            critical=False,
        )
    csr = builder.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM), key


@pytest.fixture
def authority() -> CertificateAuthority:
    return CertificateAuthority.create(TRUST_DOMAIN)


@pytest.fixture
def store(tmp_path) -> FileEnrolmentStore:
    return FileEnrolmentStore(tmp_path / "enrolment.json")


class TestTheCertificateNamesTheCluster:
    def test_an_issued_certificate_carries_a_spiffe_identity(self, authority):
        csr, _ = make_csr()
        issued = authority.issue_from_csr(csr, "prod-eu-1")

        identity = identity_from_pem(issued.certificate_pem, TRUST_DOMAIN)
        assert identity.cluster_id == "prod-eu-1"
        assert identity.serial == issued.serial
        assert identity.verified

    def test_the_csr_cannot_choose_its_own_name(self, authority):
        """The whole model rests on this: a CSR contributes a key, not a name."""
        csr, _ = make_csr(
            subject_name="prod-us-1",
            uris=[spiffe_id(TRUST_DOMAIN, "prod-us-1"), "spiffe://evil/cluster/root"],
        )
        issued = authority.issue_from_csr(csr, "staging")

        identity = identity_from_pem(issued.certificate_pem, TRUST_DOMAIN)
        assert identity.cluster_id == "staging"

        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        # Exactly one, and it is the platform's, not the requester's.
        assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
            spiffe_id(TRUST_DOMAIN, "staging")
        ]

    def test_a_certificate_from_another_trust_domain_is_refused(self):
        other = CertificateAuthority.create("someone-else.local")
        csr, _ = make_csr()
        issued = other.issue_from_csr(csr, "prod-eu-1")

        with pytest.raises(IdentityError):
            identity_from_pem(issued.certificate_pem, TRUST_DOMAIN)

    def test_a_certificate_naming_two_clusters_is_refused(self, authority):
        """Two identities have no single answer to whose evidence this is."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "two")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "two")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(spiffe_id(TRUST_DOMAIN, "a")),
                        x509.UniformResourceIdentifier(spiffe_id(TRUST_DOMAIN, "b")),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        with pytest.raises(IdentityError, match="2 identities"):
            identity_from_pem(certificate.public_bytes(serialization.Encoding.PEM), TRUST_DOMAIN)

    def test_the_common_name_is_never_the_identity(self, authority):
        """A certificate with a CN but no SAN names nobody."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "prod-eu-1")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        with pytest.raises(IdentityError):
            identity_from_pem(certificate.public_bytes(serialization.Encoding.PEM), TRUST_DOMAIN)

    @pytest.mark.parametrize(
        "cluster_id",
        ["", "../etc", "a/b", "with space", "-leading", "x" * 200, "spiffe://x"],
    )
    def test_a_cluster_id_that_could_mean_two_things_is_refused(self, authority, cluster_id):
        assert not valid_cluster_id(cluster_id)
        csr, _ = make_csr()
        with pytest.raises(IdentityError):
            authority.issue_from_csr(csr, cluster_id)

    def test_spiffe_ids_round_trip(self):
        assert parse_spiffe_id(spiffe_id(TRUST_DOMAIN, "prod-eu-1"), TRUST_DOMAIN) == (
            "default",
            "prod-eu-1",
        )

    def test_a_tenanted_identity_round_trips(self):
        uri = spiffe_id(TRUST_DOMAIN, "prod-eu-1", tenant="acme")
        assert uri == f"spiffe://{TRUST_DOMAIN}/tenant/acme/cluster/prod-eu-1"
        assert parse_spiffe_id(uri, TRUST_DOMAIN) == ("acme", "prod-eu-1")

    def test_an_untenanted_certificate_belongs_to_the_default_tenant(self):
        """Every certificate issued before M6 carries the untenanted form.

        They must keep working and must land somewhere unambiguous, or an
        upgrade silently disconnects the fleet.
        """
        tenant, cluster = parse_spiffe_id(
            f"spiffe://{TRUST_DOMAIN}/cluster/prod-eu-1", TRUST_DOMAIN
        )
        assert (tenant, cluster) == ("default", "prod-eu-1")

    def test_a_user_spiffe_id_is_not_a_cluster(self):
        with pytest.raises(IdentityError):
            parse_spiffe_id(f"spiffe://{TRUST_DOMAIN}/user/alice", TRUST_DOMAIN)

    @pytest.mark.parametrize(
        "uri",
        [
            # A cluster id that smuggles a tenant path, and a tenant that
            # smuggles a cluster — both would resolve to the wrong owner if the
            # URI were matched by prefix rather than by segment.
            "spiffe://{d}/cluster/prod/../../tenant/acme/cluster/prod",
            "spiffe://{d}/tenant/acme/cluster/a/b",
            "spiffe://{d}/tenant//cluster/prod",
            "spiffe://{d}/tenant/acme/prod",
            "spiffe://{d}/",
        ],
    )
    def test_a_malformed_identity_is_refused_rather_than_guessed(self, uri):
        with pytest.raises(IdentityError):
            parse_spiffe_id(uri.format(d=TRUST_DOMAIN), TRUST_DOMAIN)


class TestTheCaRefusesWhatItShould:
    def test_a_csr_whose_signature_does_not_verify_is_refused(self, authority):
        """Proof of possession. Without it the CA is a signing oracle.

        The corruption is a single bit inside the DER signature, so the request
        still *parses* — which is what makes this a test of the signature check
        rather than of the parser. The assertions pin that distinction: the
        structure survives, `is_signature_valid` does not, and the refusal names
        the private key.
        """
        csr, _ = make_csr()
        der = x509.load_pem_x509_csr(csr).public_bytes(serialization.Encoding.DER)

        # The last byte of the DER is inside the signature value.
        broken_der = bytearray(der)
        broken_der[-1] ^= 0x01
        broken = x509.load_der_x509_csr(bytes(broken_der))

        assert broken.subject == x509.load_pem_x509_csr(csr).subject  # still parses
        assert broken.is_signature_valid is False

        with pytest.raises(CertificateAuthorityError, match="private key"):
            authority.issue_from_csr(broken.public_bytes(serialization.Encoding.PEM), "prod-eu-1")

    def test_a_non_ec_key_is_refused(self, authority):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rsa")]))
            .sign(key, hashes.SHA256())
        )
        with pytest.raises(CertificateAuthorityError, match="P-256"):
            authority.issue_from_csr(csr.public_bytes(serialization.Encoding.PEM), "prod-eu-1")

    def test_garbage_is_refused(self, authority):
        with pytest.raises(CertificateAuthorityError):
            authority.issue_from_csr(b"not a csr", "prod-eu-1")

    def test_an_agent_certificate_cannot_sign_or_serve(self, authority):
        """An agent identity must not be usable to stand up a gateway."""
        csr, _ = make_csr()
        issued = authority.issue_from_csr(csr, "prod-eu-1")
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)

        basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert basic.ca is False

        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        assert usage.key_cert_sign is False

        extended = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in extended
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH not in extended


class TestTheCaOnDisk:
    def test_it_generates_once_and_loads_thereafter(self, tmp_path):
        certificate_path = tmp_path / "ca.crt"
        key_path = tmp_path / "ca.key"

        first = CertificateAuthority.load_or_create(certificate_path, key_path, TRUST_DOMAIN)
        second = CertificateAuthority.load_or_create(certificate_path, key_path, TRUST_DOMAIN)

        # The same CA, or every restart would invalidate the entire fleet.
        assert first.ca_bundle_pem() == second.ca_bundle_pem()

    def test_the_key_is_not_world_readable(self, tmp_path):
        key_path = tmp_path / "ca.key"
        CertificateAuthority.load_or_create(tmp_path / "ca.crt", key_path, TRUST_DOMAIN)
        assert key_path.stat().st_mode & 0o077 == 0

    def test_half_a_ca_is_refused_rather_than_completed(self, tmp_path):
        """Generating a fresh key over an existing certificate would be silent ruin."""
        certificate_path = tmp_path / "ca.crt"
        certificate_path.write_bytes(b"-----BEGIN CERTIFICATE-----\n")

        with pytest.raises(CertificateAuthorityError, match="one half"):
            CertificateAuthority.load_or_create(certificate_path, tmp_path / "ca.key", TRUST_DOMAIN)


class TestBootstrapTokensAreSingleUse:
    def test_a_token_is_spent_exactly_once(self, store):
        token = store.issue_token("prod-eu-1")

        assert store.spend_token(token) == "prod-eu-1"
        assert store.spend_token(token) is None

    def test_single_use_survives_a_restart(self, tmp_path):
        """The file store exists so a replayed token cannot outlive a reboot."""
        path = tmp_path / "enrolment.json"
        token = FileEnrolmentStore(path).issue_token("prod-eu-1")

        assert FileEnrolmentStore(path).spend_token(token) == "prod-eu-1"
        assert FileEnrolmentStore(path).spend_token(token) is None

    def test_an_expired_token_is_refused(self, store):
        token = store.issue_token("prod-eu-1", timedelta(seconds=-1))
        assert store.spend_token(token) is None

    def test_an_unknown_token_is_refused(self, store):
        store.issue_token("prod-eu-1")
        assert store.spend_token("k8sagt_nothing") is None

    def test_the_token_itself_is_never_stored(self, tmp_path):
        path = tmp_path / "enrolment.json"
        token = FileEnrolmentStore(path).issue_token("prod-eu-1")

        contents = path.read_text(encoding="utf-8")
        assert token not in contents
        assert hash_token(token) in contents

    def test_a_token_is_bound_to_its_cluster(self, store):
        store.issue_token("prod-eu-1")
        token = store.issue_token("staging")
        assert store.spend_token(token) == "staging"

    def test_an_unreadable_store_refuses_rather_than_forgets(self, tmp_path):
        """Continuing past a corrupt ledger would silently accept replays."""
        path = tmp_path / "enrolment.json"
        path.write_text("{ not json", encoding="utf-8")

        with pytest.raises(RuntimeError, match="single-use"):
            FileEnrolmentStore(path).spend_token("k8sagt_anything")


class TestRevocation:
    def test_a_revoked_serial_is_listed(self, store):
        store.record_certificate("aa11", "prod-eu-1", datetime.now(UTC) + timedelta(days=1))
        assert store.revoked_serials() == set()

        assert store.revoke_certificate("aa11", "compromised") is True
        assert store.revoked_serials() == {"aa11"}

    def test_revoking_twice_reports_the_second_as_a_no_op(self, store):
        store.record_certificate("aa11", "prod-eu-1", datetime.now(UTC) + timedelta(days=1))
        assert store.revoke_certificate("aa11") is True
        assert store.revoke_certificate("aa11") is False

    def test_a_cluster_can_be_revoked_wholesale(self, store):
        expiry = datetime.now(UTC) + timedelta(days=1)
        store.record_certificate("aa11", "prod-eu-1", expiry)
        store.record_certificate("bb22", "prod-eu-1", expiry)
        store.record_certificate("cc33", "staging", expiry)

        assert store.revoke_cluster("prod-eu-1", "rotating") == 2
        assert store.revoked_serials() == {"aa11", "bb22"}

    def test_an_already_expired_certificate_is_not_counted(self, store):
        """TLS already refuses it; counting it would misreport the blast radius."""
        store.record_certificate("old", "prod-eu-1", datetime.now(UTC) - timedelta(days=1))
        assert store.revoke_cluster("prod-eu-1") == 0
