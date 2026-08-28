"""The inbound trigger. An alert arrives; an investigation starts.

§3.7's event ingress, and the exit criterion M9 is measured against. What makes
this different from every other route here is that there is no person behind
it, which changes three things and each is handled where it arises:

- **Authentication is a signature, not a bearer token.** A monitoring system
  posts from infrastructure and cannot hold a user's credential. The signature
  covers a timestamp as well as the body, so a captured request cannot be
  replayed later with a fresh header.
- **Authorisation is the source's configured identity.** The investigation is
  impersonated as that subject exactly as a person's would be, which is what
  keeps "the platform cannot see more than you can" true through a door with no
  user behind it. `app/events/sources.py` argues this at length.
- **The same alert must not trigger twice.** Alertmanager re-sends; see
  `app/events/alerts.py`.

Its own router, with no `require_permission` dependency, because the caller
carries no principal to authorise — the signature *is* the authorisation, and
the identity it maps to is fixed by configuration rather than asserted by the
payload.
"""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from loguru import logger

from app.audit.logger import get_audit_log
from app.core.config import settings
from app.events import (
    EventSourceError,
    get_sources,
    get_trigger_ledger,
    parse_alertmanager,
)
from app.models.investigation import InvestigationRequest
from app.observability import metrics
from app.tenancy import tenant_scope

router = APIRouter(tags=["events"])

SIGNATURE_HEADER = "X-K8sagent-Signature"
TIMESTAMP_HEADER = "X-K8sagent-Timestamp"


@router.post("/events/{source_name}", status_code=202)
async def receive_event(source_name: str, request: Request) -> dict[str, Any]:
    """Accept a signed alert and start investigations for it.

    Always 202 when the signature is good, even when nothing was started.
    Alertmanager treats a non-2xx as a delivery failure and retries, so
    returning an error for "this was a duplicate" or "no cluster label" would
    turn a normal outcome into a retry storm. What happened is in the body.
    """
    body = await request.body()
    source = get_sources().get(source_name)

    if source is None:
        # Same shape of answer as a bad signature, and deliberately after the
        # body is read: a caller must not be able to enumerate configured
        # source names by timing or status code.
        logger.warning("Rejected event for unknown source {source}", source=source_name)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorised")

    try:
        source.verify(
            body,
            request.headers.get(TIMESTAMP_HEADER, ""),
            request.headers.get(SIGNATURE_HEADER, ""),
            time.time(),
        )
    except EventSourceError as exc:
        metrics.event_rejected("signature")
        logger.warning(
            "Rejected event from {source}: {reason}", source=source_name, reason=str(exc)
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorised"
        ) from exc

    try:
        payload = await request.json()
    except Exception as exc:
        metrics.event_rejected("malformed")
        raise HTTPException(status_code=400, detail="Body is not JSON") from exc

    triggers = parse_alertmanager(payload if isinstance(payload, dict) else {})
    principal = source.principal()
    audit = get_audit_log()

    started: list[dict[str, str]] = []
    skipped = 0

    # Everything below runs as the source's tenant. Fixed by configuration, so
    # a payload cannot name its own — which would be a cross-tenant trigger
    # from a system anyone who can write an alert rule can influence.
    with tenant_scope(source.tenant):
        for trigger in triggers:
            if not get_trigger_ledger().claim(
                f"{source.tenant}:{trigger.fingerprint}", settings.event_cooldown_seconds
            ):
                skipped += 1
                metrics.event_rejected("duplicate")
                continue

            job = await _start(trigger, principal)
            if job is None:
                skipped += 1
                continue

            metrics.event_triggered()
            audit.record_action(
                "investigation.triggered",
                principal,
                target=trigger.cluster,
                investigation_id=job,
                detail=trigger.describe(),
            )
            started.append({"id": job, "alert": trigger.describe()})

    return {
        "source": source_name,
        "received": len(triggers),
        "started": started,
        "skipped": skipped,
    }


async def _start(trigger, principal) -> str | None:
    """Submit one investigation, or report that it could not be submitted.

    A failure here is logged and skipped rather than raised: one unusable alert
    in a batch must not reject the batch, because Alertmanager would retry the
    whole thing and the usable ones would run again.
    """
    from app.jobs.runner import get_job_runner

    try:
        job = get_job_runner().submit(
            # An alert is a claim that the cluster just changed, so reusing a
            # read taken before it fired would investigate the world the alert
            # is complaining about the absence of. `refresh` still *writes* what
            # it reads, so an operator opening the console straight afterwards
            # gets the alert's own fresh evidence rather than a cold cache.
            InvestigationRequest(
                context=trigger.cluster,
                namespace=trigger.namespace or None,
                refresh=True,
            ),
            principal=principal,
        )
    except Exception as exc:
        metrics.event_rejected("submit_failed")
        logger.opt(exception=exc).warning(
            "Could not start an investigation for {alert}", alert=trigger.describe()
        )
        return None

    logger.info("Alert {alert} started investigation {id}", alert=trigger.describe(), id=job.id)
    return job.id
