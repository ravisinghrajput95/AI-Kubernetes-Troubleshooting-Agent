"""Collectors for optional observability backends.

Queries use only metric names exported by cAdvisor and kube-state-metrics, which
are standard across clusters. Application-level metrics (request latency, error
rates) are deliberately not queried: their names are per-application, and a
guessed name returns an empty result that looks like a healthy signal.

**A standard metric name is not the same as a metric that is present.**
kube-prometheus-stack — the most widely deployed Prometheus configuration for
Kubernetes — drops `container_spec.*` wholesale in its kubelet ServiceMonitor
for cardinality, so `container_spec_memory_limit_bytes` does not exist on the
clusters this platform is most likely to meet. Sourcing the limit from there
alone left `memory_limit_bytes` permanently `None`, which silently disabled
*both* memory signals: a container OOMKilled every ninety seconds produced no
memory finding at all, while the evidence still recorded `OK`. That is the
failure this module's own docstring warns about, one level further down — the
name was right and the series was absent.

So every query that a real deployment can legitimately not answer names a
**preferred source and a fallback** (`a or b`), and any query returning no data
is listed in `absent_metrics` on the evidence. A gap has to be citable; a
silent `None` reads as "measured, and fine".
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from app.collectors.targeted import TargetedCollector
from app.core.config import settings
from app.evidence.models import Evidence, EvidenceSource, EvidenceStatus
from app.integrations.loki import LokiClient
from app.integrations.prometheus import PrometheusClient, QueryResult


class PrometheusKind:
    POD_METRICS = "prometheus.pod.metrics"
    NODE_METRICS = "prometheus.node.metrics"


class LokiKind:
    POD_LOGS = "loki.pod.logs"


MAX_PLAUSIBLE_MEMORY_LIMIT_BYTES = 1 << 60
"""Above an exbibyte is not a memory limit.

A container with no limit set reports the cgroup sentinel
(`9223372036854771712`) through `container_spec_memory_limit_bytes`, and
dividing by it yields "0.0% of the container limit" — a wrong conclusion
presented as a measurement. kube-state-metrics has no series at all for such a
container, which is why it is the preferred source; this guards the fallback.
"""


def _escape(value: str) -> str:
    """Escape a PromQL/LogQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _absent(named: dict[str, QueryResult]) -> list[str]:
    """Names of queries that ran successfully and matched no series.

    `EMPTY` is a usable status — the query was answered — so without this the
    caller cannot tell "the metric says zero" from "the metric is not there".
    Only that distinction makes a dropped or renamed series visible instead of
    arriving as a `None` that reads like a healthy measurement.
    """
    return sorted(name for name, result in named.items() if result.status is EvidenceStatus.EMPTY)


def _plausible_limit(limit: float | None) -> float | None:
    """Discard the cgroup 'no limit' sentinel, keep every real limit."""
    if limit is None or limit <= 0 or limit >= MAX_PLAUSIBLE_MEMORY_LIMIT_BYTES:
        return None
    return limit


