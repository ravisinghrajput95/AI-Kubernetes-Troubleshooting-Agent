"""Prometheus and Loki integration.

Clients are exercised through httpx's MockTransport, so the real request
building and response parsing run — only the network is faked.
"""

import httpx
import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from app.collectors.base import CollectionContext, InvestigationScope
from app.collectors.observability import (
    NodeMetricsCollector,
    PodLogSearchCollector,
    PodMetricsCollector,
)
from app.evidence.models import EvidenceStatus, ResourceRef
from app.integrations.loki import LokiClient
from app.integrations.prometheus import PrometheusClient

POD = ResourceRef(kind="Pod", name="web-0", namespace="prod")
NODE = ResourceRef(kind="Node", name="node-1")


CONFIGURED_PROMETHEUS = "http://prometheus:9090"
CONFIGURED_LOKI = "http://loki:3100"


@pytest.fixture
def promql_responses(monkeypatch):
    """Route every Prometheus request through a MockTransport."""
    captured: list[str] = []

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
        return captured

    return install


def promql_handler(value="123", status_code=200, payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if payload is not None:
            return httpx.Response(status_code, json=payload)
        return httpx.Response(
            status_code,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"pod": "web-0"}, "value": [1700000000, value]}],
                },
            },
        )

    return handler


class TestPrometheusClient:
    async def test_unconfigured_is_not_applicable_not_an_error(self):
        result = await PrometheusClient(base_url="").query("up")

        assert result.status is EvidenceStatus.NOT_APPLICABLE
        assert "not configured" in result.detail

    async def test_parses_an_instant_vector(self, promql_responses):
        promql_responses(promql_handler(value="42"))
        result = await PrometheusClient(base_url="http://prom:9090").query("up")

        assert result.status is EvidenceStatus.OK
        assert result.scalar() == 42.0
        assert result.samples[0]["labels"]["pod"] == "web-0"

    async def test_empty_result_is_empty_not_missing(self, promql_responses):
        promql_responses(promql_handler(payload={"status": "success", "data": {"result": []}}))
        result = await PrometheusClient(base_url="http://prom:9090").query("up")

        assert result.status is EvidenceStatus.EMPTY
        assert result.usable is True

    async def test_unreachable_backend_is_unavailable(self, promql_responses):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        promql_responses(handler)
        result = await PrometheusClient(base_url="http://prom:9090").query("up")

        assert result.status is EvidenceStatus.UNAVAILABLE
        assert "unreachable" in result.detail

    async def test_timeout_is_reported_as_timeout(self, promql_responses):
        def handler(request):
            raise httpx.ReadTimeout("too slow")

        promql_responses(handler)
        result = await PrometheusClient(base_url="http://prom:9090").query("up")

        assert result.status is EvidenceStatus.TIMEOUT

    async def test_query_error_is_reported_as_failed(self, promql_responses):
        promql_responses(promql_handler(payload={"status": "error", "error": "bad query"}))
        result = await PrometheusClient(base_url="http://prom:9090").query("nonsense{")

        assert result.status is EvidenceStatus.FAILED
        assert "bad query" in result.detail


class TestLokiClient:
    async def test_unconfigured_is_not_applicable(self):
        result = await LokiClient(base_url="").query_range('{app="web"}')
        assert result.status is EvidenceStatus.NOT_APPLICABLE

    async def test_parses_and_orders_entries(self, promql_responses):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "stream": {"pod": "web-0"},
                                "values": [
                                    ["1700000000000000000", "older error"],
                                    ["1700000600000000000", "newer error"],
                                ],
                            }
                        ]
                    },
                },
            )

        promql_responses(handler)
        result = await LokiClient(base_url="http://loki:3100").query_range('{app="web"}')

        assert result.status is EvidenceStatus.OK
        assert result.entries[0]["line"] == "newer error"
        assert result.entries[0]["timestamp"].startswith("2023-")

    async def test_unreachable_backend_is_unavailable(self, promql_responses):
        def handler(request):
            raise httpx.ConnectError("refused")

        promql_responses(handler)
        result = await LokiClient(base_url="http://loki:3100").query_range("{}")

        assert result.status is EvidenceStatus.UNAVAILABLE


def context():
    return CollectionContext(scope=InvestigationScope(context="test"), kubectl=None)


