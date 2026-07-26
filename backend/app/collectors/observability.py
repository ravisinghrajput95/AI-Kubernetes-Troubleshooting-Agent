"""Collectors for optional observability backends.

Queries use only metric names exported by cAdvisor and kube-state-metrics, which
are standard across clusters. Application-level metrics (request latency, error
rates) are deliberately not queried: their names are per-application, and a
guessed name returns an empty result that looks like a healthy signal.
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


def _escape(value: str) -> str:
    """Escape a PromQL/LogQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
            "memory_limit_bytes": f"max(container_spec_memory_limit_bytes{{{selector}}})",
            "memory_peak_bytes": (
                f"max_over_time(container_memory_working_set_bytes{{{selector}}}[{window}])"
            ),
            "cpu_cores": (f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m]))"),
            "cpu_throttled_ratio": (
                f"sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[5m])) "
                f"/ clamp_min(sum(rate(container_cpu_cfs_periods_total{{{selector}}}[5m])), 1)"
            ),
            "restarts_total": (f"max(kube_pod_container_status_restarts_total{{{selector}}})"),
            "restarts_in_window": (
                f"increase(kube_pod_container_status_restarts_total{{{selector}}}[{window}])"
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
        limit = data.get("memory_limit_bytes")

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
            "memory_available_bytes": f'node_memory_MemAvailable_bytes{{node="{node}"}}',
            "allocatable_memory_bytes": (
                f'kube_node_status_allocatable{{node="{node}",resource="memory"}}'
            ),
            "allocatable_cpu_cores": (
                f'kube_node_status_allocatable{{node="{node}",resource="cpu"}}'
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