class PodMetricsCollector(TargetedCollector):
    """Memory, CPU, throttling and restart history for one pod.

    This is what distinguishes "hit its limit" from "leaked steadily" and
    "throttled into failing its probes" — none of which pod status alone shows.
    """

    kind = PrometheusKind.POD_METRICS
    prefix = "prometheus.pod.metrics"
    label = "Queried Pod Metrics"

    def __init__(self, target, client: PrometheusClient | None = None) -> None:
        super().__init__(target)
        self._client = client or PrometheusClient()

    async def collect(self, context) -> Sequence[Evidence]:
        pod = _escape(self.target.name)
        namespace = _escape(self.target.namespace or "")
        selector = f'namespace="{namespace}",pod="{pod}"'
        window = f"{settings.metrics_lookback_minutes}m"

        queries = {
            "memory_working_set_bytes": f"max(container_memory_working_set_bytes{{{selector}}})",
            # kube-state-metrics first: it reports the *declared* limit, which is
            # what "% of the container limit" means, and it simply has no series
            # for a container with no limit rather than a cgroup sentinel.
            # cAdvisor's spec metric is the fallback for clusters running no
            # kube-state-metrics — and is dropped outright by kube-prometheus-stack.
            "memory_limit_bytes": (
                f'max(kube_pod_container_resource_limits{{{selector},resource="memory"}}) '
                f"or max(container_spec_memory_limit_bytes{{{selector}}})"
            ),
            # The outer max() is not decoration. max_over_time over a pod's
            # selector returns one series per container *and* per restarted
            # container instance — ten of them for a pod that had crashed six
            # times — and `scalar()` takes the first, which Prometheus does not
            # order. Unaggregated, the peak was whichever instance happened to
            # come back first: 5.9 MB or 92 MB from the same query.
            "memory_peak_bytes": (
                f"max(max_over_time(container_memory_working_set_bytes{{{selector}}}[{window}]))"
            ),
            "cpu_cores": (f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m]))"),
            "cpu_throttled_ratio": (
                f"sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[5m])) "
                f"/ clamp_min(sum(rate(container_cpu_cfs_periods_total{{{selector}}}[5m])), 1)"
            ),
            "restarts_total": (f"max(kube_pod_container_status_restarts_total{{{selector}}})"),
            # Same reason: one series per container, so a pod with a sidecar
            # reported whichever container Prometheus happened to return first.
            "restarts_in_window": (
                f"max(increase(kube_pod_container_status_restarts_total{{{selector}}}[{window}]))"
            ),
        }

        results = await asyncio.gather(*(self._client.query(promql) for promql in queries.values()))
        named = dict(zip(queries, results, strict=True))

        unavailable = next((result for result in results if not result.usable), None)
        if unavailable is not None and all(not r.usable for r in results):
            return [
                self._evidence(
                    context,
                    unavailable.status,
                    detail=unavailable.detail,
                    command=f"promql: {unavailable.query}",
                )
            ]

        data = {name: self._value(result) for name, result in named.items()}
        data["queries"] = {name: result.query for name, result in named.items()}
        data["absent_metrics"] = _absent(named)
        data.update(self._derive(data))

        return [
            self._evidence(
                context,
                EvidenceStatus.OK,
                data=data,
                command=f"promql against {self._client.base_url}",
            )
        ]

    def _value(self, result: QueryResult) -> float | None:
        return result.scalar() if result.usable else None

    def _derive(self, data: dict[str, Any]) -> dict[str, Any]:
        working_set = data.get("memory_working_set_bytes")
        peak = data.get("memory_peak_bytes")
        limit = _plausible_limit(data.get("memory_limit_bytes"))

        derived: dict[str, Any] = {}
        if limit and limit > 0:
            if working_set is not None:
                derived["memory_utilisation_percent"] = round(working_set / limit * 100, 1)
            if peak is not None:
                derived["memory_peak_percent"] = round(peak / limit * 100, 1)
        return derived

    def _evidence(self, context, status, data=None, detail="", command=None) -> Evidence:
        return Evidence.create(
            kind=self.kind,
            status=status,
            target=self.target,
            source=EvidenceSource.PROMETHEUS,
            data=data,
            detail=detail,
            command=command,
            collector_id=self.id,
        )


