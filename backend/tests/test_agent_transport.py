"""The agent transport, end to end.

M4's exit criterion is not "the stream works" — it is that an investigation run
through an agent produces the same evidence as the same read performed locally.
Anything less and the two paths have quietly diverged, which is the failure that
would make a fleet diagnosis irreproducible.

Opt-in, because it needs a real cluster and a built agent binary:

    kind create cluster --name m4
    (cd agent && go build -o /tmp/k8s-agent ./cmd/agent)
    K8S_AGENT_CLUSTER_INTEGRATION=1 AGENT_BINARY=/tmp/k8s-agent \\
      AGENT_TEST_CONTEXT=kind-m4 python -m pytest tests/test_agent_transport.py
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.gateway.server import AgentGateway
from app.gateway.session import AgentRegistry
from app.providers.base import ReadVerb, ResourceRequest
from app.providers.local_kubectl import LocalKubectlProvider
from app.providers.remote_agent import RemoteAgentProvider

ENABLED = os.environ.get("K8S_AGENT_CLUSTER_INTEGRATION") == "1"
BINARY = os.environ.get("AGENT_BINARY", "/tmp/m3run/k8s-agent")
CONTEXT = os.environ.get("AGENT_TEST_CONTEXT", "kind-m4")
KUBECONFIG = os.environ.get("AGENT_TEST_KUBECONFIG", "")

requires_cluster = pytest.mark.skipif(
    not ENABLED or not Path(BINARY).exists(),
    reason="Set K8S_AGENT_CLUSTER_INTEGRATION=1 with a kind cluster and a built agent",
)

pytestmark = requires_cluster


@pytest.fixture
async def connected_agent():
    """A gateway, and a real agent process dialled into it."""
    registry = AgentRegistry()
    gateway = AgentGateway(port=0, registry=registry)
    port = await gateway.start()

    environment = {**os.environ, "AGENT_GATEWAY": f"127.0.0.1:{port}"}
    if KUBECONFIG:
        environment["KUBECONFIG"] = KUBECONFIG

    process = subprocess.Popen(
        [BINARY, "--cluster", CONTEXT, "--gateway", f"127.0.0.1:{port}", "--once"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        session = None
        for _ in range(100):
            session = registry.get(CONTEXT)
            if session is not None:
                break
            await asyncio.sleep(0.1)

        if session is None:
            process.terminate()
            _, stderr = process.communicate(timeout=5)
            pytest.fail(f"The agent never connected: {stderr.decode()[:400]}")

        yield session
    finally:
        process.terminate()
        process.wait(timeout=5)
        await gateway.stop()


def local_provider() -> LocalKubectlProvider:
    return LocalKubectlProvider(context=CONTEXT)


REQUESTS = [
    ResourceRequest(verb=ReadVerb.GET, resource="pods", all_namespaces=True),
    ResourceRequest(verb=ReadVerb.GET, resource="nodes"),
    ResourceRequest(verb=ReadVerb.GET, resource="deployments", all_namespaces=True),
    ResourceRequest(verb=ReadVerb.GET, resource="events", namespace="default"),
]


class TestTheAgentAnswers:
    async def test_it_says_what_it_can_collect(self, connected_agent):
        # The platform plans against this rather than assuming a uniform fleet.
        assert "k8s.pods" in connected_agent.supported_kinds
        assert "k8s.logs" in connected_agent.supported_kinds

    async def test_it_reports_the_cluster_it_is_in(self, connected_agent):
        assert connected_agent.hello.kubernetes_version.startswith("v")

    async def test_a_read_comes_back_as_evidence(self, connected_agent):
        provider = RemoteAgentProvider(connected_agent)
        result = await provider.fetch(REQUESTS[0])

        assert result.success, result.error
        assert isinstance(result.data, dict)
        assert result.data.get("kind") == "PodList"


class TestBothPathsAgree:
    """The exit criterion: remote evidence matches local evidence."""

    @pytest.mark.parametrize("request_index", range(len(REQUESTS)))
    async def test_the_same_read_returns_the_same_objects(self, connected_agent, request_index):
        request = REQUESTS[request_index]
        remote = await RemoteAgentProvider(connected_agent).fetch(request)
        local = await local_provider().fetch(request)

        assert local.success, local.error
        assert remote.success, remote.error

        # Compared on the objects, not the envelope. Two reasons, both found
        # by running this rather than by reasoning about it:
        #
        #  - kubectl rewrites every list envelope to a generic `List`, while
        #    the API server returns the typed one (`PodList`). The agent reads
        #    the API directly, so it is the *more* faithful of the two. See
        #    `test_the_envelope_differs_and_that_is_kubectl`.
        #  - a list read carries a resourceVersion that advances between two
        #    calls, so asserting on it would be asserting the cluster stood
        #    still between them.
        #
        # What has to match is what the engine consumes and what a diagnosis
        # ends up resting on: the objects.
        assert names(remote.data) == names(local.data)
        assert len(remote.data["items"]) == len(local.data["items"])

    async def test_the_envelope_differs_and_that_is_kubectl(self, connected_agent):
        """The one thing that does not match, asserted so it stays known.

        M4's exit criterion was written as "byte-identical evidence to the
        local path". It cannot be met while the local path is kubectl, because
        kubectl replaces the API server's typed list envelope with a generic
        one. Nothing downstream reads the envelope — every collector works from
        `items` — but a stored remote record does differ from a stored local
        one in this field, and that is worth knowing rather than discovering.
        """
        request = REQUESTS[0]
        remote = await RemoteAgentProvider(connected_agent).fetch(request)
        local = await local_provider().fetch(request)

        assert remote.data["kind"] == "PodList", "the agent reports what the API returned"
        assert local.data["kind"] == "List", "kubectl rewrites the envelope"

    async def test_the_audit_trail_is_the_same_command(self, connected_agent):
        request = REQUESTS[0]
        remote = await RemoteAgentProvider(connected_agent).fetch(request)
        local = await local_provider().fetch(request)

        # The agent never runs kubectl, but it records what would have. An
        # operator reproducing a remote read must get the same command.
        assert "get pods" in remote.equivalent_command
        assert "-A" in remote.equivalent_command
        assert "get pods" in local.equivalent_command

    async def test_object_contents_survive_the_round_trip(self, connected_agent):
        """Raw reads, so nothing the agent's schema does not know is dropped."""
        request = REQUESTS[0]
        remote = await RemoteAgentProvider(connected_agent).fetch(request)
        local = await local_provider().fetch(request)

        remote_pod = pod_named(remote.data, "web")
        local_pod = pod_named(local.data, "web")
        assert remote_pod is not None and local_pod is not None

        assert (
            remote_pod["spec"]["containers"][0]["image"]
            == (local_pod["spec"]["containers"][0]["image"])
        )
        assert sorted(remote_pod["metadata"]["labels"]) == sorted(local_pod["metadata"]["labels"])

    async def test_a_failing_pod_looks_the_same_from_both(self, connected_agent):
        """The evidence that matters is the evidence about what is broken."""
        request = REQUESTS[0]
        remote = await RemoteAgentProvider(connected_agent).fetch(request)
        local = await local_provider().fetch(request)

        assert waiting_reasons(remote.data) == waiting_reasons(local.data)


