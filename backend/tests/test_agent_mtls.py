"""The gateway over real mTLS, against a real gRPC client.

Hermetic on purpose. This is the test that would have to fail before an
identity hole could ship, so it must run in the default suite — which means no
kind cluster, no Go binary and no CA on disk. It stands up an actual
`AgentGateway` with an actual TLS handshake on a loopback port, and drives it
with a `grpc.aio` client that plays the part of the agent.

What it pins:

- an enrolling agent exchanges a single-use token for a certificate, and the
  token cannot be spent twice;
- `Connect` without a client certificate is refused;
- a certificate from another CA is refused by the handshake itself;
- `hello` cannot override the certificate, and a contradiction ends the stream;
- renewal is authenticated by the current certificate and cannot rename;
- revocation drops a *live* stream, not just the next connection.
"""

import asyncio
from datetime import timedelta

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.gateway.identity import AgentIdentityService
from app.gateway.server import AgentGateway
from app.gateway.session import AgentRegistry
from app.security.ca import CertificateAuthority
from app.security.enrolment import FileEnrolmentStore
from app.security.identity import identity_from_pem
from app.wire.gen.agent.v1 import agent_pb2, agent_pb2_grpc

TRUST_DOMAIN = "test.local"
CLUSTER = "prod-eu-1"

# The gateway's own certificate is issued for "localhost"; the client overrides
# the SNI/authority so it can dial 127.0.0.1 and still verify the name.
DIAL_OPTIONS = (("grpc.ssl_target_name_override", "localhost"),)


def new_key_and_csr() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    """What an agent does locally: generate a key, send only the public half."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(key, hashes.SHA256())
    )
    return key, csr.public_bytes(serialization.Encoding.PEM)


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class Harness:
    """A running gateway, and the pieces a test needs to talk to it."""

    def __init__(self, gateway: AgentGateway, port: int, registry, store, service):
        self.gateway = gateway
        self.port = port
        self.enrolment_port = gateway.enrolment_port
        self.registry = registry
        self.store = store
        self.service = service
        self._channels: list[grpc.aio.Channel] = []

    @property
    def ca_bundle(self) -> bytes:
        return self.service.ca_bundle_pem

    def channel(self, port: int, certificate: bytes = b"", key: bytes = b"") -> grpc.aio.Channel:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.ca_bundle,
            private_key=key or None,
            certificate_chain=certificate or None,
        )
        channel = grpc.aio.secure_channel(f"127.0.0.1:{port}", credentials, options=DIAL_OPTIONS)
        self._channels.append(channel)
        return channel

    async def enrol(self, token: str, cluster_id: str = CLUSTER):
        """Play the agent's bootstrap: token in, certificate out."""
        key, csr = new_key_and_csr()
        channel = self.channel(self.enrolment_port)
        response = await agent_pb2_grpc.AgentGatewayStub(channel).Register(
            agent_pb2.RegistrationRequest(
                bootstrap_token=token,
                cluster_id=cluster_id,
                certificate_signing_request=csr,
                agent_version="test",
            ),
            timeout=10,
        )
        return response, key

    async def close(self):
        for channel in self._channels:
            await channel.close()


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_revocation_sweep_seconds", 1.0)
    monkeypatch.setattr(settings, "agent_gateway_dns_names", "localhost")
    monkeypatch.setattr(settings, "agent_gateway_ip_addresses", "127.0.0.1")

    authority = CertificateAuthority.create(TRUST_DOMAIN)
    store = FileEnrolmentStore(tmp_path / "enrolment.json")
    service = AgentIdentityService(authority, store, leaf_lifetime=timedelta(days=90))

    registry = AgentRegistry()
    gateway = AgentGateway(
        port=0, registry=registry, enrolment_port=0, identity_service=service, mtls=True
    )
    port = await gateway.start()

    harness = Harness(gateway, port, registry, store, service)
    try:
        yield harness
    finally:
        await harness.close()
        await gateway.stop()


async def open_stream(harness: Harness, certificate: bytes, key: bytes, hello_cluster: str):
    """Open a Connect stream and send hello. Returns the call."""
    channel = harness.channel(harness.port, certificate, key)
    call = agent_pb2_grpc.AgentGatewayStub(channel).Connect()
    await call.write(
        agent_pb2.AgentMessage(
            hello=agent_pb2.AgentHello(
                cluster_id=hello_cluster,
                agent_version="test",
                supported_kinds=["k8s.pods"],
                protocol_version=1,
            )
        )
    )
    return call


