"""The agent transport, end to end.

M4's exit criterion is not "the stream works" — it is that an investigation run
through an agent produces the same evidence as the same read performed locally.
Anything less and the two paths have quietly diverged, which is the failure that
would make a fleet diagnosis irreproducible.

**Since M4b this runs over mTLS.** The agent enrols with a single-use bootstrap
token, receives a certificate for a key it generated itself, and every
assertion below is made against a stream whose identity was proved by that
certificate rather than asserted in `hello`. Keeping the differential suite on
the real path is the point: an identity model that only the unit tests exercise
is one the evidence comparison would not notice breaking.

Opt-in, because it needs a real cluster and a built agent binary:

    kind create cluster --name m4b
    (cd agent && go build -o /tmp/k8s-agent ./cmd/agent)
    K8S_AGENT_CLUSTER_INTEGRATION=1 AGENT_BINARY=/tmp/k8s-agent \\
      AGENT_TEST_CONTEXT=kind-m4b python -m pytest tests/test_agent_transport.py

It creates the one workload it compares against, in a namespace of its own, and
removes it afterwards — see `differential_workload`. It used to assume a pod
called `web` already existed, so an otherwise-empty cluster produced three
failures that looked exactly like a divergence.

**Nothing sets `K8S_AGENT_CLUSTER_INTEGRATION`** — not CI, not
`scripts/integration_verify.sh`. This suite runs when a person remembers to run
it, which is the same standing this repository's mutation tests had before
`scripts/mutation_check.py`. Running it is worth it: it is what found the agent
reporting an absent metrics-server as an *empty* result rather than an
unavailable one, which made an uninstalled metrics-server read as an idle
cluster and raised the confidence of a diagnosis that had seen less.
"""

import asyncio
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from app.gateway.identity import AgentIdentityService
from app.gateway.server import AgentGateway
from app.gateway.session import AgentRegistry
from app.providers.base import ReadVerb, ResourceRequest
from app.providers.local_kubectl import LocalKubectlProvider
from app.providers.remote_agent import RemoteAgentProvider
from app.security.ca import CertificateAuthority
from app.security.enrolment import FileEnrolmentStore

ENABLED = os.environ.get("K8S_AGENT_CLUSTER_INTEGRATION") == "1"
BINARY = os.environ.get("AGENT_BINARY", "/tmp/m3run/k8s-agent")
CONTEXT = os.environ.get("AGENT_TEST_CONTEXT", "kind-m4b")
KUBECONFIG = os.environ.get("AGENT_TEST_KUBECONFIG", "")

TRUST_DOMAIN = "integration.local"

requires_cluster = pytest.mark.skipif(
    not ENABLED or not Path(BINARY).exists(),
    reason="Set K8S_AGENT_CLUSTER_INTEGRATION=1 with a kind cluster and a built agent",
)

pytestmark = requires_cluster


async def wait_for_agent(registry: AgentRegistry, process, timeout: float = 20.0):
    for _ in range(int(timeout * 10)):
        session = registry.get(CONTEXT)
        if session is not None:
            return session
        if process.poll() is not None:
            break
        await asyncio.sleep(0.1)

    process.terminate()
    _, stderr = process.communicate(timeout=5)
    pytest.fail(f"The agent never connected: {stderr.decode()[-1500:]}")


# The workload the differential comparison needs, created by the suite rather
# than assumed.
#
# Three tests compare a *specific* object across both providers and look for a
# pod whose name starts with `web`. The module docstring said only
# `kind create cluster`, so a cluster without one produced three failures
# indistinguishable from a real divergence — which is precisely the judgement
# this suite exists to make. It cost a real investigation to separate them the
# first time: two of the three failures that day were a genuine shipped defect
# (the agent reporting an absent metrics-server as an empty result) and the
# third was only this missing fixture.
DIFFERENTIAL_NAMESPACE = "k8s-agent-differential"
DIFFERENTIAL_MANIFEST = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {DIFFERENTIAL_NAMESPACE}
---
apiVersion: v1
kind: Pod
metadata:
  name: web-differential
  namespace: {DIFFERENTIAL_NAMESPACE}
  labels:
    app: web
    differential: "true"
spec:
  containers:
    - name: web
      image: registry.k8s.io/pause:3.9
