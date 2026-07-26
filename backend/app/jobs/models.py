from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobEventType(StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobEvent:
    """A single progress notification for a running investigation."""

    type: JobEventType
    message: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "message": self.message,
            "at": self.at.isoformat(),
            "time": self.at.astimezone().strftime("%H:%M:%S"),
            "data": self.data,
        }


@dataclass
class InvestigationJob:
    """Lifecycle state for one investigation.

    The job id is also the investigation id: when the run completes, the report
    is persisted under this same id, so the existing report endpoints resolve
    without a second identifier space.
    """

    id: str
    request: dict[str, Any]
    # Subject of the caller who submitted it; empty when auth is disabled.
    owner: str = ""
    status: JobStatus = JobStatus.PENDING
    events: list[JobEvent] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def progress(self) -> dict[str, Any]:
        completed = sum(1 for event in self.events if event.type is JobEventType.PROGRESS)
        return {"completed_steps": completed, "total_events": len(self.events)}

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "owner": self.owner,
            "status": str(self.status),
            "request": self.request,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self._duration_ms(),
            "progress": self.progress,
            "timeline": [event.to_dict() for event in self.events],
            "error": self.error,
        }

        if include_result and self.result is not None:
            payload.update(self.result)

        return payload

    def _duration_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds() * 1000)
