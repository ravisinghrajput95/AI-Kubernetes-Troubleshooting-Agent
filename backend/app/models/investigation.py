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


class InvestigationHistoryItem(BaseModel):
    id: str
    timestamp: str
    root_cause: str
    namespace: str
    confidence: int
    status: str
    pdf_url: str