"""


@pytest.fixture(scope="module", autouse=True)
def differential_workload():
    """Its own namespace, created and removed, so the suite leaves no trace.

    A pod in `default` would linger in whatever cluster someone pointed this
    at; a namespace of its own is deleted whole. `pause` because the comparison
    is about the object's *fields* — image, labels, container spec — and not
    about anything it runs.
    """
    apply = subprocess.run(
        ["kubectl", "--context", CONTEXT, "apply", "-f", "-"],
        input=DIFFERENTIAL_MANIFEST,
        capture_output=True,
        text=True,
    )
    if apply.returncode != 0:
        pytest.skip(f"could not create the differential workload: {apply.stderr[-300:]}")

    subprocess.run(
        [
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            DIFFERENTIAL_NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod/web-differential",
            "--timeout=60s",
        ],
        capture_output=True,
        text=True,
    )
    try:
        yield
    finally:
        subprocess.run(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "delete",
                "namespace",
                DIFFERENTIAL_NAMESPACE,
                "--wait=false",
            ],
            capture_output=True,
            text=True,
        )


@pytest.fixture
async def connected_agent(tmp_path, monkeypatch):
    """A gateway, and a real agent process enrolled and dialled into it over mTLS."""
    from app.core.config import settings

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

    # The CA the agent verifies the gateway with, copied out of band exactly as
    # an operator would.
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(service.ca_bundle_pem)

    token = store.issue_token(CONTEXT)

    environment = {**os.environ}
    if KUBECONFIG:
        environment["KUBECONFIG"] = KUBECONFIG

    process = subprocess.Popen(
        [
            BINARY,
            "--cluster",
            CONTEXT,
            "--gateway",
            f"localhost:{port}",
            "--enrol",
            f"localhost:{gateway.enrolment_port}",
            "--bootstrap-token",
            token,
            "--ca-file",
            str(ca_file),
            "--identity-dir",
            str(tmp_path / "agent-identity"),
            "--once",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        yield await wait_for_agent(registry, process)
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


class TestTheRealAgentProvesWhoItIs:
    """M4b's exit criterion, against the actual binary rather than a stand-in.

    The hermetic suite (`test_agent_mtls.py`) drives the gateway with a Python
    client. That proves the platform's half. This proves the other half: that
    the Go agent generates a key, enrols, and comes back on a stream whose
    identity the certificate established.
    """

    async def test_the_session_identity_came_from_a_certificate(self, connected_agent):
        assert connected_agent.identity.verified
        assert connected_agent.identity.source == "certificate"
        assert connected_agent.certificate_serial
        assert connected_agent.cluster_id == CONTEXT

    async def test_the_agents_private_key_never_reached_the_platform(
        self, connected_agent, tmp_path
    ):
        """The key is on the agent's disk and nowhere else."""
        key_file = tmp_path / "agent-identity" / "agent.key"
        assert key_file.exists(), "the agent did not keep its key"
        assert key_file.stat().st_mode & 0o077 == 0, "the agent's key is world-readable"

        # And the platform holds a certificate for it, not the key itself.
        certificates = [
            record.serial
            for record in FileEnrolmentStore(tmp_path / "enrolment.json").certificates(CONTEXT)
        ]
        assert connected_agent.certificate_serial in certificates

    async def test_the_bootstrap_token_was_spent(self, connected_agent, tmp_path):
        tokens = FileEnrolmentStore(tmp_path / "enrolment.json").tokens(CONTEXT)
        assert tokens and all(token.spent for token in tokens)


