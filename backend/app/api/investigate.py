import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.audit.logger import get_audit_log
from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.jobs.models import JobStatus
from app.jobs.runner import InvestigationJobRunner, get_job_runner
from app.jobs.store import InvestigationJobStore, get_job_store
from app.kubernetes.context_service import KubernetesContextService
from app.models.investigation import (
    InvestigationJobAccepted,
    InvestigationRequest,
    InvestigationResponse,
)
from app.services.history_service import InvestigationHistoryService
from app.services.investigation_runner import (
    FAILURE_DETAIL,
    collection_failure,
    run_investigation,
)

# Applied at router level so a newly added endpoint is authenticated by
# default. Health checks live on their own unauthenticated router.
router = APIRouter(tags=["investigation"], dependencies=[Depends(require_principal)])

SSE_HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Disable proxy buffering so events arrive as they are produced.
    "X-Accel-Buffering": "no",
}


@router.get("/clusters")
def list_clusters(principal: Principal = Depends(require_principal)) -> dict:
    result = KubernetesContextService(principal=principal).list_contexts()
    get_audit_log().record_action("clusters.list", principal)
    return result


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


@router.get("/investigation-jobs")
def list_investigation_jobs(
    limit: int = 25,
    store: InvestigationJobStore = Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict]]:
    """In-flight and recently finished jobs held by this process."""
    return {
        "items": [
            job.to_dict(include_result=False)
            for job in store.list(limit=limit, owner=principal.subject)
        ]
    }


@router.get("/investigations")
def list_investigations(
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict]]:
    return {"items": InvestigationHistoryService().list_history(owner=principal.subject)}


@router.get("/investigations/{investigation_id}")
def get_investigation(
    investigation_id: str,
    store: InvestigationJobStore = Depends(get_job_store),
    principal: Principal = Depends(require_principal),
) -> dict:
    """Current state of an investigation.

    Resolves a live job first, then falls back to a persisted report, so an id
    stays addressable after the job has been evicted or the process restarted.
    """
    job = store.get(investigation_id)
    if job is not None:
        if job.owner and job.owner != principal.subject:
            raise HTTPException(status_code=404, detail="Investigation not found")
        return job.to_dict()

    report = InvestigationHistoryService().read_report(investigation_id, owner=principal.subject)
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


@router.post("/investigations/{investigation_id}/cancel")
async def cancel_investigation(
    investigation_id: str,
    store: InvestigationJobStore = Depends(get_job_store),
    runner: InvestigationJobRunner = Depends(get_job_runner),
    principal: Principal = Depends(require_principal),
) -> dict:
    job = store.get(investigation_id)
    if job is not None and job.owner and job.owner != principal.subject:
        raise HTTPException(status_code=404, detail="Investigation job not found")
    if job is None:
        raise HTTPException(status_code=404, detail="Investigation job not found")
    if job.status.terminal:
        raise HTTPException(
            status_code=409,
            detail=f"Investigation already {job.status}",
        )

    if not runner.cancel(investigation_id):
        raise HTTPException(status_code=409, detail="Investigation is not cancellable")

    return {"id": investigation_id, "status": str(JobStatus.CANCELLED)}


@router.get("/investigations/{investigation_id}/events")
async def stream_investigation_events(
    investigation_id: str,
    request: Request,
    store: InvestigationJobStore = Depends(get_job_store),
) -> Response:
    """Server-sent event stream of investigation progress."""
    if store.get(investigation_id) is None:
        raise HTTPException(status_code=404, detail="Investigation job not found")

    async def event_stream():
        async for event in store.subscribe(
            investigation_id,
            heartbeat=SSE_HEARTBEAT_SECONDS,
        ):
            if await request.is_disconnected():
                break

            if event is None:
                yield ": keepalive\n\n"
                continue

            payload = json.dumps(event.to_dict())
            yield f"event: {event.type}\ndata: {payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/investigations/{investigation_id}/pdf")
def download_investigation_pdf(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> FileResponse:
    history = InvestigationHistoryService()
    if not history.owns(investigation_id, principal.subject):
        raise HTTPException(status_code=404, detail="Investigation report not found")
    report_path = history.report_path(investigation_id, "pdf")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="application/pdf",
        filename=f"investigation-{investigation_id}.pdf",
    )


@router.get("/investigations/{investigation_id}/json")
def download_investigation_json(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> FileResponse:
    history = InvestigationHistoryService()
    if not history.owns(investigation_id, principal.subject):
        raise HTTPException(status_code=404, detail="Investigation report not found")
    report_path = history.report_path(investigation_id, "json")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="application/json",
        filename=f"investigation-{investigation_id}.json",
    )


@router.get("/investigations/{investigation_id}/report")
def get_investigation_report(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> dict:
    report = InvestigationHistoryService().read_report(investigation_id, owner=principal.subject)
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return report


@router.post("/investigations/{investigation_id}/regenerate")
def regenerate_investigation_report(
    investigation_id: str,
    principal: Principal = Depends(require_principal),
) -> dict:
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
) -> FileResponse:
    history = InvestigationHistoryService()
    if not history.owns(investigation_id, principal.subject):
        raise HTTPException(status_code=404, detail="Investigation report not found")
    report_path = history.report_path(investigation_id, "markdown")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="text/markdown",
        filename=f"investigation-{investigation_id}.md",
    )
