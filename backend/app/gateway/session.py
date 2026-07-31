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
from typing import Any

from loguru import logger

from app.wire.gen.agent.v1 import agent_pb2, collection_pb2, evidence_pb2

# How long a single collection may take before the platform stops waiting.
# The agent enforces its own budget too; this is the platform's backstop
# against an agent that accepted work and went silent.
DEFAULT_COLLECTION_TIMEOUT = 60.0


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
    """The platform's handle on one connected agent."""

    def __init__(self, cluster_id: str, hello: agent_pb2.AgentHello) -> None:
        self.cluster_id = cluster_id
        self.hello = hello
        self.outbound: asyncio.Queue[agent_pb2.PlatformMessage] = asyncio.Queue()
        self._pending: dict[str, PendingCollection] = {}
        self._counter = 0
        self.connected_at = asyncio.get_event_loop().time()

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

    def describe(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
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
        self._sessions: dict[str, AgentSession] = {}

    def register(self, session: AgentSession) -> None:
        existing = self._sessions.get(session.cluster_id)
        if existing is not None:
            existing.cancel_all("The agent reconnected.")
        self._sessions[session.cluster_id] = session
        logger.info("Agent connected for cluster {cluster}", cluster=session.cluster_id)

    def unregister(self, session: AgentSession) -> None:
        if self._sessions.get(session.cluster_id) is session:
            del self._sessions[session.cluster_id]
        session.cancel_all("The agent disconnected.")
        logger.info("Agent disconnected for cluster {cluster}", cluster=session.cluster_id)

    def get(self, cluster_id: str) -> AgentSession | None:
        return self._sessions.get(cluster_id)

    def clusters(self) -> list[dict[str, Any]]:
        return [session.describe() for session in self._sessions.values()]


_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    return _registry
