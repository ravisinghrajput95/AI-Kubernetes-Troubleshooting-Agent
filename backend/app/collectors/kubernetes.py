"""Collectors for Kubernetes evidence.

The inspectors under `app/kubernetes/` carry real production behaviour, so M5
moved their analysis across unchanged and replaced only how they reach a
cluster. An inspector now declares `ResourceRequest`s and analyses the results;
this module is the single adapter that runs the two halves and turns the
outcome into evidence, including the established `{"error": ...}` contract.

One consequence worth stating, because it is the reason the migration was worth
doing beyond removing `raw_executor()`: an inspector's reads now go out as a
**batch**. `WorkloadInspector` made four sequential kubectl calls; it now issues
one `fetch_many`, which against a remote agent is one round trip rather than
four on a stream that may cross a continent.
"""

from collections.abc import Sequence
from typing import Any

from app.collectors.base import BaseCollector, CollectionContext
from app.evidence.models import Evidence, EvidenceKind, EvidenceSource, EvidenceStatus
from app.kubernetes import metrics
from app.kubernetes.deployment_inspector import DeploymentInspector
from app.kubernetes.errors import classify_error
from app.kubernetes.events_analyzer import EventsAnalyzer
from app.kubernetes.inspector import Inspector
from app.kubernetes.logs_collector import LogsCollector
from app.kubernetes.network_inspector import NetworkInspector
from app.kubernetes.node_inspector import NodeInspector
from app.kubernetes.pod_inspector import PodInspector
from app.kubernetes.storage_inspector import StorageInspector
from app.kubernetes.workload_inspector import WorkloadInspector
from app.providers.base import OutputFormat, ProviderResult, ReadVerb, ResourceRequest


def _lines(result: ProviderResult) -> list[str]:
    return [line for line in result.text.splitlines() if line.strip()]


def _status_for(payload: dict[str, Any]) -> tuple[EvidenceStatus, str]:
    """Translate the inspectors' `{"error": ...}` contract into evidence status."""
    error = payload.get("error")
    if not error:
        return EvidenceStatus.OK, ""
    return classify_error(str(error))


class InspectorCollector(BaseCollector):
    """Runs one inspector: fetch what it asks for, then let it analyse."""

    def __init__(
        self,
        inspector: Inspector,
        requires: frozenset[str] = frozenset(),
        optional_requires: frozenset[str] = frozenset(),
    ) -> None:
        self.id = inspector.id
        self.label = inspector.label
        self.kind = inspector.kind
        self.provides = frozenset({inspector.kind})
        self.requires = requires
        self.optional_requires = optional_requires
        self._inspector = inspector

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        requests = self._inspector.requests(context.scope)
        results = await context.provider.fetch_many(requests)
        payload = self._inspector.analyse(results, context.scope)

        status, detail = _status_for(payload)
        command = payload.get("command", {}).get("command") if payload.get("command") else None

        return [
            Evidence.create(
                kind=self.kind,
                status=status,
                target=context.scope.cluster_ref,
                source=EvidenceSource.KUBECTL,
                data=payload,
                detail=detail,
                command=command,
                collector_id=self.id,
            )
        ]


class RawNodesCollector(BaseCollector):
    """Raw node objects, used to derive cluster overview counts.

    Kept separate from `NodeInspector` (which reports findings, not objects) so
    that overview figures become citable evidence instead of an inline read.
    """

    id = "k8s.nodes.raw"
    label = "Mapped Cluster Nodes"
    provides = frozenset({EvidenceKind.NODES_RAW})

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await context.fetch(ResourceRequest(verb=ReadVerb.GET, resource="nodes"))

        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                Evidence.create(
                    kind=EvidenceKind.NODES_RAW,
                    status=status,
                    target=context.scope.cluster_ref,
                    detail=detail,
                    command=result.equivalent_command,
                    collector_id=self.id,
                )
            ]

        items = result.data.get("items", [])
        return [
            Evidence.create(
                kind=EvidenceKind.NODES_RAW,
                status=EvidenceStatus.OK if items else EvidenceStatus.EMPTY,
                target=context.scope.cluster_ref,
                data={"items": items},
                command=result.equivalent_command,
                collector_id=self.id,
            )
        ]


