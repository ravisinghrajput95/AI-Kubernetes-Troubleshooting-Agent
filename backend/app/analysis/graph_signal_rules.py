"""Signals that only exist because the graph does.

Every other rule in this package reads one section of the investigation and
reports what it says. These read a *path*: a pod is broken here, and the reason
is two hops away in a section that, read on its own, contains nothing unusual.

That is the test for whether a rule belongs in this file. `storage.pvc_unbound`
already says a claim is unbound, and it needs no graph. "This pod is Pending
*because* the claim it mounts is unbound, and that claim is on a StorageClass
whose volumes are failing" is three facts in three sections that mean nothing
apart and name a root cause together — and no single-section rule can reach it.

The eval corpus carries cases that are only answerable this way; if these rules
regress, those cases fall back to the generic hypothesis and the numbers move.
"""

from collections.abc import Sequence

from app.analysis.models import Severity, Signal, SignalType
from app.evidence.models import ResourceRef
from app.graph import ClusterGraph, Relation
from app.graph.models import Edge


class GraphSignalRule:
    """A rule that reads the dependency graph rather than a single section."""

    id: str = "graph"

    def extract(self, data) -> Sequence[Signal]:  # pragma: no cover - interface
        raise NotImplementedError


def _graph_of(data) -> ClusterGraph:
    payload = data.investigation.get("graph")
    return ClusterGraph.from_dict(payload) if isinstance(payload, dict) else ClusterGraph()


def _evidence(edges: Sequence[Edge], *extra: str) -> tuple[str, ...]:
    """Provenance for a traversal: every edge walked, plus the facts at the ends.

    A graph signal has to cite the path, not just the destination. An operator
    reading "your pod is blocked by a storage class" needs to be able to see
    which pod, which claim, and which read established each link.
    """
    ids: list[str] = []
    for edge in edges:
        ids.extend(edge.evidence_ids)
    ids.extend(extra)
    return tuple(dict.fromkeys(item for item in ids if item))


class PodBlockedByStorage:
    """A Pending pod whose unbound claim explains it.

    §3.6 names this as the motivating traversal. Without the graph the platform
    reports two independent findings — a pod is Pending, a claim is unbound —
    and leaves the operator to notice they are the same incident.
    """

    id = "graph.pod.blocked_by_storage"

    def extract(self, data) -> Sequence[Signal]:
        graph = _graph_of(data)
        if not graph.edges:
            return ()

        unbound = {
            f"persistentvolumeclaim/{claim.get('namespace', 'default')}/{claim.get('name', '')}": claim
            for claim in data.section("storage").get("claims", []) or []
            if claim.get("phase") != "Bound"
        }
        if not unbound:
            return ()

        signals = []
        for entry in data.section("pods").get("problematic_pods", []) or []:
            if entry.get("status") not in {"Pending", "ContainerCreating"}:
                continue

            namespace = entry.get("namespace", "default")
            name = entry.get("name", "")
            key = f"pod/{namespace}/{name}"

            for edge in graph.out_edges(key):
                if edge.relation != Relation.MOUNTS or edge.target.key not in unbound:
                    continue

                claim = unbound[edge.target.key]
                signals.append(
                    Signal(
                        id=f"{SignalType.POD_BLOCKED_BY_STORAGE}:{key}",
                        type=SignalType.POD_BLOCKED_BY_STORAGE,
                        severity=Severity.CRITICAL,
                        summary=(
                            f"Pod {namespace}/{name} cannot start because the volume "
                            f"claim it mounts ({claim.get('name')}) is "
                            f"{claim.get('phase', 'not Bound')}."
                        ),
                        target=ResourceRef(kind="Pod", name=name, namespace=namespace),
                        evidence_ids=_evidence(
                            [edge],
                            *data.evidence_for("k8s.storage", "storage"),
                            *data.evidence_for("k8s.pods", "pods"),
                        ),
                        attributes={
                            "claim": claim.get("name", ""),
                            "claim_phase": claim.get("phase", ""),
                            "storage_class": claim.get("storage_class", ""),
                            "path": [edge.key],
                        },
                    )
                )
        return tuple(signals)


class StorageClassBlockingWorkloads:
    """One StorageClass with several unbound claims behind it.

    The difference between "three pods are broken" and "one storage class is
    broken and three pods are downstream of it" — which is the difference
    between three investigations and one fix.
    """

    id = "graph.storage_class.blocking"

    def extract(self, data) -> Sequence[Signal]:
        graph = _graph_of(data)
        if not graph.edges:
            return ()

        unbound = {
            f"persistentvolumeclaim/{claim.get('namespace', 'default')}/{claim.get('name', '')}"
            for claim in data.section("storage").get("claims", []) or []
            if claim.get("phase") != "Bound"
        }
        if len(unbound) < 2:
            # One unbound claim is a claim, not a pattern. `storage.pvc_unbound`
            # already reports it and does not need a graph to do so.
            return ()

        by_class: dict[str, list[Edge]] = {}
        for edge in graph.edges:
            if edge.relation == Relation.PROVISIONED_BY and edge.source.key in unbound:
                by_class.setdefault(edge.target.key, []).append(edge)

        signals = []
        for class_key, edges in by_class.items():
            if len(edges) < 2:
                continue
            name = class_key.split("/")[-1]
            signals.append(
                Signal(
                    id=f"{SignalType.STORAGE_CLASS_BLOCKING}:{class_key}",
                    type=SignalType.STORAGE_CLASS_BLOCKING,
                    severity=Severity.CRITICAL,
                    summary=(
                        f"{len(edges)} volume claims on StorageClass {name} are unbound, "
                        f"which points at the class rather than at any one workload."
                    ),
                    target=ResourceRef(kind="StorageClass", name=name),
                    evidence_ids=_evidence(edges, *data.evidence_for("k8s.storage", "storage")),
                    attributes={
                        "storage_class": name,
                        "claims": sorted(edge.source.name for edge in edges),
                        "path": sorted(edge.key for edge in edges),
                    },
                )
            )
        return tuple(signals)