class TestTheAgentRefusesWhatItDoesNotKnow:
    async def test_an_unknown_kind_is_refused_rather_than_interpreted(self, connected_agent):
        # The security property: the platform names a kind the agent already
        # knows. It cannot describe an operation.
        from app.wire.gen.agent.v1 import collection_pb2, evidence_pb2

        spec = collection_pb2.EvidenceSpec(
            kind="k8s.secrets.values",
            target=evidence_pb2.ResourceRef(kind="secrets", name="anything"),
        )
        pending = await connected_agent.collect([spec])

        assert len(pending.records) == 1
        record = pending.records[0]
        assert record.status == evidence_pb2.EVIDENCE_STATUS_NOT_APPLICABLE
        assert "unknown evidence kind" in record.detail

    async def test_the_refusal_carries_no_command(self, connected_agent):
        from app.wire.gen.agent.v1 import collection_pb2, evidence_pb2

        spec = collection_pb2.EvidenceSpec(
            kind="k8s.exec",
            target=evidence_pb2.ResourceRef(kind="pods", name="web"),
        )
        pending = await connected_agent.collect([spec])
        assert not pending.records[0].equivalent_command


def names(payload) -> list[str]:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return sorted(
        f"{item['metadata'].get('namespace', '')}/{item['metadata']['name']}" for item in items
    )


def pod_named(payload, prefix: str):
    for item in payload.get("items", []):
        if item["metadata"]["name"].startswith(prefix):
            return item
    return None


def waiting_reasons(payload) -> list[str]:
    reasons = []
    for item in payload.get("items", []):
        for status in item.get("status", {}).get("containerStatuses", []):
            reason = status.get("state", {}).get("waiting", {}).get("reason")
            if reason:
                reasons.append(f"{item['metadata']['name']}:{reason}")
    return sorted(reasons)


def test_module_imports_without_a_cluster():
    """The suite must stay collectable when the opt-in is off."""
    assert json is not None


class TestAnInvestigationRunsThroughTheAgent:
    """The milestone's claim, not just the transport's.

    M1 predicted that swapping how a cluster is reached would be a substitution
    at one field rather than a refactor of the engine. This is where that is
    either true or it is not: the same collector graph, the same analysis, with
    nothing between it and the cluster but an agent on the far end of a socket.
    """

    async def test_the_engine_collects_through_it_unchanged(self, connected_agent):
        from app.collectors.base import CollectionContext, InvestigationScope
        from app.collectors.kubernetes import RawNodesCollector

        provider = RemoteAgentProvider(connected_agent)
        context = CollectionContext(
            scope=InvestigationScope(context=CONTEXT),
            provider=provider,
        )

        # A collector that already speaks ResourceRequest, run verbatim.
        evidence = await RawNodesCollector().collect(context)

        assert len(evidence) == 1
        assert evidence[0].status.usable, evidence[0].detail
        assert evidence[0].data["items"], "the agent returned no nodes"

    async def test_the_audit_trail_records_the_remote_reads(self, connected_agent):
        provider = RemoteAgentProvider(connected_agent)
        await provider.fetch(REQUESTS[0])

        # The agent never runs kubectl. It records what would have produced the
        # same bytes, so a human can reproduce a remote read by hand.
        assert any("kubectl get pods" in command for command in provider.executed_commands)
