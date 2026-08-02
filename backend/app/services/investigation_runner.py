"""The investigation pipeline, shared by the synchronous and job-based APIs.

Both entry points run exactly this function, so the two paths cannot drift.
"""

import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

from app.ai.root_cause_analyzer import RootCauseAnalyzer
from app.auth.models import Principal
from app.collectors.base import ProgressReporter
from app.models.investigation import InvestigationRequest
from app.observability.tracing import span
from app.services.history_service import InvestigationHistoryService
from app.services.investigation_service import InvestigationService

FAILURE_DETAIL = (
    "Investigation failed unexpectedly. Verify kubeconfig, cluster "
    "access, kubectl permissions, backend logs, and OpenAI API settings."
)


def collection_failure(investigation: dict[str, Any]) -> str | None:
    """Message when collection produced nothing usable at all, else `None`.

    Partial degradation is normal and is handled per collector. Zero usable
    evidence is different in kind: there is nothing to reason over, so
    presenting the run as successful would misrepresent it. The synchronous
    endpoint deliberately still returns 200 with `health.status == "error"` to
    preserve its existing contract; the job API reports it as a failure.
    """
    coverage = investigation.get("evidence_coverage", {})
    if not isinstance(coverage, dict) or not coverage.get("total"):
        return None
    if coverage.get("usable", 0) > 0:
        return None

    return investigation.get("health", {}).get("message") or FAILURE_DETAIL


async def run_investigation(
    request: InvestigationRequest | None = None,
    reporter: ProgressReporter | None = None,
    investigation_id: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Collect evidence, diagnose, and persist a report.

    `investigation_id` lets a caller pin the stored report to an id it already
    published — the job API uses this so the job id and the report id are the
    same resource.
    """
    service = InvestigationService(
        context=request.context if request else None,
        namespace=request.namespace if request else None,
        resource_kind=request.resource_kind if request else None,
        resource_name=request.resource_name if request else None,
        reporter=reporter,
        principal=principal,
    )

    with span(
        "collect",
        cluster=str(request.context if request else ""),
        investigation=investigation_id or "",
    ):
        investigation = await service.run()

    if reporter is not None:
        reporter.report("Analyzing root cause")

    # Reasoning and report writing both block; keep them off the event loop.
    with span("analyse"):
        diagnosis = await asyncio.to_thread(RootCauseAnalyzer().analyze, investigation)
    investigation["timeline"].append(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": "Root Cause Generated",
        }
    )

    if reporter is not None:
        reporter.report("Root Cause Generated")

    with span("report"):
        history_item = await asyncio.to_thread(
            InvestigationHistoryService().save,
            diagnosis,
            investigation,
            "success",
            investigation_id,
            principal.subject if principal else "",
        )

    if reporter is not None:
        reporter.report("Report generated")

    logger.info("Investigation finished for context={context}", context=service.context)
    return {
        "investigation": investigation,
        "diagnosis": diagnosis,
        "history_item": history_item,
    }
