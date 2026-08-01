"""Which agents the *fleet* has, not which ones this replica happens to hold.

`AgentRegistry` is per-process by necessity: a gRPC stream belongs to whichever
worker holds the socket, and no amount of shared state changes that. That was
fine while the console and the gateway were the same process.

On a managed deployment they are not. Three replicas behind a load balancer,
thirty connected clusters, and `GET /agents` answers from whichever pod the
balancer picked — so the console shows roughly a third of the fleet, and a
different third on the next refresh. Nothing errors. The page just quietly
under-reports, which is worse than failing.

So each gateway announces its agents into Redis with a short TTL and refreshes
them on every heartbeat. The API reads the union. Three properties fall out of
using expiry rather than explicit deregistration:

- a worker that is killed leaves no phantom agents, because its keys lapse;
- an agent that stops answering stops being refreshed, so presence agrees with
  the heartbeat rather than contradicting it;
- no cleanup path has to be correct on the unhappy path, which is where cleanup
  paths are wrong.

**Presence is visibility, not routing.** Knowing that another worker holds an
agent does not let this worker collect through it — that needs the request
routed to the socket, which is M8. Every record says which worker holds it and
whether that is this one, and the console says so rather than implying an
investigation can be started anywhere.
"""

import json
from typing import Any

from loguru import logger

# Long enough to survive a missed heartbeat, short enough that a dead worker's
# agents disappear before anyone acts on them. Three heartbeats.
PRESENCE_TTL_SECONDS = 45


class AgentPresence:
    """Fleet-wide agent visibility, backed by Redis key expiry."""

    def __init__(self, bus, worker_id: str) -> None:
        self._bus = bus
        self._worker = worker_id

    def _key(self, tenant: str, cluster_id: str) -> str:
        return f"{self._bus.prefix}:agents:{tenant}:{cluster_id}"

    def announce(self, session) -> None:
        """Publish, or refresh, one agent's presence."""
        record = {**session.describe(), "worker": self._worker}
        try:
            self._bus.set_expiring(
                self._key(session.tenant, session.cluster_id),
                json.dumps(record),
                PRESENCE_TTL_SECONDS,
            )
        except Exception as exc:  # pragma: no cover - visibility must not break collection
            # A console that under-reports is bad; an investigation that fails
            # because the console's index was unavailable is worse.
            logger.debug("Could not announce agent presence: {error}", error=exc)

    def withdraw(self, session) -> None:
        try:
            self._bus.delete(self._key(session.tenant, session.cluster_id))
        except Exception as exc:  # pragma: no cover
            logger.debug("Could not withdraw agent presence: {error}", error=exc)

    def fleet(self, tenant: str) -> list[dict[str, Any]]:
        """Every agent this tenant has, across every worker."""
        try:
            raw = self._bus.scan_values(f"{self._bus.prefix}:agents:{tenant}:*")
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not read agent presence: {error}", error=exc)
            return []

        records = []
        for value in raw:
            try:
                record = json.loads(value)
            except (TypeError, ValueError):
                continue
            record["local"] = record.get("worker") == self._worker
            records.append(record)
        return sorted(records, key=lambda item: item.get("cluster_id", ""))


_presence: AgentPresence | None = None


def set_agent_presence(presence: AgentPresence | None) -> None:
    global _presence
    _presence = presence


def get_agent_presence() -> AgentPresence | None:
    """The fleet index, or None in a single-process deployment.

    None is the correct answer rather than a stub: with one process the local
    registry *is* the fleet, and pretending otherwise would add a Redis
    dependency to the getting-started path.
    """
    return _presence
