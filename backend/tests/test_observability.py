"""Prometheus and Loki integration.

Clients are exercised through httpx's MockTransport, so the real request
building and response parsing run — only the network is faked.

`TestAgainstCapturedRealBackends` at the foot of this file replays traffic
captured from a live kube-prometheus-stack and Loki. It exists because the
hand-written handlers here answer every query with the same value, which makes
an absent metric and a metric reading zero indistinguishable — and that is
precisely how three defects survived: the memory limit resolving to nothing on
the most common Prometheus deployment there is, a node query filtering on a
label node-exporter does not publish, and an unaggregated selector whose peak
depended on Prometheus's result ordering.
"""

import json
from pathlib import Path

import httpx
import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from app.collectors.base import CollectionContext, InvestigationScope
from app.collectors.observability import (
    NodeMetricsCollector,
    PodLogSearchCollector,
    PodMetricsCollector,
    _plausible_limit,
)
from app.evidence.models import EvidenceStatus, ResourceRef
from app.integrations.loki import LokiClient
from app.integrations.prometheus import PrometheusClient
from app.providers.local_kubectl import LocalKubectlProvider

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
    return CollectionContext(
        scope=InvestigationScope(context="test"),
        provider=LocalKubectlProvider(context="test"),
    )


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

    def test_a_pod_sampled_after_an_oom_kill_still_reports_its_peak(self):
        """The restart that proves the fault must not erase the evidence for it.

        Real numbers from a 96Mi container the kernel had killed eight times:
        peak 91.6% of the limit, current 0.2% because the sample landed just
        after a restart. The two thresholds used to be chained with `elif`, so
        a low current skipped the peak check entirely and the OOM produced no
        memory signal at all.
        """
        result = ENGINE.analyze(
            deep(
                "prometheus.pod.metrics",
                {"memory_peak_percent": 91.6, "memory_utilisation_percent": 0.2},
            )
        )

        signals = result.by_type(SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT)
        assert signals, "a container that peaked at 91.6% of its limit must be reported"
        assert "91.6" in signals[0].summary

    def test_a_peak_below_the_threshold_with_low_current_stays_silent(self):
        """The fix must not turn every restarted pod into a memory finding."""
        result = ENGINE.analyze(
            deep(
                "prometheus.pod.metrics",
                {"memory_peak_percent": 40.0, "memory_utilisation_percent": 0.2},
            )
        )
        assert result.signals == ()

    def test_a_historical_peak_says_so_rather_than_quoting_the_current_value(self):
        result = ENGINE.analyze(
            deep(
                "prometheus.pod.metrics",
                {"memory_peak_percent": 87.0, "memory_utilisation_percent": 3.0},
            )
        )
        signal = result.by_type(SignalType.METRICS_MEMORY_NEAR_LIMIT)[0]

        assert "peaked at 87.0%" in signal.summary
        assert "3.0% now" in signal.summary

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


FIXTURE = Path(__file__).parent / "fixtures" / "real_observability_kps_loki.json"
REAL = json.loads(FIXTURE.read_text())

EMPTY_VECTOR = {"status": "success", "data": {"resultType": "vector", "result": []}}


@pytest.fixture
def replay_real_backends(monkeypatch):
    """Serve the captured responses of a live kube-prometheus-stack and Loki.

    An unrecognised query returns an **empty vector**, because that is exactly
    what the real Prometheus returned for the metric names this code used to
    ask for. That makes the fixture a regression harness rather than a
    recording: change a query back to a series the platform does not export and
    the value goes missing here in the same way it went missing in the cluster.
    """
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        asked.append(query)
        backend = "loki" if "loki" in request.url.path else "prometheus"
        body = REAL[backend].get(query)
        if body is None:
            body = (
                {"status": "success", "data": {"resultType": "streams", "result": []}}
                if backend == "loki"
                else EMPTY_VECTOR
            )
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return asked


def real_pod(role: str) -> ResourceRef:
    return ResourceRef(kind="Pod", name=REAL["pods"][role], namespace="obsfault")


