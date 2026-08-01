from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.agents import router as agents_router
from app.api.health import router as health_router
from app.api.investigate import router as investigate_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.state import build_state, start_agent_gateway, start_retention


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Choose the state backend, and take it down cleanly.

    Startup is where the single-process and multi-worker deployments diverge,
    and the only place either is named. In the distributed case this also runs
    migrations and starts the queue, control and reaper loops.
    """
    state = build_state()
    state.gateway = await start_agent_gateway(state)
    start_retention(state)
    app.state.backend = state
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

    app.include_router(health_router)
    app.include_router(investigate_router)
    app.include_router(agents_router)

    return app


app = create_app()
