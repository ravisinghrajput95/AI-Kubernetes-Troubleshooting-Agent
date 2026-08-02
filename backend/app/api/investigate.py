import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger

from app.audit.logger import get_audit_log
from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.authz.dependencies import has_permission, require_permission
from app.authz.models import Permission
from app.jobs.models import JobStatus
from app.jobs.runner import InvestigationJobRunner, get_job_runner
from app.jobs.store import get_job_store
from app.kubernetes.context_service import KubernetesContextService
from app.models.investigation import (
    InvestigationJobAccepted,
    InvestigationRequest,
    InvestigationResponse,
)
from app.providers.base import ClusterUnreachable
from app.services.history_service import InvestigationHistoryService
from app.services.investigation_runner import (
    FAILURE_DETAIL,
    collection_failure,
    run_investigation,
)

# Applied at router level so a newly added endpoint is authenticated *and*
# authorised by default. `require_permission` depends on `require_principal`,
# so this is both checks in the right order; a route with no entry in
# `ROUTE_PERMISSIONS` is denied rather than allowed. Health checks live on
# their own unauthenticated router.
router = APIRouter(tags=["investigation"], dependencies=[Depends(require_permission)])

SSE_HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Disable proxy buffering so events arrive as they are produced.
    "X-Accel-Buffering": "no",
}

REPORT_FORMATS = {
    "pdf": ("application/pdf", "pdf"),
    "json": ("application/json", "json"),
    "markdown": ("text/markdown", "md"),
}


@router.get("/clusters")
def list_clusters(principal: Principal = Depends(require_principal)) -> dict:
    """Every cluster this platform can reach, however it reaches it.

    Two sources, deliberately merged rather than exposed separately: a cluster
    is a cluster whether the platform holds a kubeconfig for it or an agent
    dialled out from inside it. Before this merge the console could only show
    kubeconfig contexts, so a cluster that had done nothing wrong except be
    remote was invisible — which is the whole fleet, in the deployment this
    platform is being built for.
    """
    result = KubernetesContextService(principal=principal).list_contexts()
    result["items"] = merge_agent_clusters(result.get("items", []))
    get_audit_log().record_action("clusters.list", principal)
    return result


def merge_agent_clusters(contexts: list[dict]) -> list[dict]:
    """Fold connected agents into the kubeconfig context list.

    A cluster present in both is one cluster: the agent is the better route, so
    it wins the `connection` field, and the kubeconfig entry keeps its name.
    """
    items = [{**context, "connection": "kubeconfig", "agent": None} for context in contexts]

    for session in connected_agents():
        existing = next((item for item in items if item["name"] == session["cluster_id"]), None)
        if existing is not None:
            existing["connection"] = "agent"
            existing["agent"] = session
            continue

        items.append(
            {
                "name": session["cluster_id"],
                "cluster": session["cluster_id"],
                "current": False,
                "connection": "agent",
                "agent": session,
            }
        )

    return items


def connected_agents() -> list[dict]:
    """Every agent this tenant has, across every worker that holds one.

    A single-process deployment answers from its own registry, because there
    it *is* the fleet. A multi-replica one answers from the shared presence
    index — otherwise the console would show only the agents attached to
    whichever pod the load balancer picked, and a different subset on the next
    refresh.

    Imported lazily so a deployment with no gateway never loads grpc.
    """
    from app.core.config import settings

    if not settings.agent_gateway_enabled:
        return []

    from app.gateway.presence import get_agent_presence
    from app.gateway.session import get_agent_registry
    from app.tenancy import current_tenant

    presence = get_agent_presence()
    if presence is None:
        return [{**item, "local": True, "worker": ""} for item in get_agent_registry().clusters()]

    return presence.fleet(current_tenant())


