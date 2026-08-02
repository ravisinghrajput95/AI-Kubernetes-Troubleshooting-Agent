"""The job store seam, and the one piece of ordering logic both sides share.

Two implementations satisfy `JobStore`: `InMemoryJobStore` (single process, no
infrastructure) and `PostgresRedisJobStore` (multi-worker, durable). Everything
above this layer — the API handlers and the job runner — is written against the
protocol and does not know which one it has.
"""

from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

from app.jobs.models import InvestigationJob, JobEvent


class EventSequencer:
    """Merges a replayed backlog with a live feed, losing and repeating nothing.

    The correctness of an SSE stream that spans workers rests on two things,
    and this class is the second of them:

    1. The caller subscribes to the live feed **before** reading the backlog,
       buffering whatever arrives in between. That is what makes it impossible
       to drop an event published during the read.
    2. Every event carries a monotonic `seq`, and anything at or below the
       high-water mark has already been delivered. That is what makes it
       impossible to deliver the same event twice — which step 1, on its own,
       guarantees will happen.

    Kept free of I/O so the ordering argument can be tested without a database.
    """

    def __init__(self, after_seq: int = 0) -> None:
        self._position = max(0, after_seq)

    @property
    def position(self) -> int:
        """The highest sequence delivered so far; a resume cursor."""
        return self._position

    def accept(self, event: JobEvent) -> bool:
        """True if this event has not been delivered yet.

        An unsequenced event (`seq == 0`) is always accepted: the in-process
        store assigns sequences, but a caller constructing an event by hand
        should not be silently swallowed.
        """
        if event.seq == 0:
            return True
        if event.seq <= self._position:
            return False
        self._position = event.seq
        return True


CancelListener = Callable[[str], None]


@runtime_checkable
class JobStore(Protocol):
    """Lifecycle, history and progress fan-out for investigation jobs."""

    # False when submitting a job means running it in this process; True when a
    # queue hands it to whichever worker claims it first.
    distributed: bool

    def create(
        self,
        request: dict[str, Any],
        owner: str = "",
        principal: dict[str, Any] | None = None,
    ) -> InvestigationJob: ...

    def get(self, job_id: str) -> InvestigationJob | None: ...

    def list(self, limit: int = 25, owner: str | None = None) -> list[InvestigationJob]: ...

    def mark_running(self, job_id: str) -> None: ...

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None: ...

    def mark_failed(
        self,
        job_id: str,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Record a failure, optionally with whatever the run did produce.

        A collection failure still has an investigation behind it — degraded
        evidence, and the reason each collector could not answer. Carrying it
        means a failed job explains itself instead of only naming the error.
        """
        ...

    def mark_cancelled(self, job_id: str) -> None: ...

    def publish(self, job_id: str, event: JobEvent) -> None: ...

    def subscribe(
        self,
        job_id: str,
        heartbeat: float | None = None,
        after_seq: int = 0,
    ) -> AsyncIterator[JobEvent | None]: ...

    def request_cancel(self, job_id: str) -> bool:
        """Record that this job should stop, and tell whoever is running it.

        Returns False only for an unknown job. It does **not** report whether
        a worker acted: across processes that is not observable at the moment
        of the request, and the durable flag means it will be acted on.
        """
        ...

    def on_cancel(self, listener: CancelListener) -> None:
        """Register a callback invoked, on the event loop thread, when a
        cancellation is requested for any job. The runner uses it to cancel the
        asyncio task if the job belongs to this process."""
        ...

    def enqueue(self, job_id: str, worker_id: str = "") -> None:
        """Offer the job to the queue. A no-op for the in-process store.

        `worker_id` names the one worker that can usefully run this job, which
        is the case only when the cluster is reachable through an agent whose
        stream that worker holds. Empty means anyone may take it.
        """
        ...
