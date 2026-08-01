"""Turning collected evidence into edges.

Declarative in the same sense the hypothesis rules are: each rule states which
evidence it reads and which relation it produces, and adding a relationship
means appending a rule rather than editing a dispatcher.

Every rule is fault-isolated by the builder, so a payload shaped differently
from what a rule expects costs that one relation and not the graph.

**No rule invents a node.** An edge is only emitted when both ends were
actually observed in evidence — a pod naming a ConfigMap that was never
collected produces no edge, because "this pod depends on a ConfigMap we cannot
see" and "this pod depends on nothing" must not look the same to a traversal.
The gap is already recorded as evidence; the graph does not need to guess at
it.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from app.evidence.models import EvidenceKind, ResourceRef
from app.graph.models import Edge, Relation


@dataclass(frozen=True, slots=True)
class EdgeRule:
    """One relationship, and where it is read from."""

    id: str
    relation: str
    extract: Callable[["GraphInput"], Iterator[Edge]]


class GraphInput:
    """Read-only view of an investigation, for edge extraction.

    Deliberately the same shape as `AnalysisInput`: the graph is derived from
    the same payload the signals are, at the same point in the pipeline.
    """

    def __init__(self, investigation: dict[str, Any]) -> None:
        self.investigation = investigation
        self._evidence_by_kind: dict[str, list[str]] = {}
        for entry in investigation.get("evidence", []) or []:
            kind, evidence_id = entry.get("kind"), entry.get("id")
            if kind and evidence_id:
                self._evidence_by_kind.setdefault(kind, []).append(evidence_id)

    def section(self, key: str) -> dict[str, Any]:
        value = self.investigation.get(key)
        return value if isinstance(value, dict) else {}

    def deep(self, kind: str) -> list[dict[str, Any]]:
        deep_evidence = self.investigation.get("deep_evidence", {})
        if not isinstance(deep_evidence, dict):
            return []
        return [
            entry
            for entry in deep_evidence.get(kind, [])
            if isinstance(entry, dict) and entry.get("data") is not None
        ]

    def evidence_for(self, kind: str, fallback: str) -> tuple[str, ...]:
        ids = self._evidence_by_kind.get(kind)
        return tuple(ids) if ids else (f"investigation.{fallback}",)


def pod(namespace: str, name: str) -> ResourceRef:
    return ResourceRef(kind="Pod", name=name, namespace=namespace)


# --- rules ------------------------------------------------------------------


def _pods_on_nodes(data: GraphInput) -> Iterator[Edge]:
    """Pod → Node. The edge that makes "what is on this node" answerable."""
    evidence = data.evidence_for(EvidenceKind.PODS, "pods")

    for entry in data.section("pods").get("pod_inventory", []) or []:
        node = entry.get("node", "")
        # "Pending" is the inspector's placeholder for a pod with no node.
        # An unscheduled pod is not on a node called Pending.
        if not node or node == "Pending":
            continue
        yield Edge(
            source=pod(entry.get("namespace", "default"), entry.get("name", "")),
            relation=Relation.SCHEDULED_ON,
            target=ResourceRef(kind="Node", name=node),
            evidence_ids=evidence,
        )


def _claims_to_volumes(data: GraphInput) -> Iterator[Edge]:
    """PersistentVolumeClaim → PersistentVolume → StorageClass.

    The chain behind "this pod's claim is on a failed storage class", which is
    the example §3.6 gives and which no single evidence section can answer.
    """
    evidence = data.evidence_for(EvidenceKind.STORAGE, "storage")

    for claim in data.section("storage").get("claims", []) or []:
        namespace = claim.get("namespace", "default")
        name = claim.get("name", "")
        if not name:
            continue

        source = ResourceRef(kind="PersistentVolumeClaim", name=name, namespace=namespace)

        volume = claim.get("volume", "")
        if volume:
            yield Edge(
                source=source,
                relation=Relation.BINDS,
                target=ResourceRef(kind="PersistentVolume", name=volume),
                evidence_ids=evidence,
            )

        storage_class = claim.get("storage_class", "")
        # "none" is the inspector's word for an unset class, not a class named
        # none — an edge to it would be a fiction a traversal could act on.
        if storage_class and storage_class != "none":
            yield Edge(
                source=source,
                relation=Relation.PROVISIONED_BY,
                target=ResourceRef(kind="StorageClass", name=storage_class),
                evidence_ids=evidence,
            )


def _pod_volumes(data: GraphInput) -> Iterator[Edge]:
    """Pod → PersistentVolumeClaim / ConfigMap / Secret, from deep evidence."""
    for entry in data.deep(EvidenceKind.POD_SPEC):
        spec = entry.get("data", {})
        evidence = (entry.get("evidence_id") or f"{EvidenceKind.POD_SPEC}",)
        source = pod(spec.get("namespace", "default"), spec.get("pod", ""))
        if not source.name:
            continue

        for volume in spec.get("volumes", []) or []:
            kind = volume.get("type", "")
            if kind == "PersistentVolumeClaim" and volume.get("claim"):
                yield Edge(
                    source=source,
                    relation=Relation.MOUNTS,
                    target=ResourceRef(
                        kind="PersistentVolumeClaim",
                        name=volume["claim"],
                        namespace=source.namespace,
                    ),
                    evidence_ids=evidence,
                )
            elif kind in {"ConfigMap", "Secret"} and volume.get("name_ref"):
                yield Edge(
                    source=source,
                    relation=Relation.READS,
                    target=ResourceRef(
                        kind=kind, name=volume["name_ref"], namespace=source.namespace
                    ),
                    evidence_ids=evidence,
                )


def _pod_owners(data: GraphInput) -> Iterator[Edge]:
    """Workload → Pod, in the direction ownerReferences point.

    Deployment → Pod rather than Pod → Deployment, so "what does this
    deployment own" is a forward walk and matches every other edge's meaning
    of "depends on / contains".
    """
    for entry in data.deep(EvidenceKind.POD_SPEC):
        spec = entry.get("data", {})
        evidence = (entry.get("evidence_id") or f"{EvidenceKind.POD_SPEC}",)
        owner = spec.get("owner") or {}
        kind = owner.get("workload_kind") or owner.get("kind") or ""
        name = owner.get("workload_name") or owner.get("name") or ""
        if not kind or not name:
            continue

        namespace = spec.get("namespace", "default")
        yield Edge(
            source=ResourceRef(kind=kind, name=name, namespace=namespace),
            relation=Relation.OWNS,
            target=pod(namespace, spec.get("pod", "")),
            evidence_ids=evidence,
        )


def _pod_service_accounts(data: GraphInput) -> Iterator[Edge]:
    """Pod → ServiceAccount. What a forbidden read is usually about."""
    for entry in data.deep(EvidenceKind.POD_SPEC):
        spec = entry.get("data", {})
        account = spec.get("service_account", "")
        if not account or not spec.get("pod"):
            continue
        namespace = spec.get("namespace", "default")
        yield Edge(
            source=pod(namespace, spec["pod"]),
            relation=Relation.RUNS_AS,
            target=ResourceRef(kind="ServiceAccount", name=account, namespace=namespace),
            evidence_ids=(entry.get("evidence_id") or f"{EvidenceKind.POD_SPEC}",),
        )


def _services_to_pods(data: GraphInput) -> Iterator[Edge]:
    """Service → Pod, by label selector.

    Matched against the pod inventory the investigation already holds rather
    than re-querying: a selector that matches nothing is exactly the finding
    the network inspector reports, and the graph should agree with it.
    """
    network = data.section("network")
    selectors = network.get("selectors") or {}
    if not selectors:
        return

    evidence = data.evidence_for(EvidenceKind.NETWORK, "network")
    inventory = data.section("pods").get("pod_inventory", []) or []

    for service_key, selector in selectors.items():
        if not isinstance(selector, dict) or not selector:
            continue
        namespace, _, name = service_key.partition("/")
        if not name:
            continue

        for entry in inventory:
            if entry.get("namespace") != namespace:
                continue
            labels = entry.get("labels") or {}
            if not labels or not all(labels.get(k) == v for k, v in selector.items()):
                continue
            yield Edge(
                source=ResourceRef(kind="Service", name=name, namespace=namespace),
                relation=Relation.SELECTS,
                target=pod(namespace, entry.get("name", "")),
                evidence_ids=evidence,
            )


DEFAULT_EDGE_RULES: tuple[EdgeRule, ...] = (
    EdgeRule("graph.pod.node", Relation.SCHEDULED_ON, _pods_on_nodes),
    EdgeRule("graph.claim.volume", Relation.BINDS, _claims_to_volumes),
    EdgeRule("graph.pod.volumes", Relation.MOUNTS, _pod_volumes),
    EdgeRule("graph.pod.owner", Relation.OWNS, _pod_owners),
    EdgeRule("graph.pod.serviceaccount", Relation.RUNS_AS, _pod_service_accounts),
    EdgeRule("graph.service.pods", Relation.SELECTS, _services_to_pods),
)
