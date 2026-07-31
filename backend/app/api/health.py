from fastapi import APIRouter

from app.core.config import settings
from app.models.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service=settings.service_name,
        auth_mode=settings.auth_mode,
        # True only when this backend genuinely accepts unauthenticated
        # requests, so the console can say so rather than leaving a dangerous
        # configuration invisible.
        insecure=settings.auth_mode == "disabled",
    )
