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

**Presence became routing in M8a.** It was visibility only: knowing another
worker held an agent did not let this worker collect through it, so
`select_provider` fell back to the local kubeconfig and roughly two thirds of
agent-cluster investigations on a three-replica deployment were answered by the
platform's own kubeconfig instead of the agent. `holder()` is what closed that
— the submit path asks who holds the stream and queues the work there.

The record is still a *hint*. A job routed to a worker that has since died is
re-offered to the shared queue by the reaper, and the claim is still the
conditional UPDATE, so nothing here can make two workers run one investigation.
"""

import json
from typing import Any

from loguru import logger

# Long enough to survive a missed heartbeat, short enough that a dead worker's
# agents disappear before anyone acts on them. Three heartbeats.
#
# **It must stay below `UNCLAIMED_GRACE_SECONDS`** (`app/jobs/consumer.py`), and
# that inequality is what makes routing recovery terminate rather than loop.
# A job routed to a worker that dies sits on that worker's queue until the
# reaper re-offers it after the grace period; if presence outlived the grace,
# the re-offer would route it straight back to the same dead worker, forever.
# Because the record lapses first, the re-offer goes to the shared queue and
# whoever picks it up either holds the stream or refuses honestly.
# `tests/test_agent_routing.py` asserts the ordering rather than trusting this
# comment.
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

    @property
    def worker_id(self) -> str:
        """This worker's routing identity.

        Exposed because `agent_affinity` needs to pin a job to *this* worker
        when the stream is held here — the one case `holder()` deliberately
        refuses to answer, since for its other caller a record naming us is a
        stale record rather than a destination.
        """
        return self._worker

    def holder(self, tenant: str, cluster_id: str) -> str | None:
        """Which worker holds the agent for this cluster, if any does.

        A single `GET` on the key the announcing worker wrote, rather than a
        scan of the tenant's agents: this runs on the submit path of every
        investigation, and scanning a thousand keys to answer a question about
        one of them would make the fleet's size the cost of starting any
        investigation at all.

        `None` covers "no agent" and "the record lapsed", deliberately without
        distinguishing them. Both mean the same thing to the caller — there is
        nobody to route to — and a lapsed record is a worker that stopped
        heartbeating, which is exactly when routing to it would be wrong.
        """
        if not cluster_id:
            return None
        try:
            raw = self._bus.get(self._key(tenant, cluster_id))
        except Exception as exc:  # pragma: no cover - routing must degrade, not fail
            # Falling back to the shared queue costs a possible fallback to the
            # local kubeconfig, which `select_provider` refuses rather than
            # gets wrong. Failing the submission instead would make an
            # unreachable Redis key an outage.
            logger.debug("Could not read the agent holder: {error}", error=exc)
            return None

        if not raw:
            return None
        try:
            worker = str(json.loads(raw).get("worker") or "")
        except (TypeError, ValueError):
            return None

        if not worker or worker == self._worker:
            # Naming ourselves is a stale record, not a destination. Callers
            # consult this only *after* the local registry has said no, so a
            # record still claiming this worker means the agent disconnected
            # here within the TTL. Returning it would route work to a queue we
            # are already draining, and would make `select_provider` refuse
            # with "attached to worker-a, not this one" — where worker-a is
            # this one. Both are worse than falling back to the kubeconfig.
            return None
        return worker

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
