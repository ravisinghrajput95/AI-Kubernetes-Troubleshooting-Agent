"""Collectors for Kubernetes evidence.

The existing inspectors under `app/kubernetes/` are already correct and well
tested in production use, so they are adapted rather than rewritten. Each
adapter runs the synchronous inspector off the event loop and translates its
result — including its established error contract — into evidence.
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from app.collectors.base import BaseCollector, CollectionContext
from app.evidence.models import Evidence, EvidenceKind, EvidenceSource, EvidenceStatus
from app.kubernetes.deployment_inspector import DeploymentInspector
from app.kubernetes.errors import classify_error
from app.kubernetes.events_analyzer import EventsAnalyzer
from app.kubernetes.kubectl_executor import KubectlExecutor
from app.kubernetes.logs_collector import LogsCollector
from app.kubernetes.network_inspector import NetworkInspector
from app.kubernetes.node_inspector import NodeInspector
from app.kubernetes.pod_inspector import PodInspector
from app.kubernetes.storage_inspector import StorageInspector
from app.kubernetes.workload_inspector import WorkloadInspector
from app.providers.base import OutputFormat, ProviderResult, ReadVerb, ResourceRequest

InspectFn = Callable[[CollectionContext], dict[str, Any]]


def _status_for(payload: dict[str, Any]) -> tuple[EvidenceStatus, str]:
    """Translate the inspectors' `{"error": ...}` contract into evidence status."""
    error = payload.get("error")
    if not error:
        return EvidenceStatus.OK, ""
    return classify_error(str(error))


class LegacyInspectorCollector(BaseCollector):
    """Adapts a synchronous inspector to the collector protocol."""

    def __init__(
        self,
        collector_id: str,
        kind: str,
        inspect: InspectFn,
        label: str = "",
        requires: frozenset[str] = frozenset(),
        optional_requires: frozenset[str] = frozenset(),
    ) -> None:
        self.id = collector_id
        self.label = label
        self.provides = frozenset({kind})
        self.requires = requires
        self.optional_requires = optional_requires
        self.kind = kind
        self._inspect = inspect

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        payload = await asyncio.to_thread(self._inspect, context)
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

        return [
            self._evidence(EvidenceKind.METRICS_NODES, node_result, context),
            self._evidence(EvidenceKind.METRICS_PODS, pod_result, context),
        ]

    def _evidence(self, kind: str, result: ProviderResult, context: CollectionContext) -> Evidence:
        if not result.success:
            return Evidence.create(
                kind=kind,
                status=EvidenceStatus.UNAVAILABLE,
                target=context.scope.cluster_ref,
                detail="metrics-server is unavailable or kubectl top is not permitted.",
                command=result.equivalent_command,
                collector_id=self.id,
            )

        lines = [line for line in result.text.splitlines() if line.strip()]
        return Evidence.create(
            kind=kind,
            status=EvidenceStatus.OK if lines else EvidenceStatus.EMPTY,
            target=context.scope.cluster_ref,
            data={"lines": lines},
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

    def __init__(self, collector: LogsCollector) -> None:
        self._collector = collector

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

        payload = await asyncio.to_thread(self._collector.collect, problematic)
        return [
            Evidence.create(
                kind=EvidenceKind.POD_LOGS,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                data=payload,
                collector_id=self.id,
            )
        ]


def build_default_collectors(kubectl: KubectlExecutor) -> list[BaseCollector]:
    """The built-in collection graph.

    Everything except pod logs is independent, so the scheduler runs it as a
    single concurrent wave; logs follow in a second wave once pods are known.
    """
    pod_inspector = PodInspector(kubectl)
    events_analyzer = EventsAnalyzer(kubectl)
    deployment_inspector = DeploymentInspector(kubectl)
    network_inspector = NetworkInspector(kubectl)
    node_inspector = NodeInspector(kubectl)
    storage_inspector = StorageInspector(kubectl)
    workload_inspector = WorkloadInspector(kubectl)

    return [
        LegacyInspectorCollector(
            "k8s.pods",
            EvidenceKind.PODS,
            lambda ctx: pod_inspector.inspect(
                namespace=ctx.scope.namespace,
                pod_name=ctx.scope.resource_name if ctx.scope.targets("pod") else None,
            ),
            label="Retrieved Pods",
        ),
        LegacyInspectorCollector(
            "k8s.events",
            EvidenceKind.EVENTS,
            lambda ctx: events_analyzer.analyze(namespace=ctx.scope.namespace),
            label="Retrieved Events",
        ),
        LegacyInspectorCollector(
            "k8s.deployments",
            EvidenceKind.DEPLOYMENTS,
            lambda ctx: deployment_inspector.inspect(
                namespace=ctx.scope.namespace,
                deployment_name=(
                    ctx.scope.resource_name if ctx.scope.targets("deployment") else None
                ),
            ),
            label="Validated Deployments",
        ),
        LegacyInspectorCollector(
            "k8s.network",
            EvidenceKind.NETWORK,
            lambda ctx: network_inspector.inspect(namespace=ctx.scope.namespace),
            label="Checked Networking",
        ),
        LegacyInspectorCollector(
            "k8s.nodes",
            EvidenceKind.NODES,
            lambda ctx: node_inspector.inspect(),
            label="Checked Nodes",
        ),
        LegacyInspectorCollector(
            "k8s.storage",
            EvidenceKind.STORAGE,
            lambda ctx: storage_inspector.inspect(namespace=ctx.scope.namespace),
            label="Checked Storage",
        ),
        LegacyInspectorCollector(
            "k8s.workloads",
            EvidenceKind.WORKLOADS,
            lambda ctx: workload_inspector.inspect(namespace=ctx.scope.namespace),
            label="Checked Extended Workloads",
        ),
        RawNodesCollector(),
        ResourceMetricsCollector(),
        PodLogsCollector(LogsCollector(kubectl)),
    ]