@router.post("/investigate", response_model=InvestigationResponse)
async def investigate_cluster(
    request: InvestigationRequest | None = None,
    principal: Principal = Depends(require_principal),
) -> InvestigationResponse:
    """Run an investigation and wait for the result.

    Retained for backward compatibility. Deep investigations on large clusters
    should prefer `POST /investigations`, which returns immediately.
    """
    audit = get_audit_log()
    target = (request.context if request else "") or "current-context"
    try:
        result = await run_investigation(request, principal=principal)
    except ClusterUnreachable as exc:
        # 409, not 500: nothing is broken. The cluster's agent is attached to
        # another worker, and the honest answer is to say so rather than read a
        # local context that may be a different cluster of the same name.
        audit.record_action(
            "investigation.run", principal, outcome="refused", target=target, detail=str(exc)[:200]
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected investigation failure")
        audit.record_action(
            "investigation.run",
            principal,
            outcome="failure",
            target=target,
            detail=str(exc)[:200],
        )
        raise HTTPException(status_code=500, detail=FAILURE_DETAIL) from exc

    audit.record_action(
        "investigation.run",
        principal,
        target=target,
        investigation_id=result["history_item"]["id"],
    )
    return InvestigationResponse(status="success", **result)


@router.post("/investigations", status_code=202, response_model=InvestigationJobAccepted)
async def start_investigation_job(
    request: InvestigationRequest | None = None,
    runner: InvestigationJobRunner = Depends(get_job_runner),
    principal: Principal = Depends(require_principal),
) -> InvestigationJobAccepted:
    """Submit an investigation and return immediately with its id.

    Must stay async: task creation requires the event loop thread.
    """
    job = runner.submit(request, principal=principal)
    get_audit_log().record_action(
        "investigation.submit",
        principal,
        target=(request.context if request else "") or "current-context",
        investigation_id=job.id,
    )
    return InvestigationJobAccepted(
        id=job.id,
        status=str(job.status),
        status_url=f"/investigations/{job.id}",
        events_url=f"/investigations/{job.id}/events",
    )


def _visible_owner(principal: Principal) -> str | None:
    """Whose investigations this caller may list.

    `None` means the whole tenant, and it is a role that grants it rather than
    an argument a handler chooses: `investigation.read_all` belongs to admin
    and owner, so an incident review does not depend on the person who happened
    to run the investigation still being available. Everyone else sees their
    own, exactly as before.

    Tenant isolation is untouched either way — the store is already filtered by
    row-level security, so "the whole tenant" is the widest this can possibly
    mean.
    """
    if has_permission(principal, Permission.INVESTIGATION_READ_ALL):
        return None
    return principal.subject


def _may_read_job(job, owner: str | None) -> bool:
    """`owner=None` is the tenant-wide reader; otherwise it must be theirs.

    Unowned jobs stay readable, which is what keeps investigations submitted
    before authentication existed — and every job in an `AUTH_MODE=disabled`
    deployment — addressable.
    """
    return owner is None or not job.owner or job.owner == owner


@router.get("/investigation-jobs")
def list_investigation_jobs(
    limit: int = 25,
    store=Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict]]:
    """In-flight and recently finished investigation jobs."""
    owner = _visible_owner(principal)
    return {
        "items": [job.to_dict(include_result=False) for job in store.list(limit=limit, owner=owner)]
    }


@router.get("/investigations")
def list_investigations(
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict]]:
    return {"items": InvestigationHistoryService().list_history(owner=_visible_owner(principal))}


