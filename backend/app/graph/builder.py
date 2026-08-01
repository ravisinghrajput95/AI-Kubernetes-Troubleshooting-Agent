"""Building the graph, with one broken rule costing one relation."""

from collections.abc import Sequence

from loguru import logger

from app.graph.edge_rules import DEFAULT_EDGE_RULES, EdgeRule, GraphInput
from app.graph.models import ClusterGraph


def build_graph(
    investigation: dict,
    rules: Sequence[EdgeRule] | None = None,
) -> ClusterGraph:
    """Derive the dependency graph from a completed investigation payload.

    Fault-isolated per rule, on the same reasoning as the signal and hypothesis
    loops: a payload shaped differently from what one rule expects should cost
    that relation, not the graph. A diagnosis with a partial graph is still
    worth having; a diagnosis that failed because a selector was a string
    rather than a dict is not.
    """
    graph = ClusterGraph()
    data = GraphInput(investigation)

    for rule in rules if rules is not None else DEFAULT_EDGE_RULES:
        try:
            for edge in rule.extract(data):
                graph.add(edge)
        except Exception as exc:
            logger.opt(exception=exc).error("Edge rule {id} failed", id=rule.id)
            continue

    if graph.edges:
        logger.info(
            "Graph derived: {nodes} object(s), {edges} dependency(ies)",
            nodes=len(graph.nodes),
            edges=len(graph.edges),
        )
    return graph
