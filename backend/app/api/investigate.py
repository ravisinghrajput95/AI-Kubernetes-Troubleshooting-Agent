from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from loguru import logger

from app.models.investigation import InvestigationRequest, InvestigationResponse
from app.ai.root_cause_analyzer import RootCauseAnalyzer
from app.kubernetes.context_service import KubernetesContextService
from app.services.history_service import InvestigationHistoryService
from app.services.investigation_service import InvestigationService

router = APIRouter(tags=["investigation"])


@router.get("/clusters")
def list_clusters() -> dict:
    return KubernetesContextService().list_contexts()


@router.post("/investigate", response_model=InvestigationResponse)
def investigate_cluster(request: InvestigationRequest | None = None) -> InvestigationResponse:
    context = request.context if request else None
    namespace = request.namespace if request else None
    resource_kind = request.resource_kind if request else None
    resource_name = request.resource_name if request else None
    try:
        service = InvestigationService(
            context=context,
            namespace=namespace,
            resource_kind=resource_kind,
            resource_name=resource_name,
        )
        investigation = service.run()
        diagnosis = RootCauseAnalyzer().analyze(investigation)
        investigation["timeline"].append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": "Root Cause Generated",
            }
        )
        history_item = InvestigationHistoryService().save(diagnosis, investigation)
        return InvestigationResponse(
            status="success",
            investigation=investigation,
            diagnosis=diagnosis,
            history_item=history_item,
        )
    except Exception as exc:
        logger.exception("Unexpected investigation failure")
        raise HTTPException(
            status_code=500,
            detail=(
                "Investigation failed unexpectedly. Verify kubeconfig, cluster "
                "access, kubectl permissions, backend logs, and OpenAI API settings."
            ),
        ) from exc


@router.get("/investigations")
def list_investigations() -> dict[str, list[dict]]:
    return {"items": InvestigationHistoryService().list_history()}


@router.get("/investigations/{investigation_id}/pdf")
def download_investigation_pdf(investigation_id: str) -> FileResponse:
    report_path = InvestigationHistoryService().report_path(investigation_id, "pdf")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="application/pdf",
        filename=f"investigation-{investigation_id}.pdf",
    )


@router.get("/investigations/{investigation_id}/json")
def download_investigation_json(investigation_id: str) -> FileResponse:
    report_path = InvestigationHistoryService().report_path(investigation_id, "json")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="application/json",
        filename=f"investigation-{investigation_id}.json",
    )


@router.get("/investigations/{investigation_id}/report")
def get_investigation_report(investigation_id: str) -> dict:
    report = InvestigationHistoryService().read_report(investigation_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return report


@router.post("/investigations/{investigation_id}/regenerate")
def regenerate_investigation_report(investigation_id: str) -> dict:
    report = InvestigationHistoryService().regenerate(investigation_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return {"status": "success", "report": report}


@router.get("/investigations/{investigation_id}/markdown")
def download_investigation_markdown(investigation_id: str) -> FileResponse:
    report_path = InvestigationHistoryService().report_path(investigation_id, "markdown")
    if report_path is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")

    return FileResponse(
        report_path,
        media_type="text/markdown",
        filename=f"investigation-{investigation_id}.md",
    )
