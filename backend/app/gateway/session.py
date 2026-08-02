"""One connected agent, and the correlation that makes one stream serve many.

A single bidirectional stream carries every investigation for a cluster, so
nothing about it is request/response. Work goes down tagged with a
`request_id`; evidence comes back tagged the same way, and this is where the
two are matched.

The direction is the whole point (ADR-004): the agent dials out, because no
customer opens an inbound port into a production cluster. That means the
platform can never *call* an agent — it can only put a message on a stream the
agent already opened, and wait.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.observability import metrics
from app.security.identity import AgentIdentity
from app.wire.gen.agent.v1 import agent_pb2, collection_pb2, evidence_pb2

# How long a single collection may take before the platform stops waiting.
# The agent enforces its own budget too; this is the platform's backstop
# against an agent that accepted work and went silent.
DEFAULT_COLLECTION_TIMEOUT = 60.0

# How often the platform pings a connected agent, and how long silence may last
# before the console stops calling it online. The gap between the two is
# deliberate: one missed heartbeat is a slow network, three is a problem.
AGENT_HEARTBEAT_SECONDS = 15.0
AGENT_STALE_SECONDS = 45.0


@dataclass
class PendingCollection:
    """Records for one request, and the signal that they are all in."""

    request_id: str
    expected: int
    records: list[evidence_pb2.EvidenceRecord] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    detail: str = ""

    def add(self, record: evidence_pb2.EvidenceRecord) -> None:
        self.records.append(record)

    def finish(self, detail: str = "") -> None:
        self.detail = detail
        self.done.set()


class AgentSession:
    """The platform's handle on one connected agent.

    Constructed from an `AgentIdentity`, not from anything the agent said about
    itself. Under mTLS that identity was read out of the peer certificate
    before the first message was accepted, which is what makes `cluster_id`
    here a fact rather than a claim.
    """

    def __init__(
        self,
        identity: AgentIdentity,
        hello: agent_pb2.AgentHello,
        closed: asyncio.Event | None = None,
    ) -> None:
        self.identity = identity
        self.hello = hello
        self.outbound: asyncio.Queue[agent_pb2.PlatformMessage] = asyncio.Queue()
        # Set when the stream must end for a reason the stream itself has not
        # noticed — today, only revocation. Created by whoever owns the RPC so
        # it can be waited on before the session exists.
        self.closed = closed or asyncio.Event()
        self.termination_reason = ""
        self._pending: dict[str, PendingCollection] = {}
        self._counter = 0
        self.connected_at = datetime.now(UTC)
        # Refreshed by every inbound message, including the heartbeat reply.
        # An open TCP connection is not proof an agent is alive — a half-open
        # stream looks identical to a healthy idle one from this side — so
        # liveness is "we heard from it recently", not "the socket exists".
        self.last_seen = self.connected_at
        self.degradation = ""

    @property
    def cluster_id(self) -> str:
        """The cluster this session speaks for.

        A property rather than a field so the identity stays the single source
        of it — the registry, the provider and the engine all read this and
        none of them needs to know a certificate was involved.
        """
        return self.identity.cluster_id

    @property
    def tenant(self) -> str:
        """The organisation this agent belongs to, read from its certificate."""
        return self.identity.tenant

    @property
    def key(self) -> tuple[str, str]:
        """How the registry addresses this session.

        Tenant first. Two customers may both call a cluster `prod`, and keying
        on the cluster id alone would let whichever connected second evict the
        first — and let either reach the other's evidence.
        """
        return (self.identity.tenant, self.cluster_id)

    @property
    def certificate_serial(self) -> str:
        """Empty on the plaintext development path, where there is no certificate."""
        return self.identity.serial

    @property
    def supported_kinds(self) -> frozenset[str]:
        """What this agent can collect.

        The platform plans against this rather than assuming a uniform fleet,
        so an agent two releases behind serves fewer kinds instead of failing.
        """
        return frozenset(self.hello.supported_kinds)

    def next_request_id(self) -> str:
        self._counter += 1
        return f"{self.cluster_id}-{self._counter}"

    async def collect(
        self,
        specs: list[collection_pb2.EvidenceSpec],
        investigation_id: str = "",
        actor: collection_pb2.Impersonation | None = None,
        budget: collection_pb2.Budget | None = None,
        timeout: float = DEFAULT_COLLECTION_TIMEOUT,
    ) -> PendingCollection:
        """Send work down the stream and wait for the records to come back."""
        request_id = self.next_request_id()
        pending = PendingCollection(request_id=request_id, expected=len(specs))
        self._pending[request_id] = pending

        request = collection_pb2.CollectionRequest(
            investigation_id=investigation_id,
            request_id=request_id,
            specs=specs,
        )
        if actor is not None:
            request.actor.CopyFrom(actor)
        if budget is not None:
            request.budget.CopyFrom(budget)

        await self.outbound.put(agent_pb2.PlatformMessage(collect=request))

        try:
            await asyncio.wait_for(pending.done.wait(), timeout=timeout)
        except TimeoutError:
            # Not an error to raise: a silent agent is a gap in the evidence,
            # and the layers above already know how to report a gap.
            pending.detail = "The agent did not answer within the deadline."
            logger.warning(
                "Collection {request} on {cluster} timed out",
                request=request_id,
                cluster=self.cluster_id,
            )
        finally:
            self._pending.pop(request_id, None)

        return pending

    def touch(self, degradation: str = "") -> None:
        """Record that the agent just spoke."""
        self.last_seen = datetime.now(UTC)
        self.degradation = degradation

    @property
    def seconds_since_seen(self) -> float:
        return (datetime.now(UTC) - self.last_seen).total_seconds()

    def online(self, stale_after: float = AGENT_STALE_SECONDS) -> bool:
        """Whether this agent counts as reachable right now."""
        return self.seconds_since_seen <= stale_after

    def on_evidence(self, record: evidence_pb2.EvidenceRecord, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            # Late or unsolicited: dropped rather than guessed at. Attributing
            # it to another request would corrupt an unrelated investigation.
            logger.debug("Evidence for unknown request {request}", request=request_id)
            return
        pending.add(record)

    def on_done(self, done: collection_pb2.CollectionDone) -> None:
        pending = self._pending.get(done.request_id)
        if pending is not None:
            pending.finish(done.detail)

    def cancel_all(self, reason: str) -> None:
        for pending in self._pending.values():
            pending.finish(reason)
        self._pending.clear()

    def terminate(self, reason: str) -> None:
        """Ask the stream to end, from outside the stream.

        Revocation is the reason this exists. The transport's defining property
        is a connection that stays open for weeks, so a revocation that only
        took effect at the next reconnect would be close to meaningless.
        """
        self.termination_reason = reason
        self.cancel_all(reason)
        self.closed.set()

    def describe(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "tenant": self.tenant,
            "online": self.online(),
            "connected_at": self.connected_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "seconds_since_seen": round(self.seconds_since_seen, 1),
            "degradation": self.degradation,
            "identity_source": self.identity.source,
            "certificate_serial": self.identity.serial,
            "certificate_expires_at": (
                self.identity.expires_at.isoformat() if self.identity.expires_at else ""
            ),
            "agent_version": self.hello.agent_version,
            "kubernetes_version": self.hello.kubernetes_version,
            "supported_kinds": sorted(self.supported_kinds),
            "available_backends": sorted(self.hello.available_backends),
            "protocol_version": self.hello.protocol_version,
        }


class AgentRegistry:
    """Which clusters currently have an agent connected.

    Deliberately in memory and per-process: a stream belongs to the process
    holding the socket, so a registry shared through Postgres would list agents
    this worker cannot actually reach. Routing a request to the worker that
    holds the stream is M8's problem, not this one's.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], AgentSession] = {}

    def register(self, session: AgentSession) -> None:
        existing = self._sessions.get(session.key)
        if existing is not None:
            existing.cancel_all("The agent reconnected.")
        self._sessions[session.key] = session
        metrics.agents(len(self._sessions))
        self._announce(session)
        logger.info(
            "Agent connected for cluster {cluster} (tenant {tenant})",
            cluster=session.cluster_id,
            tenant=session.tenant,
        )

    def unregister(self, session: AgentSession) -> None:
        if self._sessions.get(session.key) is session:
            del self._sessions[session.key]
            metrics.agents(len(self._sessions))
        session.cancel_all("The agent disconnected.")
        self._withdraw(session)
        logger.info(
            "Agent disconnected for cluster {cluster} (tenant {tenant})",
            cluster=session.cluster_id,
            tenant=session.tenant,
        )

    def _announce(self, session: AgentSession) -> None:
        from app.gateway.presence import get_agent_presence

        presence = get_agent_presence()
        if presence is not None:
            presence.announce(session)

    def _withdraw(self, session: AgentSession) -> None:
        from app.gateway.presence import get_agent_presence

        presence = get_agent_presence()
        if presence is not None:
            presence.withdraw(session)

    def refresh(self, session: AgentSession) -> None:
        """Re-announce a session that has just proved it is alive."""
        self._announce(session)

    def get(self, cluster_id: str, tenant: str | None = None) -> AgentSession | None:
        """The agent for a cluster, within a tenant.

        The tenant defaults to whichever one the caller is running as, so a
        handler cannot reach another tenant's agent by forgetting to pass it —
        the same reasoning as the ambient tenant on the database.
        """
        from app.tenancy import current_tenant

        return self._sessions.get((tenant or current_tenant(), cluster_id))

    def sessions(self, tenant: str | None = None) -> list[AgentSession]:
        """Every session belonging to one tenant, defaulting to the caller's.

        There is deliberately no "all tenants" branch here. The one thing that
        legitimately spans tenants — the revocation sweep — reads `_sessions`
        directly, because a revoked certificate is revoked regardless of who
        was looking. Giving this method an escape hatch would mean every future
        caller had to be trusted to not use it.
        """
        from app.tenancy import current_tenant

        scope = tenant or current_tenant()
        return [session for session in self._sessions.values() if session.tenant == scope]

    def terminate_revoked(self, revoked_serials: set[str], reason: str) -> list[AgentSession]:
        """End every live session whose certificate has since been revoked.

        Returns what it ended, so the caller can log it rather than this having
        an opinion about logging.
        """
        if not revoked_serials:
            return []
        # Revocation sweeps every tenant: it is infrastructure, and a revoked
        # certificate is revoked regardless of who was looking.
        doomed = [
            session
            for session in self._sessions.values()
            if session.certificate_serial and session.certificate_serial in revoked_serials
        ]
        for session in doomed:
            session.terminate(reason)
        return doomed

    def clusters(self) -> list[dict[str, Any]]:
        """What the caller's tenant can see."""
        return [session.describe() for session in self.sessions()]


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry
