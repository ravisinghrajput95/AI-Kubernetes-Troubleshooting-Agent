from typing import Any

from pydantic import BaseModel, Field

# Every field on `InvestigationRequest` names a Kubernetes object, and RFC 1123
# caps those at 253 characters. 512 is deliberately generous against that — the
# bound exists to stop a caller storing arbitrary data, not to second-guess a
# name — while still being three orders of magnitude below what was accepted
# before it.
#
# Unbounded was not merely untidy. A 1 MB `context` was accepted with a 202 and
# written into the job row's `request` jsonb *and* into the audit log, which is
# append-only by design and therefore the one store that must stay bounded
# regardless of what the API lets through. See `docs/QA_AUDIT_2026-08-03.md`.
MAX_IDENTIFIER_LENGTH = 512


class InvestigationResponse(BaseModel):
    status: str
    investigation: dict[str, Any]
    diagnosis: dict[str, Any] | None = None
    history_item: dict[str, Any] | None = None


class InvestigationRequest(BaseModel):
    context: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    namespace: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    resource_kind: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    resource_name: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)


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