class NodeMetricsCollector(TargetedCollector):
    """Allocatable pressure on a node, for scheduling and eviction questions."""

    kind = PrometheusKind.NODE_METRICS
    prefix = "prometheus.node.metrics"
    label = "Queried Node Metrics"

    def __init__(self, target, client: PrometheusClient | None = None) -> None:
        super().__init__(target)
        self._client = client or PrometheusClient()

    async def collect(self, context) -> Sequence[Evidence]:
        node = _escape(self.target.name)
        queries = {
            # Not node_exporter's `node_memory_MemAvailable_bytes`: its series
            # carry `instance`/`job` and no `node` label, so filtering by node
            # matched nothing on every deployment and this read `None` forever.
            # cAdvisor's per-container series do carry `node`, and summing them
            # is the usage figure a scheduling or eviction question wants.
            "used_memory_bytes": f'sum(container_memory_working_set_bytes{{node="{node}"}})',
            # Aggregated for the same reason as the pod queries: two
            # kube-state-metrics replicas, or one mid-rollout, publish the same
            # fact twice and `scalar()` would pick between them arbitrarily.
            "allocatable_memory_bytes": (
                f'max(kube_node_status_allocatable{{node="{node}",resource="memory"}})'
            ),
            "allocatable_cpu_cores": (
                f'max(kube_node_status_allocatable{{node="{node}",resource="cpu"}})'
            ),
            "requested_memory_bytes": (
                f'sum(kube_pod_container_resource_requests{{node="{node}",resource="memory"}})'
            ),
            "requested_cpu_cores": (
                f'sum(kube_pod_container_resource_requests{{node="{node}",resource="cpu"}})'
            ),
        }

        results = await asyncio.gather(*(self._client.query(promql) for promql in queries.values()))
        named = dict(zip(queries, results, strict=True))

        if all(not result.usable for result in results):
            first = results[0]
            return [
                Evidence.create(
                    kind=self.kind,
                    status=first.status,
                    target=self.target,
                    source=EvidenceSource.PROMETHEUS,
                    detail=first.detail,
                    command=f"promql: {first.query}",
                    collector_id=self.id,
                )
            ]

        data: dict[str, Any] = {
            name: (result.scalar() if result.usable else None) for name, result in named.items()
        }
        data["absent_metrics"] = _absent(named)

        allocatable = data.get("allocatable_memory_bytes")
        requested = data.get("requested_memory_bytes")
        if allocatable and requested is not None and allocatable > 0:
            data["memory_committed_percent"] = round(requested / allocatable * 100, 1)

        allocatable_cpu = data.get("allocatable_cpu_cores")
        requested_cpu = data.get("requested_cpu_cores")
        if allocatable_cpu and requested_cpu is not None and allocatable_cpu > 0:
            data["cpu_committed_percent"] = round(requested_cpu / allocatable_cpu * 100, 1)

        return [
            Evidence.create(
                kind=self.kind,
                status=EvidenceStatus.OK,
                target=self.target,
                source=EvidenceSource.PROMETHEUS,
                data=data,
                command=f"promql against {self._client.base_url}",
                collector_id=self.id,
            )
        ]


class PodLogSearchCollector(TargetedCollector):
    """Historical logs for a pod, from before the current container instance.

    kubectl only serves logs for the current and previous container. Loki
    retains the whole history, which is what makes a slow-building failure
    visible.
    """

    kind = LokiKind.POD_LOGS
    prefix = "loki.pod.logs"
    label = "Searched Historical Logs"

    def __init__(self, target, client: LokiClient | None = None) -> None:
        super().__init__(target)
        self._client = client or LokiClient()

    async def collect(self, context) -> Sequence[Evidence]:
        namespace = _escape(self.target.namespace or "")
        pod = _escape(self.target.name)
        logql = (
            f'{{namespace="{namespace}",pod="{pod}"}} |~ "(?i)(error|fatal|panic|exception|failed)"'
        )

        result = await self._client.query_range(logql)

        if not result.usable:
            return [
                Evidence.create(
                    kind=self.kind,
                    status=result.status,
                    target=self.target,
                    source=EvidenceSource.LOKI,
                    detail=result.detail,
                    command=f"logql: {logql}",
                    collector_id=self.id,
                )
            ]

        return [
            Evidence.create(
                kind=self.kind,
                status=EvidenceStatus.OK if result.entries else EvidenceStatus.EMPTY,
                target=self.target,
                source=EvidenceSource.LOKI,
                data={
                    "matched_lines": len(result.entries),
                    "entries": result.entries[:60],
                    "query": logql,
                },
                command=f"logql: {logql}",
                collector_id=self.id,
            )
        ]