class TestEveryCollectorAgrees:
    """M5's exit criterion: the whole collector set, both providers, compared.

    M4a compared four hand-written reads. That proved the transport, not the
    engine — it would not have noticed an inspector whose migration changed what
    it asked for, because no inspector was involved. This runs the actual
    baseline graph twice against one cluster, once through kubectl and once
    through the agent, and compares the evidence each produced.

    Comparison is on the analysed payload rather than the raw objects, because
    the payload is what a diagnosis rests on. Fields that legitimately move
    between two reads of a live cluster (event counts, usage figures) are
    excluded by name rather than by rounding, so an exclusion is visible.
    """

    async def collect_both(self, session):
        """Run the baseline graph through each provider. Returns two stores."""
        from app.collectors.base import CollectionContext, InvestigationScope
        from app.collectors.kubernetes import build_default_collectors
        from app.collectors.registry import CollectorRegistry
        from app.collectors.scheduler import CollectionScheduler
        from app.evidence.store import EvidenceStore

        stores = []
        for provider in (RemoteAgentProvider(session), local_provider()):
            registry = CollectorRegistry()
            for collector in build_default_collectors():
                registry.register(collector)

            context = CollectionContext(
                scope=InvestigationScope(context=CONTEXT),
                provider=provider,
                store=EvidenceStore(),
            )
            stores.append(await CollectionScheduler(registry).run(context))

        return stores[0], stores[1]

    # Values that change between two reads of a running cluster, or that name
    # the transport rather than the finding.
    VOLATILE: ClassVar[set[str]] = {
        "total_events",
        "findings",  # events findings carry live messages; compared separately
        "command",
        "checked_pods",
        "logs",
        "records",
        "items",
    }

    async def test_the_baseline_graph_produces_the_same_evidence(self, connected_agent):
        remote, local = await self.collect_both(connected_agent)

        remote_kinds = {record.kind for record in remote}
        local_kinds = {record.kind for record in local}
        assert remote_kinds == local_kinds, "the two providers produced different evidence kinds"

        # Nothing may be usable on one path and not the other: that is the
        # failure mode a differential test exists to catch.
        for kind in sorted(local_kinds):
            remote_usable = [record.usable for record in remote.by_kind(kind)]
            local_usable = [record.usable for record in local.by_kind(kind)]
            assert remote_usable == local_usable, f"{kind} degraded on only one path"

    @pytest.mark.parametrize(
        "kind",
        ["k8s.pods", "k8s.deployments", "k8s.nodes", "k8s.network", "k8s.storage", "k8s.workloads"],
    )
    async def test_each_inspector_reaches_the_same_conclusion(self, connected_agent, kind):
        remote, local = await self.collect_both(connected_agent)

        remote_payload = remote.data(kind, {}) or {}
        local_payload = local.data(kind, {}) or {}

        assert set(remote_payload) == set(local_payload), f"{kind} payload keys differ"

        for key in sorted(set(local_payload) - self.VOLATILE):
            assert remote_payload[key] == local_payload[key], f"{kind}.{key} differs"

    async def test_the_pod_inventory_is_identical(self, connected_agent):
        """The most consequential payload: what the analysis layer reasons over."""
        remote, local = await self.collect_both(connected_agent)

        def by_name(payload):
            return {
                f"{pod['namespace']}/{pod['name']}": pod
                for pod in (payload or {}).get("pod_inventory", [])
            }

        remote_pods = by_name(remote.data("k8s.pods", {}))
        local_pods = by_name(local.data("k8s.pods", {}))

        assert set(remote_pods) == set(local_pods)
        for name in sorted(local_pods):
            assert remote_pods[name] == local_pods[name], f"{name} differs between providers"

    async def test_problematic_pods_match(self, connected_agent):
        remote, local = await self.collect_both(connected_agent)

        def flagged(payload):
            return sorted(
                (pod["namespace"], pod["name"], pod["status"])
                for pod in (payload or {}).get("problematic_pods", [])
            )

        assert flagged(remote.data("k8s.pods", {})) == flagged(local.data("k8s.pods", {}))

    async def test_pod_logs_are_collected_through_both(self, connected_agent):
        """`LogsCollector` fans out over pods, so it is the odd one out."""
        remote, local = await self.collect_both(connected_agent)

        remote_logs = remote.data("k8s.pods.logs", {}) or {}
        local_logs = local.data("k8s.pods.logs", {}) or {}

        assert remote_logs.get("checked_pods") == local_logs.get("checked_pods")
        assert [entry["name"] for entry in remote_logs.get("logs", [])] == [
            entry["name"] for entry in local_logs.get("logs", [])
        ]

    async def test_metrics_agree_or_are_absent_on_both(self, connected_agent):
        """The only collector where the two sources genuinely differ.

        kubectl reads `top`, the agent reads metrics.k8s.io. Usage is measured
        and the percentage derived, so the node *set* must match either way —
        and if metrics-server is not installed, both must say so rather than
        one reporting an idle cluster.
        """
        remote, local = await self.collect_both(connected_agent)

        remote_record = remote.by_kind("k8s.metrics.nodes")[0]
        local_record = local.by_kind("k8s.metrics.nodes")[0]
        assert remote_record.usable == local_record.usable

        if not local_record.usable:
            pytest.skip("metrics-server is not installed in this cluster")

        remote_rows = (remote_record.data or {})["records"]
        local_rows = (local_record.data or {})["records"]
        assert sorted(row["name"] for row in remote_rows) == sorted(
            row["name"] for row in local_rows
        )

        # Usage itself cannot be compared — CPU moves between two reads of a
        # live cluster — so what is checked is that each path's percentage is
        # *derived from its own measurement*, rather than transported. A
        # collector that started trusting kubectl's percentage column would
        # produce a figure inconsistent with the usage printed beside it.
        from app.kubernetes import metrics as metrics_module

        allocatable = metrics_module.allocatable_by_node(
            (local.data("k8s.nodes.raw", {}) or {}).get("items", [])
        )
        for rows in (remote_rows, local_rows):
            for row in rows:
                capacity = allocatable.get(row["name"], {})
                expected = metrics_module.percent(
                    metrics_module.parse_cpu(row["cpu"]), capacity.get("cpu_cores")
                )
                assert row["cpu_percent"] == (f"{expected}%" if expected is not None else "N/A"), (
                    f"{row['name']} reports {row['cpu_percent']} for {row['cpu']} of "
                    f"{capacity.get('cpu_cores')} cores — the percentage was not derived "
                    f"from the usage beside it"
                )

    async def test_no_collector_needed_an_executor(self, connected_agent):
        """The migration's whole point, asserted rather than assumed.

        Before M5 every inspector reached `raw_executor()`, which a remote
        provider refused — so this graph could not have run at all against an
        agent. It runs now, and there is no hatch left to fall back through.
        """
        from app.providers.remote_agent import RemoteAgentProvider as Remote

        assert not hasattr(Remote, "raw_executor")

        remote, _ = await self.collect_both(connected_agent)
        usable = [record for record in remote if record.usable]
        assert len(usable) >= 8, f"only {len(usable)} usable records through the agent"


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


