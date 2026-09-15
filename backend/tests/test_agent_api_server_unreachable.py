"""An agent that answers, whose API server does not.

Reproduced live: an enrolled agent stayed connected and heartbeating while an
iptables rule on the kind node dropped only its traffic to port 6443. Every
read came back promptly as a failure carrying client-go's own reason —
`dial tcp …:6443: i/o timeout` — and the platform recorded each one, and the
investigation, as "Kubernetes investigation failed. Verify kubeconfig, cluster
access, and kubectl permissions": kubectl advice, for a cluster no kubeconfig
was involved in reading, with the reason discarded on the way.

The provider here is the real `RemoteAgentProvider` translation fed the
record an agent sends, so the test crosses the seam the defect lived on:
agent error text into the platform's kubectl-shaped classifier.
"""

import pytest

from app.providers import remote_agent
from app.services.investigation_service import InvestigationService
from app.wire.gen.agent.v1 import evidence_pb2

DIAL = (
    'Get "https://k8s-agent-dev-control-plane:6443/api/v1/namespaces/payments/pods": '
    "dial tcp [fc00:f853:ccd:e793::2]:6443: i/o timeout"
)


class RemoteAgentProvider(remote_agent.RemoteAgentProvider):
    """The real result translation; only the stream is replaced.

    Named like the class `select_provider` returns, because the diagnosis asks
    what the provider *is* by name so a kubeconfig deployment never imports
    grpc.
    """

    def __init__(self) -> None:
        self._executed = []
        self._truncations = []

    @property
    def cluster_id(self) -> str:
        return "api-cut"

    async def fetch(self, request):
        return (await self.fetch_many([request]))[0]

    async def fetch_many(self, requests):
        record = evidence_pb2.EvidenceRecord(
            kind="k8s.pods", status=evidence_pb2.EVIDENCE_STATUS_FAILED, detail=DIAL
        )
        return [self._to_result(record, request) for request in requests]


@pytest.fixture
async def investigation():
    service = InvestigationService(context="api-cut", namespace="payments")
    service.provider = RemoteAgentProvider()
    return await service.run()


async def test_every_record_keeps_the_agents_reason(investigation):
    failed = [
        e for e in investigation["evidence"] if e["status"] not in {"ok", "empty", "not_applicable"}
    ]
    assert failed, "nothing failed: the provider double is not in the path"
    for entry in failed:
        assert "Verify kubeconfig" not in entry["detail"], entry
    assert any("i/o timeout" in entry["detail"] for entry in failed)


async def test_the_investigation_names_the_agents_path_not_a_kubeconfig(investigation):
    message = investigation["health"]["message"]
    assert "could not reach its cluster's API server" in message, message
    assert "i/o timeout" in message
    assert "kubeconfig or your permissions" in message  # says what it is *not*
