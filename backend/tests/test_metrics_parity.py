"""`kubectl top` and metrics.k8s.io must describe the same cluster identically.

This is the one collector where the two providers genuinely see different
things: kubectl prints formatted text with a percentage it computed itself,
and the metrics API returns quantities and nothing else. Everything else in the
collector set reads the same JSON either way.

The rule these tests hold in place: **usage is measured, percentage is
derived** — on the platform, for both providers, from node allocatable
capacity. The alternative was teaching the Go agent to reproduce kubectl's
column layout, which would put a formatting contract in a binary shipped to a
thousand clusters.

Hermetic; the payloads are what the two sources actually return.
"""

from typing import ClassVar

import pytest

from app.collectors.kubernetes import ResourceMetricsCollector
from app.kubernetes import metrics
from app.providers.base import ProviderResult

# What `kubectl top nodes --no-headers` prints.
TOP_NODES_TEXT = "node-a   250m   6%    2048Mi   26%\nnode-b   1500m  39%   4096Mi   53%\n"

# What `kubectl top pods -A --no-headers` prints.
TOP_PODS_TEXT = "prod   web-0   120m   512Mi\nprod   api-7   35m    128Mi\n"

# What the agent reads from metrics.k8s.io/v1beta1/nodes — the same cluster.
NODE_METRICS_API = {
    "kind": "NodeMetricsList",
    "items": [
        {"metadata": {"name": "node-a"}, "usage": {"cpu": "250m", "memory": "2097152Ki"}},
        {"metadata": {"name": "node-b"}, "usage": {"cpu": "1500m", "memory": "4194304Ki"}},
    ],
}

# ...and from .../pods. Note `web-0` has two containers: kubectl reports one row
# per pod, so the agent's per-container figures have to be summed.
POD_METRICS_API = {
    "kind": "PodMetricsList",
    "items": [
        {
            "metadata": {"name": "web-0", "namespace": "prod"},
            "containers": [
                {"name": "app", "usage": {"cpu": "100m", "memory": "384Mi"}},
                {"name": "sidecar", "usage": {"cpu": "20m", "memory": "128Mi"}},
            ],
        },
        {
            "metadata": {"name": "api-7", "namespace": "prod"},
            "containers": [{"name": "api", "usage": {"cpu": "35m", "memory": "128Mi"}}],
        },
    ],
}

NODES = [
    {
        "metadata": {"name": "node-a"},
        "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}},
    },
    {
        "metadata": {"name": "node-b"},
        "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}},
    },
]


def text_result(text: str) -> ProviderResult:
    """What `LocalKubectlProvider` returns for a `top` read."""
    return ProviderResult(success=True, text=text, equivalent_command="kubectl top nodes")


def json_result(payload: dict) -> ProviderResult:
    """What `RemoteAgentProvider` returns for the same read."""
    return ProviderResult(success=True, data=payload, equivalent_command="kubectl top nodes")


class TestQuantities:
    @pytest.mark.parametrize(
        ("value", "cores"),
        [("250m", 0.25), ("1", 1.0), ("120500000n", 0.1205), ("1500u", 0.0015), ("0", 0.0)],
    )
    def test_cpu_quantities(self, value, cores):
        assert metrics.parse_cpu(value) == pytest.approx(cores)

    @pytest.mark.parametrize(
        ("value", "byte_count"),
        [
            ("1443Mi", 1513095168),
            ("2097152Ki", 2147483648),
            ("8Gi", 8589934592),
            ("1000k", 1000000),
            ("512", 512),
        ],
    )
    def test_memory_quantities(self, value, byte_count):
        assert metrics.parse_memory(value) == byte_count

    @pytest.mark.parametrize("value", ["", "abc", "12Qi", "--"])
    def test_an_unparseable_quantity_is_none_rather_than_zero(self, value):
        # Zero would read as "measured, and idle"; None reads as "not measured".
        assert metrics.parse_cpu(value) is None
        assert metrics.parse_memory(value) is None


class TestBothSourcesAgree:
    """The parity property, at the only place the sources differ."""

    def test_node_usage_matches(self):
        from_text = metrics.node_usage_from_text(TOP_NODES_TEXT.splitlines())
        from_api = metrics.node_usage_from_api(NODE_METRICS_API)

        assert from_text == from_api

    def test_pod_usage_matches_with_containers_summed(self):
        from_text = metrics.pod_usage_from_text(TOP_PODS_TEXT.splitlines())
        from_api = metrics.pod_usage_from_api(POD_METRICS_API)

        assert from_text == from_api
        # The sum is the point: 100m + 20m, 384Mi + 128Mi.
        assert from_api[0]["cpu_cores"] == pytest.approx(0.12)

    async def test_the_collector_emits_identical_evidence_either_way(self):
        collector = ResourceMetricsCollector()
        allocatable = metrics.allocatable_by_node(NODES)

        local_nodes = collector._nodes(text_result(TOP_NODES_TEXT), allocatable)
        remote_nodes = collector._nodes(json_result(NODE_METRICS_API), allocatable)
        assert local_nodes == remote_nodes

        local_pods = collector._pods(text_result(TOP_PODS_TEXT))
        remote_pods = collector._pods(json_result(POD_METRICS_API))
        assert local_pods == remote_pods


