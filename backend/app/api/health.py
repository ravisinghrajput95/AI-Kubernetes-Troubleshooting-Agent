import asyncio

from fastapi import APIRouter, HTTPException, Response
from loguru import logger

from app.core.config import settings
from app.core.readiness import get_readiness
from app.models.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])

# A readiness probe that hangs is a worker that never leaves rotation. Well
# under any sensible probe `timeoutSeconds`, so the answer is "unavailable"
# rather than the probe itself timing out — which reports the same outcome with
# none of the detail.
_PROBE_TIMEOUT = 2.0

# The one status that takes a worker out of rotation. `degraded` means reduced
# capability on a worker that still serves reads; only the store knows which of
# its dependencies is which.
UNAVAILABLE = "unavailable"


def _health() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service=settings.service_name,
        auth_mode=settings.auth_mode,
        # True only when this backend genuinely accepts unauthenticated
        # requests, so the console can say so rather than leaving a dangerous
        # configuration invisible.
        insecure=settings.auth_mode == "disabled",
    )


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Unchanged, and deliberately still liveness-shaped.

    The console reads this to learn `auth_mode` before it can authenticate, and
    every existing deployment's probes point at it. Making it conditional on a
    database would mean a Postgres blip logs the whole console out.
    """
    return _health()


@router.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    """Is the process alive. Never consults a dependency.

    A liveness probe that checks Postgres restarts every worker in the fleet
    the moment Postgres hiccups, converting a recoverable dependency failure
    into an outage. If the event loop is running enough to answer this, killing
    the container cannot help.
    """
    return _health()


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Should this worker be sent traffic.

    503 while starting, while draining, or while a dependency it cannot work
    without is unreachable. The draining case is the one that matters: SIGTERM
    and Endpoints removal race, so a worker that keeps answering "ready" during
    that window receives requests it is about to stop serving.
    """
    state = get_readiness()
    checks = await _dependency_checks()

    # `degraded` is deliberately not fatal, and the store is what decides which
    # of its dependencies earns which word. A Redis outage leaves every read
    # working; failing readiness on it would take every worker out of rotation
    # at once and turn a degradation into an outage.
    fatal = [name for name, status in checks.items() if status == UNAVAILABLE]
    ok = state.ready and not fatal

    if not ok:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if ok else "not_ready",
        reason=state.reason() if not state.ready else (fatal[0] if fatal else "ready"),
        checks=checks,
    )


async def _dependency_checks() -> dict[str, str]:
    """Ask the store, which is the only thing that knows what it depends on.

    The handler never learns which deployment it is in — the in-process store
    returns `{}` and the distributed one names Postgres and Redis. Same rule
    that keeps every other API handler ignorant of the state backend.
    """
    from app.jobs.store import get_job_store

    try:
        store = get_job_store()
    except Exception:
        # Startup has not wired one yet. `started` already reports that; this
        # just avoids a confusing second reason for the same fact.
        return {}

    try:
        return await asyncio.wait_for(asyncio.to_thread(store.check_health), timeout=_PROBE_TIMEOUT)
    except TimeoutError:
        # A hung dependency is not a hung probe: report it and stay out of
        # rotation, rather than letting the probe's own timeout do it with no
        # detail attached.
        logger.warning("Readiness checks timed out after {t}s", t=_PROBE_TIMEOUT)
        return {"store": UNAVAILABLE}
    except Exception as exc:
        logger.warning("Readiness checks failed: {error}", error=str(exc))
        return {"store": UNAVAILABLE}


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
