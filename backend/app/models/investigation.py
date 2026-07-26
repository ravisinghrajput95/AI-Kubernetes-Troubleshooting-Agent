from typing import Any

from pydantic import BaseModel


class InvestigationResponse(BaseModel):
    status: str
    investigation: dict[str, Any]
    diagnosis: dict[str, Any] | None = None
    history_item: dict[str, Any] | None = None


class InvestigationRequest(BaseModel):
    context: str | None = None
    namespace: str | None = None
    resource_kind: str | None = None
    resource_name: str | None = None


class InvestigationJobAccepted(BaseModel):
    """202 response for an asynchronously submitted investigation.

    `id` is both the job id and the id the finished report is stored under, so
    the report endpoints resolve once the job succeeds.
    """

    id: str
    status: str
    status_url: str
    events_url: str


class InvestigationHistoryItem(BaseModel):
    id: str
    timestamp: str
    root_cause: str
    namespace: str
    confidence: int
    status: str
    pdf_url: str