class TestPercentagesAreDerived:
    def test_kubectls_own_percentage_column_is_not_used(self):
        """`kubectl top` said 6% and 39%; allocatable says otherwise.

        250m of 4 cores is 6% and 1500m of 4 is 38% — close, because kubectl
        divides by the same allocatable. The test asserts the *derived* value,
        so a future change that started trusting the transported column would
        have to disagree with this on the second node.
        """
        collector = ResourceMetricsCollector()
        rows = collector._nodes(text_result(TOP_NODES_TEXT), metrics.allocatable_by_node(NODES))

        assert rows[0]["cpu_percent"] == "6%"
        assert rows[1]["cpu_percent"] == "38%"
        assert rows[0]["memory_percent"] == "25%"

    def test_without_node_capacity_the_percentage_is_absent_not_zero(self):
        """A missing ratio must not read as an idle cluster."""
        collector = ResourceMetricsCollector()
        rows = collector._nodes(text_result(TOP_NODES_TEXT), {})

        assert rows[0]["cpu_percent"] == "N/A"
        assert rows[0]["cpu_percent_value"] is None
        # Usage itself was still measured and is still reported.
        assert rows[0]["cpu"] == "250m"

    def test_usage_is_formatted_the_way_the_console_has_always_shown_it(self):
        collector = ResourceMetricsCollector()
        rows = collector._nodes(json_result(NODE_METRICS_API), metrics.allocatable_by_node(NODES))

        assert rows[0]["cpu"] == "250m"
        assert rows[0]["memory"] == "2048Mi"


class TestAnAbsentMetricsServerLooksAbsentOnBothPaths:
    """The divergence a real cluster found, pinned from this side of the wire.

    metrics-server is absent from most clusters. kubectl's `top` fails, the
    collector records `UNAVAILABLE`, and a diagnosis says metrics were not
    consulted. The agent 404s on `apis/metrics.k8s.io/v1beta1/nodes` — and
    mapped *every* 404 to `EMPTY`, which the platform counts as **usable**. So
    the same cluster, read the same moment, was "we could not look" through a
    kubeconfig and "we looked and there is no usage" through an agent.

    That is the rule `docs/OBSERVABILITY_INTEGRATIONS.md` states outright:
    missing metrics must never read as healthy metrics. It also inflates
    `evidence_coverage.completeness`, which feeds the confidence score — so the
    agent path reported *more* confidence for having seen *less*.

    Fixed in the agent (`statusFor` now distinguishes a named read from a list
    read: a 404 on a list means the API is not served here). These tests hold
    the platform half of the coupling, which is what makes that Go change
    load-bearing rather than cosmetic.
    """

    def _evidence_for(self, result: ProviderResult):
        import asyncio

        from app.collectors.base import CollectionContext, InvestigationScope

        class Fixed:
            cluster_id = "prod"
            executed_commands: ClassVar[list] = []
            truncations: ClassVar[list] = []

            async def fetch(self, request):
                return result

            async def fetch_many(self, requests):
                return [result for _ in requests]

        context = CollectionContext(scope=InvestigationScope(context="prod"), provider=Fixed())
        records = asyncio.run(ResourceMetricsCollector().collect(context))
        return {record.kind: record for record in records}

    def test_a_failed_top_is_unavailable_not_empty(self):
        records = self._evidence_for(
            ProviderResult(success=False, error="Metrics API not available")
        )
        assert not records["k8s.metrics.nodes"].usable
        assert str(records["k8s.metrics.nodes"].status) == "unavailable"

    def test_empty_is_a_usable_status_which_is_why_the_agent_mapping_matters(self):
        """The load-bearing half. If `EMPTY` ever stopped being usable this
        coupling would quietly stop mattering, and the Go fix would look like a
        cosmetic status rename to whoever read it next."""
        from app.evidence.models import EvidenceStatus

        assert EvidenceStatus.EMPTY.usable
        assert not EvidenceStatus.UNAVAILABLE.usable

    def test_the_agent_status_for_a_missing_api_is_not_treated_as_a_read(self):
        """`RemoteAgentProvider` must turn an unavailable record into a failed
        result, so the collector takes the same branch kubectl's failure does."""
        from app.providers.remote_agent import _USABLE
        from app.wire.gen.agent.v1 import evidence_pb2

        assert evidence_pb2.EVIDENCE_STATUS_UNAVAILABLE not in _USABLE
        assert evidence_pb2.EVIDENCE_STATUS_EMPTY in _USABLE

    def test_the_go_agent_still_makes_the_distinction(self):
        """Read from the Go source, because the mapping lives there and a
        Python-only assertion would pass with the defect restored.

        Not a substitute for `internal/collectors/status_test.go` — a
        tripwire, so someone editing the Python side sees the coupling.
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "agent/internal/collectors/collector.go"
        ).read_text()
        assert "apierrors.IsNotFound(err) && named:" in source, (
            "the agent no longer distinguishes a 404 on a named read from one on a "
            "list read, so an absent metrics-server reads as an idle cluster again"
        )
