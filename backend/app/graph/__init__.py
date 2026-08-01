"""The cluster dependency graph, derived from evidence.

M7. See `models.py` for why it is a derivation rather than a second collection
path, and `edge_rules.py` for the rule that no edge is emitted unless both of
its ends were actually observed.
"""

from app.graph.builder import build_graph
from app.graph.edge_rules import DEFAULT_EDGE_RULES, EdgeRule, GraphInput
from app.graph.models import ClusterGraph, Edge, Relation

__all__ = [
    "DEFAULT_EDGE_RULES",
    "ClusterGraph",
    "Edge",
    "EdgeRule",
    "GraphInput",
    "Relation",
    "build_graph",
]