class TestRotationDoesNotInterruptAnything:
    """Rotation observed, not asserted.

    The Go tests pin the arithmetic and the hermetic suite pins the renewal
    RPC. Neither would notice the failure that actually matters at fleet scale:
    a renewal that drops the stream, and with it every collection in flight
    across a thousand clusters at the same moment. That is what this checks —
    a real agent, a real renewal, and the *same session object* still serving
    reads afterwards.

    What it deliberately does **not** check is the two-thirds timing. The CA
    backdates `NotBefore` by five minutes for clock skew, which against a
    thirty-second certificate puts the renewal point already in the past, so
    the agent renews on its first check. That is the right trade for a test of
    the mechanism — it takes seconds instead of minutes — and the timing itself
    is pinned by `TestRenewalHappensAtTwoThirdsOfLife` in the Go suite, where
    it can be exercised against an arbitrary clock rather than a real one.
    """

    @pytest.fixture
    async def rotating_agent(self, tmp_path, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "agent_gateway_dns_names", "localhost")
        monkeypatch.setattr(settings, "agent_gateway_ip_addresses", "127.0.0.1")

        authority = CertificateAuthority.create(TRUST_DOMAIN)
        store = FileEnrolmentStore(tmp_path / "enrolment.json")
        service = AgentIdentityService(authority, store, leaf_lifetime=timedelta(seconds=30))

        registry = AgentRegistry()
        gateway = AgentGateway(
            port=0, registry=registry, enrolment_port=0, identity_service=service, mtls=True
        )
        port = await gateway.start()

        ca_file = tmp_path / "ca.crt"
        ca_file.write_bytes(service.ca_bundle_pem)

        process = subprocess.Popen(
            [
                BINARY,
                "--cluster",
                CONTEXT,
                "--gateway",
                f"localhost:{port}",
                "--enrol",
                f"localhost:{gateway.enrolment_port}",
                "--bootstrap-token",
                store.issue_token(CONTEXT),
                "--ca-file",
                str(ca_file),
                "--identity-dir",
                str(tmp_path / "agent-identity"),
                "--renewal-check",
                "1s",
                "--once",
            ],
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            session = await wait_for_agent(registry, process)
            yield session, store, registry
        finally:
            process.terminate()
            process.wait(timeout=5)
            await gateway.stop()

    async def test_the_agent_renews_itself_without_dropping_the_stream(self, rotating_agent):
        session, store, registry = rotating_agent
        original_serial = session.certificate_serial

        # Renewal is due at 20s of a 30s life; allow for the check interval.
        for _ in range(300):
            if len(store.certificates(CONTEXT)) > 1:
                break
            await asyncio.sleep(0.1)

        serials = [record.serial for record in store.certificates(CONTEXT)]
        assert len(serials) > 1, "the agent never renewed its certificate"
        assert original_serial in serials

        # The stream is the same one. Not "a stream exists" — the *same object*,
        # which is what proves nothing reconnected.
        assert registry.get(CONTEXT) is session
        assert session.certificate_serial == original_serial

        # And it still works, on the old certificate, after the new one exists.
        result = await RemoteAgentProvider(session).fetch(REQUESTS[0])
        assert result.success, result.error

    async def test_the_old_certificate_is_not_revoked_by_renewing(self, rotating_agent):
        """The overlap window is the whole reason renewal is safe."""
        _, store, _ = rotating_agent

        for _ in range(300):
            if len(store.certificates(CONTEXT)) > 1:
                break
            await asyncio.sleep(0.1)

        assert store.revoked_serials() == set()
        assert not any(record.revoked for record in store.certificates(CONTEXT))


class TestThePlaintextPathIsStillThere:
    """The development opt-in, kept honest by being tested.

    `AGENT_GATEWAY_TLS=disabled` is a supported mode, not a dead branch, and the
    same discipline applies to it as to the single-process job store: it has to
    keep working, and it has to keep being something you choose rather than
    something you end up in.
    """

    @pytest.fixture
    async def plaintext_agent(self, tmp_path):
        registry = AgentRegistry()
        gateway = AgentGateway(port=0, registry=registry, mtls=False)
        port = await gateway.start()

        environment = {**os.environ}
        if KUBECONFIG:
            environment["KUBECONFIG"] = KUBECONFIG

        process = subprocess.Popen(
            [
                BINARY,
                "--cluster",
                CONTEXT,
                "--gateway",
                f"127.0.0.1:{port}",
                "--insecure",
                "--once",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            yield await wait_for_agent(registry, process)
        finally:
            process.terminate()
            process.wait(timeout=5)
            await gateway.stop()

    async def test_it_connects_and_collects(self, plaintext_agent):
        result = await RemoteAgentProvider(plaintext_agent).fetch(REQUESTS[0])
        assert result.success, result.error
        assert result.data.get("kind") == "PodList"

    async def test_its_identity_is_marked_unverified(self, plaintext_agent):
        """The whole difference between the two modes, made visible.

        The cluster id here came from `hello`. Nothing proved it, the session
        says so, and `/clusters` reports it — so a deployment that left this
        mode on cannot look like one that did not.
        """
        assert not plaintext_agent.identity.verified
        assert plaintext_agent.identity.source == "declared"
        assert plaintext_agent.certificate_serial == ""
        assert plaintext_agent.describe()["identity_source"] == "declared"

    async def test_an_mtls_gateway_refuses_a_plaintext_agent(self, tmp_path):
        """Choosing the wrong mode fails to connect; it does not downgrade."""
        authority = CertificateAuthority.create(TRUST_DOMAIN)
        service = AgentIdentityService(
            authority,
            FileEnrolmentStore(tmp_path / "enrolment.json"),
            leaf_lifetime=timedelta(days=90),
        )
        registry = AgentRegistry()
        gateway = AgentGateway(
            port=0, registry=registry, enrolment_port=0, identity_service=service, mtls=True
        )
        port = await gateway.start()

        process = subprocess.Popen(
            [
                BINARY,
                "--cluster",
                CONTEXT,
                "--gateway",
                f"127.0.0.1:{port}",
                "--insecure",
                "--once",
            ],
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            await asyncio.sleep(5)
            assert registry.get(CONTEXT) is None, "a plaintext agent reached an mTLS gateway"
        finally:
            process.terminate()
            process.wait(timeout=5)
            await gateway.stop()