class TestCollectors:
    async def test_pod_metrics_derives_utilisation_against_the_limit(self, promql_responses):
        values = {
            "container_memory_working_set_bytes": "900",
            "container_spec_memory_limit_bytes": "1000",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("query", "")
            value = "0"
            for metric, sample in values.items():
                if metric in query:
                    value = sample
                    break
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"result": [{"metric": {}, "value": [1, value]}]},
                },
            )

        promql_responses(handler)
        evidence = await PodMetricsCollector(
            POD, PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())
        data = evidence[0].data

        assert evidence[0].status is EvidenceStatus.OK
        assert evidence[0].source == "prometheus"
        # max_over_time also matches the working-set metric, so peak resolves too.
        assert data["memory_utilisation_percent"] == 90.0

    async def test_pod_metrics_records_why_when_prometheus_is_absent(self):
        evidence = await PodMetricsCollector(POD, PrometheusClient(base_url="")).collect(context())

        assert evidence[0].status is EvidenceStatus.NOT_APPLICABLE
        assert "PROMETHEUS_URL" in evidence[0].detail

    async def test_node_metrics_derives_commitment(self, promql_responses):
        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("query", "")
            value = "100" if "allocatable" in query else "95"
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"result": [{"metric": {}, "value": [1, value]}]},
                },
            )

        promql_responses(handler)
        evidence = await NodeMetricsCollector(
            NODE, PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())

        assert evidence[0].data["memory_committed_percent"] == 95.0

    async def test_log_search_records_why_when_loki_is_absent(self):
        evidence = await PodLogSearchCollector(POD, LokiClient(base_url="")).collect(context())

        assert evidence[0].status is EvidenceStatus.NOT_APPLICABLE
        assert "LOKI_URL" in evidence[0].detail

    async def test_label_values_are_escaped(self, promql_responses):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("query", ""))
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        promql_responses(handler)
        hostile = ResourceRef(kind="Pod", name='we"b', namespace="prod")
        await PodMetricsCollector(
            hostile, PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())

        assert seen
        assert all('pod="we\\"b"' in query for query in seen)


ENGINE = AnalysisEngine()


def deep(kind, data, target=POD):
    return {
        "health": {"status": "issues_found"},
        "deep_evidence": {
            kind: [
                {
                    "id": f"{kind}:{target.key}",
                    "target": target.to_dict(),
                    "status": "ok",
                    "data": data,
                }
            ]
        },
    }


class TestSignals:
    def test_memory_at_the_limit_confirms_the_limit_caused_it(self):
        result = ENGINE.analyze(deep("prometheus.pod.metrics", {"memory_peak_percent": 99.4}))
        assert result.by_type(SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT)

    def test_memory_merely_high_is_a_weaker_signal(self):
        result = ENGINE.analyze(
            deep("prometheus.pod.metrics", {"memory_utilisation_percent": 88.0})
        )
        assert result.by_type(SignalType.METRICS_MEMORY_NEAR_LIMIT)
        assert not result.by_type(SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT)

    def test_healthy_memory_produces_no_signal(self):
        result = ENGINE.analyze(
            deep("prometheus.pod.metrics", {"memory_utilisation_percent": 40.0})
        )
        assert result.signals == ()

    def test_cpu_throttling_is_detected(self):
        result = ENGINE.analyze(deep("prometheus.pod.metrics", {"cpu_throttled_ratio": 0.6}))
        signal = result.by_type(SignalType.METRICS_CPU_THROTTLED)[0]
        assert "60%" in signal.summary

    def test_repeated_restarts_are_detected(self):
        result = ENGINE.analyze(deep("prometheus.pod.metrics", {"restarts_in_window": 9}))
        assert result.by_type(SignalType.METRICS_RESTART_RATE)

    def test_node_overcommitment_supports_the_scheduling_hypothesis(self):
        result = ENGINE.analyze(
            deep(
                "prometheus.node.metrics",
                {"memory_committed_percent": 97.0, "cpu_committed_percent": 40.0},
                target=NODE,
            )
        )
        assert result.by_type(SignalType.METRICS_NODE_OVERCOMMITTED)

    def test_historical_logs_need_enough_lines_to_matter(self):
        few = ENGINE.analyze(deep("loki.pod.logs", {"matched_lines": 3, "entries": []}))
        assert not few.by_type(SignalType.LOGS_HISTORICAL_ERRORS)

        many = ENGINE.analyze(
            deep("loki.pod.logs", {"matched_lines": 250, "entries": [{"line": "boom"}]})
        )
        assert many.by_type(SignalType.LOGS_HISTORICAL_ERRORS)

    def test_absent_backends_produce_no_signals_at_all(self):
        """Missing metrics must never read as healthy metrics."""
        result = ENGINE.analyze({"health": {"status": "issues_found"}, "deep_evidence": {}})
        assert result.signals == ()

    def test_metrics_corroborate_the_oom_hypothesis(self):
        payload = deep("prometheus.pod.metrics", {"memory_peak_percent": 99.0})
        payload["deep_evidence"]["k8s.pod.spec"] = [
            {
                "id": f"k8s.pod.spec:{POD.key}",
                "target": POD.to_dict(),
                "status": "ok",
                "data": {
                    "containers": [
                        {
                            "name": "web",
                            "restart_count": 4,
                            "limits": {"memory": "512Mi"},
                            "last_state": {"reason": "OOMKilled", "exit_code": 137},
                        }
                    ]
                },
            }
        ]

        result = ENGINE.analyze(payload)
        oom = result.hypothesis("workload.out_of_memory")

        assert oom is not None
        assert any(
            "metrics.memory_peaked_at_limit" in signal_id for signal_id in oom.supporting_signal_ids
        )
