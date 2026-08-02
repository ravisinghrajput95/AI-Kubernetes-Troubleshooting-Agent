"""Turning an alert into an investigation request, and not doing it twice.

Alertmanager's webhook is the first shape supported because it is what a
Kubernetes operator already has. The parser is deliberately tolerant: an alert
carries whatever labels a customer's rules attach, and a trigger that only
worked for one label convention would not survive contact with a second
customer.

**Deduplication is not an optimisation here, it is the difference between a
feature and a self-inflicted outage.** Alertmanager re-sends every firing group
on its `repeat_interval` — commonly every few hours, often every few minutes —
and a grouped notification arrives again whenever the group's membership
changes. Investigating on each delivery would have a single flapping alert
issue an unbounded series of production cluster reads. So a fingerprint that
has already triggered is refused for a cooldown window.

The cooldown is *per fingerprint*, not per alert name: Alertmanager's
fingerprint is derived from the full label set, so two namespaces alerting on
the same rule are two investigations, and the same namespace alerting twice is
one.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from loguru import logger

# Long enough that a flapping alert cannot drive a loop, short enough that a
# genuine recurrence within a shift is still investigated. Alertmanager's
# default `repeat_interval` is 4h; this deliberately sits below it so a
# deliberate re-notification is honoured while a burst is not.
DEFAULT_COOLDOWN_SECONDS = 1800

# Labels that name a cluster, in the order they are trusted. `cluster` is the
# Prometheus convention for a federated setup; the rest are what the common
# Helm charts emit.
CLUSTER_LABELS = ("cluster", "cluster_name", "kubernetes_cluster", "k8s_cluster")
NAMESPACE_LABELS = ("namespace", "exported_namespace", "kubernetes_namespace")


@dataclass(frozen=True, slots=True)
class AlertTrigger:
    """One alert, reduced to what an investigation needs."""

    fingerprint: str
    cluster: str
    namespace: str
    alert_name: str
    severity: str

    def describe(self) -> str:
        target = f"{self.cluster}/{self.namespace}" if self.namespace else self.cluster
        return f"{self.alert_name} on {target}"


def _first_label(labels: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(labels.get(name) or "").strip()
        if value:
            return value
    return ""


def parse_alertmanager(payload: dict[str, Any]) -> list[AlertTrigger]:
    """Firing alerts from an Alertmanager webhook body.

    Resolved alerts are dropped rather than investigated: the interesting
    moment has passed, and reading a production cluster to explain something
    that has already stopped is work nobody asked for.
    """
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return []

    common = payload.get("commonLabels") if isinstance(payload.get("commonLabels"), dict) else {}

    triggers: list[AlertTrigger] = []
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue

        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        merged = {**common, **labels}

        cluster = _first_label(merged, CLUSTER_LABELS)
        if not cluster:
            # No cluster means nothing to investigate. Guessing "the current
            # kubeconfig context" would let an alert from one cluster start an
            # investigation of another.
            continue

        fingerprint = str(alert.get("fingerprint") or "").strip()
        if not fingerprint:
            # Alertmanager always sends one; anything else that adopts this
            # shape may not, and a stable fallback is what keeps deduplication
            # working rather than silently disabled.
            fingerprint = hashlib.sha256(repr(sorted(merged.items())).encode()).hexdigest()[:32]

        triggers.append(
            AlertTrigger(
                fingerprint=fingerprint,
                cluster=cluster,
                namespace=_first_label(merged, NAMESPACE_LABELS),
                alert_name=str(merged.get("alertname") or "alert"),
                severity=str(merged.get("severity") or ""),
            )
        )

    return triggers


@runtime_checkable
class TriggerLedger(Protocol):
    def claim(self, key: str, cooldown_seconds: int) -> bool:
        """True when this is the first claim for `key` within the window."""
        ...


class InMemoryTriggerLedger:
    """The single-process default, where one process is the whole fleet."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, cooldown_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            if len(self._seen) > 10_000:
                self._seen = {
                    entry: at for entry, at in self._seen.items() if now - at < cooldown_seconds
                }
            last = self._seen.get(key)
            if last is not None and now - last < cooldown_seconds:
                return False
            self._seen[key] = now
            return True


class RedisTriggerLedger:
    """One ledger for the fleet.

    Without this, three replicas behind a load balancer would each investigate
    the same alert — deduplication that is per worker is not deduplication, in
    the same way a per-worker rate limit is not a limit.

    `SET NX EX` is the claim: exactly one caller creates the key, and it
    expires on its own so no sweep has to be correct on the unhappy path.
    """

    def __init__(self, bus) -> None:
        self._bus = bus

    def claim(self, key: str, cooldown_seconds: int) -> bool:
        try:
            return self._bus.set_if_absent(
                f"{self._bus.prefix}:events:seen:{key}", cooldown_seconds
            )
        except Exception as exc:
            # Fail **closed** here, unlike the rate limiter. A missed
            # deduplication turns one alert into an unbounded series of
            # production cluster reads; a missed investigation is one alert an
            # operator still sees in their own alerting. The asymmetry is
            # deliberate: the expensive mistake is the one to avoid.
            logger.warning("Trigger ledger unavailable, skipping alert: {error}", error=exc)
            return False


_ledger: TriggerLedger | None = None


def set_trigger_ledger(ledger: TriggerLedger | None) -> None:
    global _ledger
    _ledger = ledger


def get_trigger_ledger() -> TriggerLedger:
    global _ledger
    if _ledger is None:
        _ledger = InMemoryTriggerLedger()
    return _ledger