class TestAgainstCapturedRealBackends:
    """Replays traffic captured from a live kube-prometheus-stack and Loki.

    Every defect this class pins was invisible to the hand-written fixtures
    above, and for one shared reason: those handlers answer every query with
    the same value, so a metric that does not exist and a metric reading zero
    are the same thing, and `peak` and `current` can never differ.
    """

    async def test_the_memory_limit_survives_kube_prometheus_stack(self, replay_real_backends):
        """The signal this integration exists for, on the deployment it will meet.

        kube-prometheus-stack drops `container_spec.*` in its kubelet
        ServiceMonitor, so sourcing the limit from cAdvisor alone left
        `memory_limit_bytes` permanently None — and with it both derived
        percentages and both memory signals — while the evidence still said OK.
        """
        evidence = await PodMetricsCollector(
            real_pod("oom"), PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())
        data = evidence[0].data

        assert data["memory_limit_bytes"] == 100663296.0, "96Mi, from kube-state-metrics"
        assert data["memory_peak_percent"] == 91.6
        assert evidence[0].status is EvidenceStatus.OK

    async def test_the_metric_names_this_used_to_ask_for_really_are_absent(self):
        """Not an assumption: both were queried against the live deployment."""
        for record in REAL["known_absent"].values():
            if not isinstance(record, dict):
                continue
            assert record["response"]["data"]["result"] == [], record["query"]

    async def test_every_query_reduces_to_a_single_series(self, replay_real_backends):
        """`scalar()` takes samples[0], and Prometheus does not order results.

        An unaggregated selector over a pod returns one series per container and
        per restarted instance — ten for a pod that had crashed six times — so
        the peak was whichever the server happened to return first: 5.9 MB or
        92 MB from the same query, the difference between a signal and silence.
        """
        for role in ("oom", "throttled", "crasher"):
            await PodMetricsCollector(
                real_pod(role), PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
            ).collect(context())
        await NodeMetricsCollector(
            ResourceRef(kind="Node", name=REAL["node"]),
            PrometheusClient(base_url=CONFIGURED_PROMETHEUS),
        ).collect(context())

        assert replay_real_backends, "the collectors must actually have queried"
        for query in replay_real_backends:
            series = REAL["prometheus"][query]["data"]["result"]
            assert len(series) <= 1, f"{len(series)} series returned for: {query}"

    async def test_an_absent_series_is_named_rather_than_silently_none(self, replay_real_backends):
        """A gap has to be citable; a None reads as a healthy measurement."""
        pod = ResourceRef(kind="Pod", name="never-scraped", namespace="obsfault")
        evidence = await PodMetricsCollector(
            pod, PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())
        data = evidence[0].data

        assert data["memory_limit_bytes"] is None
        assert "memory_limit_bytes" in data["absent_metrics"]
        assert "memory_working_set_bytes" in data["absent_metrics"]

    async def test_a_pod_with_real_metrics_lists_no_absent_ones(self, replay_real_backends):
        evidence = await PodMetricsCollector(
            real_pod("oom"), PrometheusClient(base_url=CONFIGURED_PROMETHEUS)
        ).collect(context())

        assert evidence[0].data["absent_metrics"] == []

    async def test_node_metrics_read_a_label_that_exists(self, replay_real_backends):
        """node-exporter series carry instance/job and never a node label."""
        evidence = await NodeMetricsCollector(
            ResourceRef(kind="Node", name=REAL["node"]),
            PrometheusClient(base_url=CONFIGURED_PROMETHEUS),
        ).collect(context())
        data = evidence[0].data

        assert data["used_memory_bytes"] is not None
        assert data["absent_metrics"] == []
        assert data["memory_committed_percent"] == 6.5

    async def test_the_cgroup_no_limit_sentinel_is_not_treated_as_a_limit(self):
        """cAdvisor reports 2^63-ish for a container with no limit set."""
        assert _plausible_limit(9223372036854771712.0) is None
        assert _plausible_limit(100663296.0) == 100663296.0
        assert _plausible_limit(0.0) is None
        assert _plausible_limit(None) is None

    async def test_real_promtail_labels_and_entries_parse(self, replay_real_backends):
        """Promtail's stream labels, Loki 3.x nanosecond strings, real lines."""
        evidence = await PodLogSearchCollector(
            real_pod("crasher"), LokiClient(base_url=CONFIGURED_LOKI)
        ).collect(context())
        data = evidence[0].data

        assert evidence[0].status is EvidenceStatus.OK
        assert data["matched_lines"] > 20
        first = data["entries"][0]
        assert first["labels"]["namespace"] == "obsfault"
        assert first["timestamp"].startswith("2026-")
        assert first["line"]

    async def test_captured_logs_are_ordered_newest_first(self, replay_real_backends):
        evidence = await PodLogSearchCollector(
            real_pod("crasher"), LokiClient(base_url=CONFIGURED_LOKI)
        ).collect(context())
        stamps = [entry["timestamp"] for entry in evidence[0].data["entries"]]

        assert stamps == sorted(stamps, reverse=True)

    async def test_the_live_faults_produce_the_signals_they_should(self, replay_real_backends):
        """End to end on real data: the OOM, the throttling and the log volume."""
        records = []
        for role in ("oom", "throttled", "crasher"):
            target = real_pod(role)
            for collector in (
                PodMetricsCollector(target, PrometheusClient(base_url=CONFIGURED_PROMETHEUS)),
                PodLogSearchCollector(target, LokiClient(base_url=CONFIGURED_LOKI)),
            ):
                for item in await collector.collect(context()):
                    records.append(
                        {
                            "id": item.id,
                            "target": target.to_dict(),
                            "status": "ok",
                            "data": item.data,
                            "kind": item.kind,
                        }
                    )

        grouped: dict[str, list] = {}
        for record in records:
            grouped.setdefault(record["kind"], []).append(record)

        result = ENGINE.analyze({"health": {"status": "issues_found"}, "deep_evidence": grouped})
        types = {signal.type for signal in result.signals}

        assert SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT in types
        assert SignalType.METRICS_CPU_THROTTLED in types
        assert SignalType.METRICS_RESTART_RATE in types
        assert SignalType.LOGS_HISTORICAL_ERRORS in types