class ResourceMetricsCollector(BaseCollector):
    """Cluster resource usage via metrics-server.

    metrics-server is frequently absent or unauthorized; that is a normal,
    expected degradation and is recorded as unavailable evidence rather than an
    error, so a diagnosis can state that metrics were not consulted.
    """

    id = "k8s.metrics"
    label = "Collected Cluster Metrics"
    provides = frozenset({EvidenceKind.METRICS_NODES, EvidenceKind.METRICS_PODS})
    # Node capacity turns raw usage into a percentage. Optional, because
    # metrics without percentages are still worth having and a cluster whose
    # nodes could not be listed has larger problems than a missing ratio.
    optional_requires = frozenset({EvidenceKind.NODES_RAW})

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        node_result, pod_result = await context.provider.fetch_many(
            [
                ResourceRequest(verb=ReadVerb.TOP, resource="nodes", output=OutputFormat.TEXT),
                ResourceRequest(
                    verb=ReadVerb.TOP,
                    resource="pods",
                    all_namespaces=True,
                    output=OutputFormat.TEXT,
                ),
            ]
        )

        allocatable = metrics.allocatable_by_node(
            (context.store.data(EvidenceKind.NODES_RAW, {}) or {}).get("items", [])
        )

        return [
            self._evidence(
                EvidenceKind.METRICS_NODES,
                node_result,
                context,
                self._nodes(node_result, allocatable),
            ),
            self._evidence(EvidenceKind.METRICS_PODS, pod_result, context, self._pods(pod_result)),
        ]

    def _nodes(
        self, result: ProviderResult, allocatable: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Node usage, with percentages derived rather than transported."""
        records = (
            metrics.node_usage_from_api(result.data)
            if isinstance(result.data, dict)
            else metrics.node_usage_from_text(_lines(result))
        )

        rows = []
        for record in records:
            capacity = allocatable.get(record["name"], {})
            cpu_percent = metrics.percent(record["cpu_cores"], capacity.get("cpu_cores"))
            memory_percent = metrics.percent(record["memory_bytes"], capacity.get("memory_bytes"))
            rows.append(
                {
                    "name": record["name"],
                    "cpu": metrics.format_cpu(record["cpu_cores"]),
                    "cpu_percent": f"{cpu_percent}%" if cpu_percent is not None else "N/A",
                    "memory": metrics.format_memory(record["memory_bytes"]),
                    "memory_percent": f"{memory_percent}%" if memory_percent is not None else "N/A",
                    "cpu_percent_value": cpu_percent,
                    "memory_percent_value": memory_percent,
                }
            )
        return rows

    def _pods(self, result: ProviderResult) -> list[dict[str, Any]]:
        records = (
            metrics.pod_usage_from_api(result.data)
            if isinstance(result.data, dict)
            else metrics.pod_usage_from_text(_lines(result))
        )
        return [
            {
                "namespace": record["namespace"],
                "name": record["name"],
                "cpu": metrics.format_cpu(record["cpu_cores"]),
                "memory": metrics.format_memory(record["memory_bytes"]),
            }
            for record in records
        ]

    def _evidence(
        self,
        kind: str,
        result: ProviderResult,
        context: CollectionContext,
        rows: list[dict[str, Any]],
    ) -> Evidence:
        if not result.success:
            return Evidence.create(
                kind=kind,
                status=EvidenceStatus.UNAVAILABLE,
                target=context.scope.cluster_ref,
                detail="metrics-server is unavailable or kubectl top is not permitted.",
                command=result.equivalent_command,
                collector_id=self.id,
            )

        return Evidence.create(
            kind=kind,
            status=EvidenceStatus.OK if rows else EvidenceStatus.EMPTY,
            target=context.scope.cluster_ref,
            data={"records": rows},
            command=result.equivalent_command,
            collector_id=self.id,
        )


class PodLogsCollector(BaseCollector):
    """Logs for pods the pod inspector flagged as problematic.

    Declaring `requires` on pod evidence is what makes the dependency explicit:
    if pod collection degrades, the scheduler records logs as not-applicable
    with the reason, instead of silently collecting nothing.
    """

    id = "k8s.pods.logs"
    label = "Read Pod Logs"
    provides = frozenset({EvidenceKind.POD_LOGS})
    requires = frozenset({EvidenceKind.PODS})

    def __init__(self, collector: LogsCollector | None = None) -> None:
        self._collector = collector or LogsCollector()

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        pods = context.store.data(EvidenceKind.PODS, {}) or {}
        problematic = pods.get("problematic_pods", [])

        if not problematic:
            return [
                Evidence.create(
                    kind=EvidenceKind.POD_LOGS,
                    status=EvidenceStatus.EMPTY,
                    target=context.scope.cluster_ref,
                    data={"checked_pods": 0, "logs": []},
                    detail="No problematic pods were found, so no logs were collected.",
                    collector_id=self.id,
                )
            ]

        requests = self._collector.requests(problematic)
        results = await context.provider.fetch_many(requests)
        payload = self._collector.analyse(problematic, results)

        return [
            Evidence.create(
                kind=EvidenceKind.POD_LOGS,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                data=payload,
                collector_id=self.id,
            )
        ]


# Every inspector the baseline graph runs. Each declares its own id, evidence
# kind and label, so adding one is appending to this list — there is no longer
# a per-inspector lambda restating what the inspector already knows.
DEFAULT_INSPECTORS: tuple[type[Inspector], ...] = (
    PodInspector,
    EventsAnalyzer,
    DeploymentInspector,
    NetworkInspector,
    NodeInspector,
    StorageInspector,
    WorkloadInspector,
)


def build_default_collectors() -> list[BaseCollector]:
    """The built-in collection graph.

    Everything except pod logs is independent, so the scheduler runs it as a
    single concurrent wave; logs follow in a second wave once pods are known.

    Takes no executor: since M5 nothing in this graph knows how a cluster is
    reached. The provider on the `CollectionContext` decides that, which is
    what lets the identical graph run against a local kubeconfig or an agent.
    """
    return [
        *(InspectorCollector(inspector()) for inspector in DEFAULT_INSPECTORS),
        RawNodesCollector(),
        ResourceMetricsCollector(),
        PodLogsCollector(),
    ]
