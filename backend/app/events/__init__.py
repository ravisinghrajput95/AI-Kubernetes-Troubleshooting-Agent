"""Event ingress: what makes the platform autonomous rather than invoked.

`sources.py` carries the decision that matters — a source is an *identity*, not
a secret, because an alert has no human and impersonation is what keeps "the
platform cannot see more than you can" true.

`alerts.py` carries the other one: deduplication is not an optimisation.
Alertmanager re-sends, and investigating every delivery would turn one flapping
alert into an unbounded series of production cluster reads.
"""

from app.events.alerts import (
    DEFAULT_COOLDOWN_SECONDS,
    AlertTrigger,
    InMemoryTriggerLedger,
    RedisTriggerLedger,
    TriggerLedger,
    get_trigger_ledger,
    parse_alertmanager,
    set_trigger_ledger,
)
from app.events.sources import (
    SIGNATURE_TOLERANCE_SECONDS,
    EventSource,
    EventSourceError,
    get_sources,
    parse_sources,
    reset_sources,
)

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "SIGNATURE_TOLERANCE_SECONDS",
    "AlertTrigger",
    "EventSource",
    "EventSourceError",
    "InMemoryTriggerLedger",
    "RedisTriggerLedger",
    "TriggerLedger",
    "get_sources",
    "get_trigger_ledger",
    "parse_alertmanager",
    "parse_sources",
    "reset_sources",
    "set_trigger_ledger",
]