class TestMultiTenantBackends:
    """`X-Scope-OrgID`, without which a tenanted Loki or Mimir refuses everything.

    Loki answers a query with no org id with `401 no org id`, which this client
    correctly records as `unavailable`. So the failure was always legible in an
    investigation; what was missing was any way to succeed against a backend
    the majority of Grafana Cloud and self-hosted Loki deployments run.

    Asserted on the header the client actually put on the wire, through
    MockTransport, rather than on the attribute — a `headers` property that is
    correct and never passed to `AsyncClient` is the same defect as no property
    at all, and reads identically in a test that inspects the object.
    """

    async def test_loki_sends_the_org_id_when_configured(self, promql_responses):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        promql_responses(handler)
        await LokiClient(base_url=CONFIGURED_LOKI, tenant_id="acme").query_range('{app="web"}')

        assert seen[0].headers["X-Scope-OrgID"] == "acme"

    async def test_loki_sends_no_org_id_when_unset(self, promql_responses):
        """Single-tenant Loki rejects a header it did not expect."""
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        promql_responses(handler)
        await LokiClient(base_url=CONFIGURED_LOKI, tenant_id="").query_range('{app="web"}')

        assert "X-Scope-OrgID" not in seen[0].headers

    async def test_prometheus_sends_the_org_id_when_configured(self, promql_responses):
        """Mimir, Cortex and Thanos use the same header, and refuse without it."""
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        promql_responses(handler)
        await PrometheusClient(base_url=CONFIGURED_PROMETHEUS, tenant_id="acme").query("up")

        assert seen[0].headers["X-Scope-OrgID"] == "acme"

    async def test_prometheus_sends_no_org_id_when_unset(self, promql_responses):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        promql_responses(handler)
        await PrometheusClient(base_url=CONFIGURED_PROMETHEUS, tenant_id="").query("up")

        assert "X-Scope-OrgID" not in seen[0].headers

    async def test_the_default_comes_from_configuration(self, monkeypatch):
        """Not from the ambient platform tenant, which is a different namespace."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "loki_tenant_id", "from-config")
        monkeypatch.setattr(settings, "prometheus_tenant_id", "from-config")

        assert LokiClient(base_url=CONFIGURED_LOKI).headers == {"X-Scope-OrgID": "from-config"}
        assert PrometheusClient(base_url=CONFIGURED_PROMETHEUS).headers == {
            "X-Scope-OrgID": "from-config"
        }
