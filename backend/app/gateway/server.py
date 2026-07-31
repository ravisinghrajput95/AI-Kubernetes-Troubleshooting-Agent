"""The agent gateway: a gRPC server that agents dial into.

Stateless with respect to investigations. It terminates the stream, resolves
the peer to a cluster, and bridges messages to and from an `AgentSession`. It
holds no investigation state, which is what lets it scale horizontally and lets
any gateway serve any agent.

**Registration and mTLS are not implemented here yet.** M4a carries a bootstrap
token on the stream so the transport can be proven end to end; ADR-005's
certificate identity is M4b. The gap is deliberate and narrow, and it is stated
in the log line at startup rather than left for someone to discover.
"""

import asyncio
from collections.abc import AsyncIterator

import grpc
from loguru import logger

from app.core.config import settings
from app.gateway.session import AgentRegistry, AgentSession, get_agent_registry
from app.wire.gen.agent.v1 import agent_pb2, agent_pb2_grpc


class AgentGatewayService(agent_pb2_grpc.AgentGatewayServicer):
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def Connect(
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.PlatformMessage]:
        """One long-lived stream, opened by the agent.

        The first message must be `hello`; anything else is refused. That is
        what makes the session's cluster identity a fact established before any
        work is accepted, rather than something inferred from later traffic.
        """
        if not self._authorised(context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid bootstrap token")
            return

        session: AgentSession | None = None
        pump: asyncio.Task | None = None

        try:
            async for message in request_iterator:
                kind = message.WhichOneof("payload")

                if session is None:
                    if kind != "hello":
                        await context.abort(
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "The first message on a stream must be hello",
                        )
                        return
                    session = AgentSession(message.hello.cluster_id, message.hello)
                    self._registry.register(session)
                    # Outbound is a separate task: reading the inbound stream
                    # must not block on there being work to send.
                    pump = asyncio.create_task(self._pump(session, context))
                    continue

                if kind == "evidence":
                    session.on_evidence(message.evidence.record, message.evidence.request_id)
                elif kind == "done":
                    session.on_done(message.done)
                elif kind == "health":
                    if message.health.degradation:
                        logger.warning(
                            "Agent {cluster} degraded: {detail}",
                            cluster=session.cluster_id,
                            detail=message.health.degradation,
                        )
                elif kind == "hello":
                    logger.debug("Ignoring a second hello on an established stream")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.opt(exception=exc).warning("Agent stream failed")
        finally:
            if pump is not None:
                pump.cancel()
            if session is not None:
                self._registry.unregister(session)

    async def _pump(
        self,
        session: AgentSession,
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Drain queued work onto the stream until the connection ends."""
        while True:
            message = await session.outbound.get()
            await context.write(message)

    def _authorised(self, context: grpc.aio.ServicerContext) -> bool:
        expected = settings.agent_bootstrap_token
        if not expected:
            # No token configured: local development, and the startup log says
            # so. Refusing outright would make the default path unusable.
            return True
        for key, value in context.invocation_metadata():
            if key == "x-agent-token" and value == expected:
                return True
        return False


class AgentGateway:
    """Lifecycle for the gRPC server, owned by application startup."""

    def __init__(self, port: int, registry: AgentRegistry | None = None) -> None:
        self._port = port
        self._registry = registry or get_agent_registry()
        self._server: grpc.aio.Server | None = None

    async def start(self) -> int:
        server = grpc.aio.server()
        agent_pb2_grpc.add_AgentGatewayServicer_to_server(
            AgentGatewayService(self._registry), server
        )
        # Port 0 asks the OS for a free one, which is what the tests use.
        bound = server.add_insecure_port(f"0.0.0.0:{self._port}")
        await server.start()
        self._server = server

        logger.info(
            "Agent gateway listening on {port} — plaintext, bootstrap-token auth. "
            "mTLS agent identity (ADR-005) arrives with M4b; do not expose this "
            "port outside a trusted network until then.",
            port=bound,
        )
        return bound

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1.0)
            self._server = None