@router.get("/investigations/{investigation_id}")
def get_investigation(
    investigation_id: str,
    store=Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict:
    """Current state of an investigation.

    Resolves a live job first, then falls back to a persisted report, so an id
    stays addressable after the job has been evicted or the process restarted.
    """
    owner = _visible_owner(principal)
    job = store.get(investigation_id)
    if job is not None:
        if not _may_read_job(job, owner):
            raise HTTPException(status_code=404, detail="Investigation not found")
        return job.to_dict()

    report = InvestigationHistoryService().read_report(investigation_id, owner=owner)
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    investigation = report.get("investigation", {})
    # Same verdict the job runner applied, so an evicted job does not appear to
    # change outcome once it is served from the persisted report.
    failure = collection_failure(investigation)

    return {
        "id": investigation_id,
        "status": str(JobStatus.FAILED if failure else JobStatus.SUCCEEDED),
        "persisted": True,
        "error": failure or "",
        "investigation": investigation,
        "diagnosis": report.get("diagnosis", {}),
    }


@router.get("/investigations/{investigation_id}/status")
def get_investigation_status(
    investigation_id: str,
    store=Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict:
    """State and timeline, without the investigation itself.

    The polling fallback asks "is it done yet?" every 1.5 seconds and reads two
    fields off the answer. Serving that from `GET /investigations/{id}` meant a
    finished investigation was re-serialised out of Postgres on every tick —
    2.7 MB on a cluster at the `MAX_LIST_ITEMS` ceiling, to render a progress
    bar. The path exists because a corporate proxy blocked SSE, so it is
    already the degraded one; it should not also be the expensive one.

    Additive rather than a change to the existing endpoint: `/investigations/{id}`
    keeps returning the whole result, because that is what a client rendering
    the report actually wants, and changing it would break every consumer to
    benefit one.
    """
    job = store.get_summary(investigation_id)
    if job is None or not _may_read_job(job, _visible_owner(principal)):
        # Falls through to the persisted report for an evicted job, exactly as
        # the full read does — an id must not stop being addressable here and
        # keep working there.
        report = InvestigationHistoryService().read_report(
            investigation_id, owner=_visible_owner(principal)
        )
        if report is None:
            raise HTTPException(status_code=404, detail="Investigation not found")
        failure = collection_failure(report.get("investigation", {}))
        return {
            "id": investigation_id,
            "status": str(JobStatus.FAILED if failure else JobStatus.SUCCEEDED),
            "persisted": True,
            "error": failure or "",
            "timeline": [],
        }

    return job.to_dict(include_result=False)


@router.post("/investigations/{investigation_id}/cancel")
async def cancel_investigation(
    investigation_id: str,
    store=Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict:
    """Ask for an investigation to stop.

    Must stay async: with the in-process store this reaches `Task.cancel()`
    synchronously, which requires the event loop thread.

    Cancellation is a request, not an instruction executed here. Once jobs can
    run on another worker, whether one acted is not observable at this moment —
    so the durable flag this sets is the guarantee, and the message it publishes
    is only what makes it fast.
    """
    job = store.get_summary(investigation_id)
    if job is None or (job.owner and job.owner != principal.subject):
        raise HTTPException(status_code=404, detail="Investigation job not found")
    if job.status.terminal:
        raise HTTPException(
            status_code=409,
            detail=f"Investigation already {job.status}",
        )

    store.request_cancel(investigation_id)
    return {"id": investigation_id, "status": str(JobStatus.CANCELLED)}


def _resume_position(request: Request) -> int:
    """Where a reconnecting client left off, from the SSE `Last-Event-ID`."""
    try:
        return max(0, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError:
        return 0


@router.get("/investigations/{investigation_id}/events")
async def stream_investigation_events(
    investigation_id: str,
    request: Request,
    store=Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> Response:
    """Server-sent event stream of investigation progress.

    Each frame carries the event's sequence as its SSE id, so a browser that
    reconnects resumes from where it stopped instead of replaying the timeline.

    **This endpoint had no ownership check.** Every other investigation route
    takes the principal and 404s on someone else's id; this one took only the
    store, so any authenticated caller who knew — or guessed — an id received
    the live progress of another user's investigation. Authentication was
    applied at the router and authorisation simply was not, which is the exact
    shape of gap this milestone exists to close, so it is closed here rather
    than filed.
    """
    job = store.get_summary(investigation_id)
    if job is None or not _may_read_job(job, _visible_owner(principal)):
        raise HTTPException(status_code=404, detail="Investigation job not found")

    after_seq = _resume_position(request)

    async def event_stream():
        async for event in store.subscribe(
            investigation_id,
            heartbeat=SSE_HEARTBEAT_SECONDS,
            after_seq=after_seq,
        ):
            if await request.is_disconnected():
                break

            if event is None:
                yield ": keepalive\n\n"
                continue

            payload = json.dumps(event.to_dict())
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _report_response(investigation_id: str, report_format: str, principal: Principal) -> Response:
    """Serve a rendered report as bytes.

    Deliberately not a `FileResponse`: once state is out of process the report
    may have been rendered by a different worker, so there is no local path to
    hand to the kernel. Status, media type and filename are unchanged.
    """
    content = InvestigationHistoryService().read_report_bytes(
        investigation_id, report_format, owner=_visible_owner(principal)
    )
    if content is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    media_type, extension = REPORT_FORMATS[report_format]
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="investigation-{investigation_id}.{extension}"'
            )
        },
    )


@router.get("/investigations/{investigation_id}/pdf")
def download_investigation_pdf(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> Response:
    return _report_response(investigation_id, "pdf", principal)


@router.get("/investigations/{investigation_id}/json")
def download_investigation_json(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> Response:
    return _report_response(investigation_id, "json", principal)


@router.get("/investigations/{investigation_id}/report")
def get_investigation_report(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> dict:
    report = InvestigationHistoryService().read_report(
        investigation_id, owner=_visible_owner(principal)
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return report


@router.post("/investigations/{investigation_id}/regenerate")
def regenerate_investigation_report(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> dict:
    # Owner-scoped rather than widened by `investigation.read_all`: this writes
    # to the report store, and a read permission must not authorise a write.
    history = InvestigationHistoryService()
    if not history.owns(investigation_id, principal.subject):
        raise HTTPException(status_code=404, detail="Investigation report not found")
    report = history.regenerate(investigation_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return {"status": "success", "report": report}


@router.get("/investigations/{investigation_id}/markdown")
def download_investigation_markdown(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> Response:
    return _report_response(investigation_id, "markdown", principal)
