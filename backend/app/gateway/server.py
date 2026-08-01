"""The agent gateway: a gRPC server that agents dial into.

Stateless with respect to investigations. It terminates the stream, resolves
the peer to a cluster, and bridges messages to and from an `AgentSession`. It
holds no investigation state, which is what lets it scale horizontally and lets
any gateway serve any agent.

**Identity comes from the certificate.** `AgentHello.cluster_id` is checked
against the peer certificate rather than believed, and a disagreement ends the
stream. M4a took the agent's word for it and said so in its startup log; that
gap is what M4b closes.

Two listeners, because gRPC's Python bindings offer only "never request a
client certificate" or "require and verify one" — there is no request-but-do-
not-require mode to serve both trust contexts on one port. That constraint
turns out to describe the design rather than fight it:

- **the enrolment listener** requests no client certificate, and is the only
  surface an unauthenticated peer can reach. `Register` there spends a
  single-use bootstrap token. `Connect` there is refused, because resolving an
  identity needs a certificate and there is none.
- **the gateway listener** requires a client certificate signed by the platform
  CA. `Connect` lives here, and so does `Register` — which on this listener can
  only be a renewal, authenticated by the certificate the agent already holds.

A deployment that has finished enrolling its fleet can firewall the enrolment
port off entirely and lose nothing but the ability to add clusters.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import grpc
from loguru import logger

from app.core.config import settings
from app.gateway.identity import AgentIdentityService, RegistrationRefused
from app.gateway.session import AgentRegistry, AgentSession, get_agent_registry
from app.security.identity import AgentIdentity, IdentityError
from app.wire.gen.agent.v1 import agent_pb2, agent_pb2_grpc

# Warn on a stream whose certificate is inside this much of expiry. Well past
# the 2/3-life renewal point, so seeing one means renewal is failing.
EXPIRY_WARNING = timedelta(days=14)


class AgentGatewayService(agent_pb2_grpc.AgentGatewayServicer):
    """Serves both listeners. Which trust context it is in is decided by the
    transport — whether a peer certificate is present — never by the request."""

    def __init__(
        self,
        registry: AgentRegistry,
        identity_service: AgentIdentityService | None = None,
    ) -> None:
        self._registry = registry
        self._identity = identity_service

    # --- registration ------------------------------------------------------

    async def Register(
        self,
        request: agent_pb2.RegistrationRequest,
        context: grpc.aio.ServicerContext,
    ) -> agent_pb2.RegistrationResponse:
        """Exchange a single-use bootstrap token — or a current certificate —
        for the next certificate.

        The agent generates its own key and sends only a CSR, so no private key
        for a cluster identity ever crosses the wire or exists on the platform.
        """
        if self._identity is None:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                "This gateway runs with AGENT_GATEWAY_TLS=disabled and issues no "
                "certificates. Registration requires mTLS mode.",
            )
            return agent_pb2.RegistrationResponse()

        try:
            # Signing and the enrolment ledger both block; the event loop is
            # also serving every other agent's stream.
            granted = await asyncio.to_thread(self._identity.register, request, context)
        except RegistrationRefused as refusal:
            logger.warning(
                "Refused a registration from {peer}: {reason}",
                peer=context.peer(),
                reason=refusal.detail,
            )
            await context.abort(refusal.code, refusal.detail)
            return agent_pb2.RegistrationResponse()
        except IdentityError as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
            return agent_pb2.RegistrationResponse()

        response = agent_pb2.RegistrationResponse(
            certificate=granted.certificate_pem,
            ca_bundle=granted.ca_bundle_pem,
            gateway_endpoint=self._identity.gateway_endpoint,
        )
        response.expires_at.FromDatetime(granted.expires_at)
        return response

    # --- the stream --------------------------------------------------------

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
        identity = await self._identify(context)
        if identity is None:
            return

        # Created before the session so revocation can be waited on from the
        # moment the stream exists, and handed to the session to be set.
        closed = asyncio.Event()
        holder: dict[str, AgentSession] = {}

        inbound = asyncio.create_task(
            self._consume(request_iterator, context, identity, closed, holder)
        )
        revoked = asyncio.create_task(closed.wait())

        try:
            done, _ = await asyncio.wait({inbound, revoked}, return_when=asyncio.FIRST_COMPLETED)

            if inbound in done:
                refusal = inbound.result()
                if refusal is not None:
                    await context.abort(*refusal)
                return

            # The sweeper ended it: the certificate this stream authenticated
            # with has been revoked since the handshake.
            session = holder.get("session")
            reason = session.termination_reason if session else "Certificate revoked."
            logger.warning(
                "Dropping the stream for cluster {cluster}: {reason}",
                cluster=identity.cluster_id,
                reason=reason,
            )
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, reason)
        finally:
            for task in (inbound, revoked):
                task.cancel()
            session = holder.get("session")
            if session is not None:
                self._registry.unregister(session)

    async def _identify(self, context: grpc.aio.ServicerContext) -> AgentIdentity | None:
        """Who is on the other end, or None having already aborted the stream."""
        if self._identity is None:
            # Plaintext development mode. There is nothing to resolve, so the
            # identity is whatever hello says — which is exactly the property
            # this mode exists to trade away, and it is logged at startup.
            if not self._plaintext_token_ok(context):
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid bootstrap token")
                return None
            return AgentIdentity(cluster_id="", serial="", source="declared")

        try:
            # A fresh read of the revocation list on every connect: connects
            # are rare, and a stale answer here would let a revoked agent back
            # in for up to a sweep interval.
            await asyncio.to_thread(self._identity.refresh_revocations)
            return await asyncio.to_thread(self._identity.resolve, context)
        except IdentityError as exc:
            logger.warning(
                "Refusing a stream from {peer}: {reason}", peer=context.peer(), reason=str(exc)
            )
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
            return None

    async def _consume(
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
        identity: AgentIdentity,
        closed: asyncio.Event,
        holder: dict[str, AgentSession],
    ) -> tuple[grpc.StatusCode, str] | None:
        """Read the stream until it ends. Returns a refusal for the caller to abort with."""
        session: AgentSession | None = None
        pump: asyncio.Task | None = None

        try:
            async for message in request_iterator:
                kind = message.WhichOneof("payload")

                if session is None:
                    if kind != "hello":
                        return (
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "The first message on a stream must be hello",
                        )

                    resolved = self._reconcile(identity, message.hello)
                    if isinstance(resolved, tuple):
                        return resolved

                    session = AgentSession(resolved, message.hello, closed=closed)
                    holder["session"] = session
                    self._registry.register(session)
                    self._warn_if_expiring(resolved)
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
        return None

    def _reconcile(
        self, identity: AgentIdentity, hello: agent_pb2.AgentHello
    ) -> AgentIdentity | tuple[grpc.StatusCode, str]:
        """Settle what the certificate says against what the agent says.

        **The certificate wins, and a contradiction ends the stream.** Silently
        preferring the certificate would be defensible against an attacker but
        not against a mistake: an agent redeployed with the wrong `--cluster`
        flag would have its evidence filed under one name while its own logs
        and configuration said another, permanently, with nothing to notice it
        by. A refused connection naming both values is a one-line fix.

        An empty `hello.cluster_id` is not a contradiction — the certificate
        supplies it, which is what a certificate is for.
        """
        if not identity.verified:
            # Plaintext mode: hello is the only source there is.
            if not hello.cluster_id:
                return (
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "hello carried no cluster id, and without mTLS there is nothing "
                    "else to identify this agent by.",
                )
            return AgentIdentity(cluster_id=hello.cluster_id, serial="", source="declared")

        if hello.cluster_id and hello.cluster_id != identity.cluster_id:
            detail = (
                f"This agent's certificate ({identity.serial}) names cluster "
                f"{identity.cluster_id!r}, but its hello claims {hello.cluster_id!r}. "
                f"The certificate is the identity; fix the agent's --cluster flag "
                f"or re-enrol it for {hello.cluster_id!r}."
            )
            logger.error("Rejecting a stream on an identity mismatch: {detail}", detail=detail)
            return (grpc.StatusCode.PERMISSION_DENIED, detail)

        return identity

    def _warn_if_expiring(self, identity: AgentIdentity) -> None:
        from app.gateway.identity import expiring_soon

        if expiring_soon(identity, EXPIRY_WARNING):
            logger.warning(
                "Cluster {cluster} connected with a certificate expiring at {expiry}. "
                "Agents renew at two-thirds of certificate life, so this one's "
                "renewal is failing.",
                cluster=identity.cluster_id,
                expiry=identity.expires_at.isoformat() if identity.expires_at else "unknown",
            )

    async def _pump(self, session: AgentSession, context: grpc.aio.ServicerContext) -> None:
        """Drain queued work onto the stream until the connection ends."""
        while True:
            message = await session.outbound.get()
            await context.write(message)

    def _plaintext_token_ok(self, context: grpc.aio.ServicerContext) -> bool:
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
    """Lifecycle for the gRPC servers, owned by application startup."""

    def __init__(
        self,
        port: int,
        registry: AgentRegistry | None = None,
        enrolment_port: int | None = None,
        identity_service: AgentIdentityService | None = None,
        mtls: bool | None = None,
    ) -> None:
        self._port = port
        self._registry = registry or get_agent_registry()
        self._requested_enrolment_port = enrolment_port
        self._identity = identity_service
        self._mtls = settings.agent_mtls_enabled if mtls is None else mtls
        self._servers: list[grpc.aio.Server] = []
        self._sweeper: asyncio.Task | None = None
        self.enrolment_port = 0

    @property
    def identity_service(self) -> AgentIdentityService | None:
        return self._identity

    async def start(self) -> int:
        """Bind the listeners. Returns the port agents Connect on."""
        if not self._mtls:
            return await self._start_plaintext()
        return await self._start_mtls()

    async def _start_plaintext(self) -> int:
        server = grpc.aio.server()
        agent_pb2_grpc.add_AgentGatewayServicer_to_server(
            AgentGatewayService(self._registry, None), server
        )
        bound = server.add_insecure_port(f"0.0.0.0:{self._port}")
        await server.start()
        self._servers.append(server)

        logger.warning(
            "Agent gateway listening on {port} with AGENT_GATEWAY_TLS=disabled: "
            "plaintext, shared-token auth, and a cluster id the agent asserts "
            "about itself. This is the local-development path and it is not "
            "safe on an untrusted network — unset AGENT_GATEWAY_TLS for mTLS.",
            port=bound,
        )
        return bound

    async def _start_mtls(self) -> int:
        if self._identity is None:
            self._identity = build_identity_service()

        bundle = self._identity.ca_bundle_pem
        server_cert, server_key = self._server_certificate()

        # Requires and verifies a client certificate. `Connect` is only
        # reachable here, which is what makes an unverified stream impossible
        # rather than merely refused.
        gateway = grpc.aio.server()
        agent_pb2_grpc.add_AgentGatewayServicer_to_server(
            AgentGatewayService(self._registry, self._identity), gateway
        )
        bound = gateway.add_secure_port(
            f"0.0.0.0:{self._port}",
            grpc.ssl_server_credentials(
                [(server_key, server_cert)],
                root_certificates=bundle,
                require_client_auth=True,
            ),
        )
        await gateway.start()
        self._servers.append(gateway)

        # Requests no client certificate, because an enrolling agent has none.
        # The only thing reachable here is `Register` with a bootstrap token.
        enrolment = grpc.aio.server()
        agent_pb2_grpc.add_AgentGatewayServicer_to_server(
            AgentGatewayService(self._registry, self._identity), enrolment
        )
        self.enrolment_port = enrolment.add_secure_port(
            f"0.0.0.0:{self._enrolment_port(bound)}",
            grpc.ssl_server_credentials(
                [(server_key, server_cert)],
                root_certificates=bundle,
                require_client_auth=False,
            ),
        )
        await enrolment.start()
        self._servers.append(enrolment)

        self._sweeper = asyncio.create_task(self._sweep_revocations())

        logger.info(
            "Agent gateway listening on {port} (mTLS, trust domain {domain}); "
            "enrolment on {enrolment}. Identity comes from the peer certificate, "
            "not from hello. Issue a bootstrap token with: python -m app.agentctl "
            "issue-token --cluster <id>",
            port=bound,
            enrolment=self.enrolment_port,
            domain=self._identity.trust_domain,
        )
        return bound

    def _enrolment_port(self, gateway_port: int) -> int:
        if self._requested_enrolment_port is not None:
            return self._requested_enrolment_port
        configured = settings.agent_enrolment_port
        if configured:
            return configured
        # Port 0 asks the OS for a free one, which is what the tests use; there
        # is no "one above" an OS-assigned port, so ask again.
        return gateway_port + 1 if self._port else 0

    def _server_certificate(self) -> tuple[bytes, bytes]:
        assert self._identity is not None
        dns = [name.strip() for name in settings.agent_gateway_dns_names.split(",") if name.strip()]
        ips = [
            address.strip()
            for address in settings.agent_gateway_ip_addresses.split(",")
            if address.strip()
        ]
        return self._identity.authority.issue_server_certificate(dns, ips)

    async def _sweep_revocations(self) -> None:
        """Drop live streams whose certificates have been revoked.

        Without this, revocation would only take effect the next time an agent
        reconnected — which, on a transport designed around a stream that stays
        open for weeks, is close to never.
        """
        interval = max(1.0, settings.agent_revocation_sweep_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
                assert self._identity is not None
                serials = await asyncio.to_thread(self._identity.refresh_revocations)
                ended = self._registry.terminate_revoked(
                    serials, "This agent's certificate has been revoked."
                )
                for session in ended:
                    logger.warning(
                        "Revoked certificate {serial}: ending the stream for cluster {cluster}",
                        serial=session.certificate_serial,
                        cluster=session.cluster_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.opt(exception=exc).warning("Revocation sweep failed")

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for server in self._servers:
            await server.stop(grace=1.0)
        self._servers = []


def build_identity_service() -> AgentIdentityService:
    """The CA, the enrolment ledger and the revocation list, wired from settings."""
    from app.security.ca import CertificateAuthority
    from app.security.enrolment import get_enrolment_store

    settings.validate_agent_gateway()
    certificate_path, key_path = settings.agent_ca_paths
    authority = CertificateAuthority.load_or_create(
        Path(certificate_path), Path(key_path), settings.agent_trust_domain
    )
    return AgentIdentityService(
        authority,
        get_enrolment_store(),
        leaf_lifetime=timedelta(hours=settings.agent_cert_ttl_hours),
        gateway_endpoint=settings.agent_gateway_advertise,
    )
