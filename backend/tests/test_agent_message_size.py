"""How large an evidence message the gateway accepts, and what a refusal costs.

**gRPC defaults the receive limit to 4 MiB and nothing here ever set it**, which
put the transport's ceiling below the platform's own: `MAX_LIST_ITEMS` allows
2,000 objects, and a pod list crosses 4 MiB at roughly 4,700. So a large
cluster's list read was refused — and a refusal at this layer is not a degraded
read, it **ends the Connect stream**: the agent reconnects and everything in
flight for that cluster goes with it.

Measured against a real gateway before the fix: 3.7 MB accepted, 11.2 MB
refused with `RESOURCE_EXHAUSTED: Received message larger than max (11160087
vs. 4194304)`.

What this does not change is that the payload arrives whole. Bounding *that*
needs chunked evidence, which is a wire change — `docs/PERFORMANCE_ENVELOPE.md`
says so rather than implying the ceiling is gone.
"""

import asyncio
import json
from datetime import timedelta

import grpc
import pytest

from app.core.config import settings
from app.gateway.identity import AgentIdentityService
from app.gateway.server import AgentGateway
from app.gateway.session import AgentRegistry
from app.security.ca import CertificateAuthority
from app.security.enrolment import FileEnrolmentStore
from app.wire.gen.agent.v1 import agent_pb2, evidence_pb2
from tests.test_agent_mtls import CLUSTER, TRUST_DOMAIN, Harness, key_pem, open_stream

POD = {
    "metadata": {"name": "checkout-5b5fd56dbf-4cnmv", "namespace": "payments"},
    "spec": {"containers": [{"name": "app", "image": "example/checkout:1.4"}]},
    "status": {"phase": "Running"},
}

# Comfortably past the 4 MiB gRPC default, and past what MAX_LIST_ITEMS keeps:
# this is the size that used to end the stream.
BIG_PODS = 60_000


def payload(pods: int) -> bytes:
    return json.dumps({"kind": "PodList", "items": [POD] * pods}).encode()


async def gateway_with_limit(tmp_path, monkeypatch, limit: int) -> Harness:
    monkeypatch.setattr(settings, "agent_max_message_bytes", limit)
    monkeypatch.setattr(settings, "agent_gateway_dns_names", "localhost")
    monkeypatch.setattr(settings, "agent_gateway_ip_addresses", "127.0.0.1")

    authority = CertificateAuthority.create(TRUST_DOMAIN)
    store = FileEnrolmentStore(tmp_path / "enrolment.json")
    service = AgentIdentityService(authority, store, leaf_lifetime=timedelta(days=90))
    gateway = AgentGateway(
        port=0, registry=AgentRegistry(), enrolment_port=0, identity_service=service, mtls=True
    )
    port = await gateway.start()
    return Harness(gateway, port, gateway._registry, store, service)


async def send_evidence(harness: Harness, body: bytes) -> str:
    """Write one evidence message. Returns "accepted" or the refusal code."""
    token = harness.store.issue_token(CLUSTER, timedelta(minutes=5))
    response, key = await harness.enrol(token)
    call = await open_stream(harness, response.certificate, key_pem(key), CLUSTER)

    envelope = agent_pb2.EvidenceEnvelope(
        investigation_id="probe",
        request_id="r1",
        record=evidence_pb2.EvidenceRecord(
            id="k8s.pods:payments",
            kind="k8s.pods",
            status=evidence_pb2.EVIDENCE_STATUS_OK,
            payload=body,
        ),
    )
    try:
        await call.write(agent_pb2.AgentMessage(evidence=envelope))
        # A server-side rejection surfaces on read, never on write.
        try:
            await asyncio.wait_for(call.read(), timeout=3)
        except TimeoutError:
            return "accepted"
        return "accepted"
    except grpc.aio.AioRpcError as error:
        return error.code().name


@pytest.mark.parametrize("limit", [32 * 1024 * 1024])
async def test_a_list_larger_than_the_grpc_default_is_accepted(tmp_path, monkeypatch, limit):
    """The regression: 11.2 MB used to end the stream."""
    harness = await gateway_with_limit(tmp_path, monkeypatch, limit)
    body = payload(BIG_PODS)
    assert len(body) > 4 * 1024 * 1024, "the payload must exceed the gRPC default to mean anything"
    try:
        assert await send_evidence(harness, body) == "accepted", (
            f"{len(body) / 1e6:.1f} MB was refused, so a cluster this size cannot be read "
            f"through an agent at all — and the refusal takes the stream with it"
        )
    finally:
        await harness.close()
        await harness.gateway.stop()


async def test_a_message_past_the_configured_limit_is_still_refused(tmp_path, monkeypatch):
    """The control, and the reason the limit is a number rather than 'unlimited':
    an agent is a customer's process and its payload is attacker-influenced."""
    harness = await gateway_with_limit(tmp_path, monkeypatch, 1024 * 1024)
    body = payload(BIG_PODS)
    try:
        assert await send_evidence(harness, body) == "RESOURCE_EXHAUSTED"
    finally:
        await harness.close()
        await harness.gateway.stop()
