"""The cluster as a graph, derived from evidence that was already collected.

M7's framing in the architecture doc is that the graph is "a byproduct of
collection, not a separate ingestion pipeline". Taken literally that would mean
collectors emitting edges alongside evidence — every collector touched, and a
second thing to keep correct.

The stronger reading, and the one built here, is that the graph is a
**derivation from evidence**, exactly as signals are. Collectors already fetch
`ownerReferences`, `nodeName`, volume claims, selectors and storage classes;
the edges are already in the store. Deriving rather than emitting buys three
things the platform already relies on elsewhere:

- **Reproducibility.** A graph rebuilt from a stored report is identical to the
  one built during the investigation, so a report is not a lossy snapshot.
- **Redaction and fault isolation for free.** Both happen at the collection
  boundary, and a derivation cannot get behind them.
- **No new collection path.** Nothing about how a cluster is read changes, so
  the local and agent paths cannot diverge on the graph.

Every edge carries the evidence ids it was derived from, for the same reason
every signal does: an edge nobody can trace is an assertion, and this platform
does not make those.
"""

from dataclasses import dataclass, field
from typing import Any

from app.evidence.models import ResourceRef


class Relation:
    """How one object depends on another.

    A closed set, and directional: `A -[relation]-> B` always reads "A depends
    on B" or "A is placed on B". Keeping the direction consistent is what makes
    "what does this pod depend on" a forward walk and "what breaks if this node
    goes" a reverse one, rather than two special cases.
    """

    # Workload ownership, in the direction ownerReferences point.
    OWNS = "owns"
    # A pod is scheduled onto a node.
    SCHEDULED_ON = "scheduled_on"
    # A pod mounts a claim; a claim binds a volume; a volume has a class.
    MOUNTS = "mounts"
    BINDS = "binds"
    PROVISIONED_BY = "provisioned_by"
    # A pod reads configuration.
    READS = "reads"
    # A service selects pods, and an ingress routes to a service.
    SELECTS = "selects"
    ROUTES_TO = "routes_to"
    # A pod runs under a service account.
    RUNS_AS = "runs_as"


@dataclass(frozen=True, slots=True)
class Edge:
    """One dependency, and the evidence it was read from."""

    source: ResourceRef
    relation: str
    target: ResourceRef
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            # The same rule signals follow. An edge with no provenance cannot
            # be defended when a diagnosis rests on it.
            raise ValueError(f"edge {self.key} has no evidence behind it")

    @property
    def key(self) -> str:
        return f"{self.source.key} -{self.relation}-> {self.target.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.key,
            "relation": self.relation,
            "target": self.target.key,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class ClusterGraph:
    """Objects and the dependencies between them, for one investigation.

    Bounded by construction: the node count is the number of objects the
    investigation actually collected, which the collection budget already caps.
    Traversal is depth-limited on top of that, so a cycle — which Kubernetes
    permits, via a ConfigMap referenced by the pod that generates it — cannot
    turn a page load into a hang.
    """

    nodes: dict[str, ResourceRef] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    _out: dict[str, list[Edge]] = field(default_factory=dict)
    _in: dict[str, list[Edge]] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)

    def add(self, edge: Edge) -> None:
        """Record an edge, ignoring an exact duplicate.

        Duplicates are normal rather than exceptional: two collectors can both
        observe that a pod is on a node, and the graph should say it once.
        """
        if edge.key in self._seen:
            return
        self._seen.add(edge.key)

        self.nodes.setdefault(edge.source.key, edge.source)
        self.nodes.setdefault(edge.target.key, edge.target)
        self.edges.append(edge)
        self._out.setdefault(edge.source.key, []).append(edge)
        self._in.setdefault(edge.target.key, []).append(edge)

    def out_edges(self, key: str) -> list[Edge]:
        return list(self._out.get(key, []))

    def in_edges(self, key: str) -> list[Edge]:
        return list(self._in.get(key, []))

    def depends_on(self, key: str, max_depth: int = 5) -> list[Edge]:
        """Everything `key` rests on, to `max_depth` hops.

        The question a diagnosis asks: this pod is broken, what does it need?
        """
        return self._walk(key, forward=True, max_depth=max_depth)

    def dependents(self, key: str, max_depth: int = 5) -> list[Edge]:
        """Everything that rests on `key`.

        The question an operator asks before draining a node or deleting a
        claim: what breaks if this goes away?
        """
        return self._walk(key, forward=False, max_depth=max_depth)

    def _walk(self, key: str, forward: bool, max_depth: int) -> list[Edge]:
        """Breadth-first, depth-limited, cycle-safe.

        Depth-limited because §3.6 bounds the useful traversal at three to five
        hops and an unbounded walk over a large cluster is a way to make a
        request never return. Cycle-safe because Kubernetes allows them.

        The direction is a flag rather than the step function's identity. It
        was `step is self.out_edges` first, which is always False: attribute
        access builds a new bound method each time, so a forward walk read the
        *source* of each edge, decided it had already visited it, and stopped
        after one hop. Every traversal silently returned direct neighbours
        only — the sort of bug that leaves a feature looking like it works.
        """
        found: list[Edge] = []
        visited = {key}
        frontier = [key]

        for _ in range(max(0, max_depth)):
            following: list[str] = []
            for current in frontier:
                for edge in self.out_edges(current) if forward else self.in_edges(current):
                    other = edge.target.key if forward else edge.source.key
                    found.append(edge)
                    if other not in visited:
                        visited.add(other)
                        following.append(other)
            if not following:
                break
            frontier = following

        return found

    def neighbours(self, key: str, relation: str = "") -> list[ResourceRef]:
        """Immediate targets of `key`, optionally filtered to one relation."""
        return [
            edge.target for edge in self.out_edges(key) if not relation or edge.relation == relation
        ]

    def sources(self, key: str, relation: str = "") -> list[ResourceRef]:
        """Immediate objects pointing at `key`."""
        return [
            edge.source for edge in self.in_edges(key) if not relation or edge.relation == relation
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [{"key": key, **ref.to_dict()} for key, ref in sorted(self.nodes.items())],
            "edges": [edge.to_dict() for edge in self.edges],
            "counts": {"nodes": len(self.nodes), "edges": len(self.edges)},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClusterGraph":
        """Rebuild a graph from a stored report.

        The round trip is what makes the graph reproducible rather than a view
        that existed only while the investigation was running.
        """
        graph = cls()
        refs = {
            entry["key"]: ResourceRef(
                kind=entry.get("kind", ""),
                name=entry.get("name", ""),
                namespace=entry.get("namespace"),
                uid=entry.get("uid"),
            )
            for entry in payload.get("nodes", [])
            if entry.get("key")
        }

        for entry in payload.get("edges", []):
            source = refs.get(entry.get("source", ""))
            target = refs.get(entry.get("target", ""))
            if source is None or target is None:
                continue
            graph.add(
                Edge(
                    source=source,
                    relation=entry.get("relation", ""),
                    target=target,
                    evidence_ids=tuple(entry.get("evidence_ids") or ("graph.restored",)),
                )
            )
        return graph