async def wait_for_session(registry: AgentRegistry, cluster_id: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = registry.get(cluster_id)
        if session is not None:
            return session
        await asyncio.sleep(0.02)
    return None


class TestEnrolment:
    async def test_a_token_buys_a_certificate(self, harness):
        token = harness.store.issue_token(CLUSTER)
        response, _ = await harness.enrol(token)

        assert response.certificate
        assert response.ca_bundle == harness.ca_bundle
        assert identity_from_pem(response.certificate, TRUST_DOMAIN).cluster_id == CLUSTER

    async def test_the_certificate_certifies_the_agents_own_key(self, harness):
        """The reason Register takes a CSR rather than returning a keypair.

        Two halves of one property: the platform certified the key the agent
        generated locally, and the request that asked for it had no field in
        which a private key could have travelled.
        """
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        certificate = x509.load_pem_x509_certificate(response.certificate)
        assert certificate.public_key().public_numbers() == key.public_key().public_numbers()

        fields = {field.name for field in agent_pb2.RegistrationRequest.DESCRIPTOR.fields}
        assert "certificate_signing_request" in fields
        assert not {name for name in fields if "private" in name or "key" in name}

    async def test_a_token_cannot_be_spent_twice(self, harness):
        token = harness.store.issue_token(CLUSTER)
        await harness.enrol(token)

        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await harness.enrol(token)
        assert refusal.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_the_token_decides_the_cluster_not_the_agent(self, harness):
        token = harness.store.issue_token(CLUSTER)

        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await harness.enrol(token, cluster_id="somebody-elses-cluster")
        assert refusal.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert CLUSTER in refusal.value.details()

    async def test_enrolment_without_a_token_is_refused(self, harness):
        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await harness.enrol("")
        assert refusal.value.code() == grpc.StatusCode.UNAUTHENTICATED


class TestOnlyACertificateGetsAStream:
    async def test_connect_without_a_certificate_is_refused(self, harness):
        """The enrolment listener exists for Register; it cannot carry a stream."""
        channel = harness.channel(harness.enrolment_port)
        call = agent_pb2_grpc.AgentGatewayStub(channel).Connect()
        await call.write(agent_pb2.AgentMessage(hello=agent_pb2.AgentHello(cluster_id=CLUSTER)))

        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await call.read()
        assert refusal.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_the_gateway_port_will_not_talk_without_a_client_certificate(self, harness):
        channel = harness.channel(harness.port)
        call = agent_pb2_grpc.AgentGatewayStub(channel).Connect()

        with pytest.raises(grpc.aio.AioRpcError):
            await call.write(agent_pb2.AgentMessage(hello=agent_pb2.AgentHello(cluster_id=CLUSTER)))
            await call.read()

    async def test_a_certificate_from_another_ca_is_refused(self, harness):
        """Not by us — by the handshake, which is why the failure arrives early.

        The impostor CA uses the same trust domain and issues a structurally
        perfect certificate for the same cluster. Everything above TLS would
        happily place it; nothing above TLS ever sees it.
        """
        impostor = CertificateAuthority.create(TRUST_DOMAIN)
        key, csr = new_key_and_csr()
        issued = impostor.issue_from_csr(csr, CLUSTER)

        # The refusal may surface on the write or the read depending on when
        # the handshake completes; both are the same rejection.
        with pytest.raises(grpc.aio.AioRpcError):
            call = await open_stream(harness, issued.certificate_pem, key_pem(key), CLUSTER)
            await call.read()

        assert harness.registry.get(CLUSTER) is None


class TestHelloCannotOverrideTheCertificate:
    async def test_a_matching_hello_is_accepted(self, harness):
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        await open_stream(harness, response.certificate, key_pem(key), CLUSTER)
        session = await wait_for_session(harness.registry, CLUSTER)

        assert session is not None
        assert session.identity.verified
        assert session.cluster_id == CLUSTER

    async def test_an_empty_hello_takes_its_name_from_the_certificate(self, harness):
        """Not a contradiction: supplying the name is what a certificate is for."""
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        await open_stream(harness, response.certificate, key_pem(key), "")
        session = await wait_for_session(harness.registry, CLUSTER)

        assert session is not None
        assert session.cluster_id == CLUSTER

    async def test_a_contradicting_hello_ends_the_stream(self, harness):
        """The sharp edge: the certificate is the identity, and a mismatch is loud."""
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        call = await open_stream(harness, response.certificate, key_pem(key), "prod-us-1")

        # Bounded, because the interesting failure is the stream being
        # *accepted*: without the check the read simply blocks for ever, and a
        # hanging test reports nothing useful.
        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await asyncio.wait_for(call.read(), timeout=10)
        assert refusal.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # Both values named, because the operator has to know which is wrong.
        assert CLUSTER in refusal.value.details()
        assert "prod-us-1" in refusal.value.details()

        # And critically: no session under *either* name.
        assert harness.registry.get(CLUSTER) is None
        assert harness.registry.get("prod-us-1") is None


class TestRenewal:
    async def test_a_current_certificate_buys_the_next_one(self, harness):
        """No token, no human — this is what makes rotation work at fleet scale."""
        token = harness.store.issue_token(CLUSTER)
        first, key = await harness.enrol(token)

        _, csr2 = new_key_and_csr()
        channel = harness.channel(harness.port, first.certificate, key_pem(key))
        second = await agent_pb2_grpc.AgentGatewayStub(channel).Register(
            agent_pb2.RegistrationRequest(cluster_id=CLUSTER, certificate_signing_request=csr2),
            timeout=10,
        )

        assert second.certificate
        assert second.certificate != first.certificate

        assert identity_from_pem(second.certificate, TRUST_DOMAIN).cluster_id == CLUSTER

    async def test_the_old_certificate_still_works_after_renewal(self, harness):
        """The overlap window. Renewing must not invalidate a live stream."""
        token = harness.store.issue_token(CLUSTER)
        first, key = await harness.enrol(token)

        _, csr2 = new_key_and_csr()
        channel = harness.channel(harness.port, first.certificate, key_pem(key))
        await agent_pb2_grpc.AgentGatewayStub(channel).Register(
            agent_pb2.RegistrationRequest(cluster_id=CLUSTER, certificate_signing_request=csr2),
            timeout=10,
        )

        await open_stream(harness, first.certificate, key_pem(key), CLUSTER)
        assert await wait_for_session(harness.registry, CLUSTER) is not None

    async def test_a_renewal_cannot_rename_the_cluster(self, harness):
        token = harness.store.issue_token(CLUSTER)
        first, key = await harness.enrol(token)

        _, csr2 = new_key_and_csr()
        channel = harness.channel(harness.port, first.certificate, key_pem(key))

        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await agent_pb2_grpc.AgentGatewayStub(channel).Register(
                agent_pb2.RegistrationRequest(
                    cluster_id="prod-us-1", certificate_signing_request=csr2
                ),
                timeout=10,
            )
        assert refusal.value.code() == grpc.StatusCode.PERMISSION_DENIED


class TestRevocation:
    async def test_a_revoked_certificate_cannot_open_a_stream(self, harness):
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        serial = identity_from_pem(response.certificate, TRUST_DOMAIN).serial
        harness.service.revoke(serial, "test")

        call = await open_stream(harness, response.certificate, key_pem(key), CLUSTER)
        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await call.read()
        assert refusal.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_a_revoked_certificate_cannot_renew_itself(self, harness):
        """Otherwise revocation is a speed bump: renew back in before anyone looks."""
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        harness.service.revoke(identity_from_pem(response.certificate, TRUST_DOMAIN).serial, "test")

        _, csr2 = new_key_and_csr()
        channel = harness.channel(harness.port, response.certificate, key_pem(key))
        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await agent_pb2_grpc.AgentGatewayStub(channel).Register(
                agent_pb2.RegistrationRequest(certificate_signing_request=csr2), timeout=10
            )
        assert refusal.value.code() == grpc.StatusCode.PERMISSION_DENIED

    async def test_revocation_drops_a_stream_that_is_already_open(self, harness):
        """The reason the sweeper exists.

        This transport is designed around a connection that stays open for
        weeks. Revocation that only took effect at the next reconnect would be
        close to no revocation at all, so this test asserts the *live* stream
        ends — and it is the one that fails if the sweeper is removed.
        """
        token = harness.store.issue_token(CLUSTER)
        response, key = await harness.enrol(token)

        call = await open_stream(harness, response.certificate, key_pem(key), CLUSTER)
        session = await wait_for_session(harness.registry, CLUSTER)
        assert session is not None

        harness.service.revoke(identity_from_pem(response.certificate, TRUST_DOMAIN).serial, "test")

        # The sweep interval is 1s in this harness; allow a few of them.
        with pytest.raises(grpc.aio.AioRpcError) as refusal:
            await asyncio.wait_for(call.read(), timeout=10)
        assert refusal.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert harness.registry.get(CLUSTER) is None