class NodeCarryingFailures:
    """Several failing pods that share one node.

    Co-location is only visible through the graph: the pod section knows each
    pod's node, but "these particular broken pods are all on node-3, and the
    healthy ones are not" is a join nothing else performs.
    """

    id = "graph.node.carrying_failures"

    def extract(self, data) -> Sequence[Signal]:
        graph = _graph_of(data)
        if not graph.edges:
            return ()

        problematic = {
            f"pod/{entry.get('namespace', 'default')}/{entry.get('name', '')}"
            for entry in data.section("pods").get("problematic_pods", []) or []
        }
        if len(problematic) < 2:
            return ()

        by_node: dict[str, list[Edge]] = {}
        for edge in graph.edges:
            if edge.relation == Relation.SCHEDULED_ON and edge.source.key in problematic:
                by_node.setdefault(edge.target.key, []).append(edge)

        # Only interesting when the failures concentrate. Every pod being on
        # the one node of a single-node cluster is not a finding.
        placed = {edge.target.key for edge in graph.edges if edge.relation == Relation.SCHEDULED_ON}
        if len(placed) < 2:
            return ()

        signals = []
        for node_key, edges in by_node.items():
            if len(edges) < 2:
                continue
            name = node_key.split("/")[-1]
            signals.append(
                Signal(
                    id=f"{SignalType.NODE_CARRYING_FAILURES}:{node_key}",
                    type=SignalType.NODE_CARRYING_FAILURES,
                    severity=Severity.HIGH,
                    summary=(
                        f"{len(edges)} failing pods are all scheduled on node {name}, "
                        f"which is a node-level cause rather than {len(edges)} workload ones."
                    ),
                    target=ResourceRef(kind="Node", name=name),
                    evidence_ids=_evidence(edges, *data.evidence_for("k8s.pods", "pods")),
                    attributes={
                        "node": name,
                        "pods": sorted(edge.source.name for edge in edges),
                        "path": sorted(edge.key for edge in edges),
                    },
                )
            )
        return tuple(signals)


class ServiceSelectingOnlyBrokenPods:
    """A service whose every backing pod is failing.

    The network inspector reports "no ready endpoints", which is a symptom. The
    graph says which pods should have been the endpoints and that all of them
    are broken — turning a networking finding into a workload one.
    """

    id = "graph.service.all_backends_failing"

    def extract(self, data) -> Sequence[Signal]:
        graph = _graph_of(data)
        if not graph.edges:
            return ()

        problematic = {
            f"pod/{entry.get('namespace', 'default')}/{entry.get('name', '')}"
            for entry in data.section("pods").get("problematic_pods", []) or []
        }
        if not problematic:
            return ()

        by_service: dict[str, list[Edge]] = {}
        for edge in graph.edges:
            if edge.relation == Relation.SELECTS:
                by_service.setdefault(edge.source.key, []).append(edge)

        signals = []
        for service_key, edges in by_service.items():
            targets = {edge.target.key for edge in edges}
            if not targets or not targets.issubset(problematic):
                continue

            namespace, name = service_key.split("/")[1], service_key.split("/")[2]
            signals.append(
                Signal(
                    id=f"{SignalType.SERVICE_BACKENDS_FAILING}:{service_key}",
                    type=SignalType.SERVICE_BACKENDS_FAILING,
                    # The same outage `network.no_endpoints` already calls
                    # critical — a service with no healthy backend is down.
                    # This one additionally knows why, and ranking it lower
                    # would bury the better explanation under the symptom.
                    severity=Severity.CRITICAL,
                    summary=(
                        f"Service {namespace}/{name} has no healthy backend: all "
                        f"{len(targets)} pods it selects are failing, so the fault is "
                        f"in the workload rather than the service."
                    ),
                    target=ResourceRef(kind="Service", name=name, namespace=namespace),
                    evidence_ids=_evidence(
                        edges,
                        *data.evidence_for("k8s.network", "network"),
                        *data.evidence_for("k8s.pods", "pods"),
                    ),
                    attributes={
                        "service": f"{namespace}/{name}",
                        "pods": sorted(edge.target.name for edge in edges),
                        "path": sorted(edge.key for edge in edges),
                    },
                )
            )
        return tuple(signals)


GRAPH_SIGNAL_RULES: tuple = (
    PodBlockedByStorage(),
    StorageClassBlockingWorkloads(),
    NodeCarryingFailures(),
    ServiceSelectingOnlyBrokenPods(),
)
