from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.agents import router as agents_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.investigate import router as investigate_router
from app.api.mcp import router as mcp_router
from app.api.members import router as members_router
from app.api.session import router as session_router
from app.core.config import settings
from app.core.correlation import (
    correlation_id,
    correlation_scope,
    new_request_id,
    sanitise,
)
from app.core.logging import configure_logging
from app.core.readiness import get_readiness, reset_readiness
from app.state import build_state, start_agent_gateway, start_retention


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Choose the state backend, and take it down cleanly.

    Startup is where the single-process and multi-worker deployments diverge,
    and the only place either is named. In the distributed case this also runs
    migrations and starts the queue, control and reaper loops.
    """
    reset_readiness()
    state = build_state()
    state.gateway = await start_agent_gateway(state)
    start_retention(state)
    app.state.backend = state
    # Last, and only on the success path: everything above can raise, and a
    # worker that failed to wire its store must not report itself ready.
    get_readiness().mark_started()
    logger.info("{service} started", service=settings.service_name)
    try:
        yield
    finally:
        await state.shutdown()
        logger.info("{service} stopped", service=settings.service_name)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        description="AI Kubernetes troubleshooting agent API foundation.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        """Give every request an id, and hand it back on the response.

        An inbound `X-Correlation-ID` is honoured so a trace can start at the
        caller's gateway, but it is sanitised first: the value lands in every
        log line this request writes, so an unbounded one pads the aggregator
        and an embedded newline forges a line.

        The response header matters more than it looks — it is what lets a user
        reporting "my investigation hung" quote an id that finds the worker's
        logs, without the platform having to correlate by timestamp.
        """
        incoming = sanitise(request.headers.get("X-Correlation-ID"))
        with correlation_scope(incoming or new_request_id()):
            response = await call_next(request)
            # Read back rather than reusing `incoming`: the submit path rebinds
            # to the investigation id, and that is the id worth returning.
            response.headers["X-Correlation-ID"] = correlation_id()
            return response

    app.include_router(health_router)
    app.include_router(investigate_router)
    app.include_router(agents_router)
    app.include_router(session_router)
    app.include_router(members_router)
    # No permission dependency: an alert carries no principal, and the
    # signature is the authorisation. See app/api/events.py.
    app.include_router(events_router)
    # Authorisation is per tool, not per route. See app/api/mcp.py.
    app.include_router(mcp_router)

    return app


app = create_app()
