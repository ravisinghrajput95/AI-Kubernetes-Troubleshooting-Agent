from fastapi import APIRouter, HTTPException, Response

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


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Prometheus exposition, on the unauthenticated router.

    Deliberately alongside `/health` rather than behind `require_permission`,
    and the reasoning is the same in both directions:

    - A scraper is infrastructure. It has no tenant, so there is no role for it
      to hold, and inventing a service account for Prometheus would be a second
      identity system for one consumer.
    - There is nothing here to protect. No series carries a cluster, tenant,
      namespace, user or investigation id — enforced and argued in
      `app/observability/metrics.py` — so the endpoint reveals how much work the
      platform is doing and nothing about whose work it is.

    If that second point ever stops being true, this decision has to be
    revisited rather than the label added.
    """
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics are disabled")

    from app.observability import render

    payload, content_type = render()
    return Response(content=payload, media_type=content_type)
