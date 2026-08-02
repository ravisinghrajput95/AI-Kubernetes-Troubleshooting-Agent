"""Self-observability.

The platform's own metrics, chosen from `docs/PERFORMANCE_ENVELOPE.md`: every
number that document tells an operator to act on has a series here.

`metrics.py` documents the one rule that shapes the whole module — no cluster,
tenant, namespace, user or investigation id is ever a label — and why
cardinality and disclosure both demand it.
"""

from app.observability.metrics import REGISTRY, render

__all__ = ["REGISTRY", "render"]
