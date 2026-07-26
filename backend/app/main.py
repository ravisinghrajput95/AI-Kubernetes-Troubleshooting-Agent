from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.health import router as health_router
from app.api.investigate import router as investigate_router
from app.core.config import settings
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        description="AI Kubernetes troubleshooting agent API foundation.",
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

    @app.on_event("startup")
    async def on_startup() -> None:
        logger.info("{service} started", service=settings.service_name)

    return app


app = create_app()
