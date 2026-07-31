"""In-process store for investigation jobs, with per-job event fan-out.

Scope and limits — deliberate, and important to understand before deploying:

- State lives in the process. Jobs do not survive a restart, and a deployment
  running multiple uvicorn workers will not find a job created by another
  worker. Single-process deployment is the supported topology for this store,
  and it is the default: it needs no infrastructure, so `uvicorn app.main:app
  --reload` works against nothing but a kubeconfig.
- For multi-worker deployments set `DATABASE_URL` and `REDIS_URL`, which
  selects `PostgresRedisJobStore` instead. Both satisfy `JobStore`; nothing
  above this layer knows which one it has.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from loguru import logger

from app.jobs.base import CancelListener, EventSequencer
from app.jobs.models import InvestigationJob, JobEvent, JobEventType, JobStatus

DEFAULT_MAX_JOBS = 100
SUBSCRIBER_QUEUE_SIZE = 256


class InMemoryJobStore:
    distributed = False

    def __init__(self, max_jobs: int = DEFAULT_MAX_JOBS) -> None:
        self.max_jobs = max_jobs
        self._jobs: dict[str, InvestigationJob] = {}
        self._subscribers: dict[str, list[asyncio.Queue[JobEvent | None]]] = {}
        self._sequences: dict[str, int] = {}
        self._cancel_listeners: list[CancelListener] = []

    def create(
        self,
        request: dict[str, Any],
        owner: str = "",
        principal: dict[str, Any] | None = None,
    ) -> InvestigationJob:
        job = InvestigationJob(
            id=str(uuid4()),
            request=request,
            owner=owner,
            principal=principal,
        )
        self._jobs[job.id] = job
        self._evict()
        self.publish(job.id, JobEvent(JobEventType.QUEUED, "Investigation queued"))
        return job

    def get(self, job_id: str) -> InvestigationJob | None:
        return self._jobs.get(job_id)

    def list(self, limit: int = 25, owner: str | None = None) -> list[InvestigationJob]:
        """Recent jobs, optionally restricted to one owner.

        `owner=None` returns everything and is for internal callers; API
        handlers pass the caller's subject.
        """
        jobs = [
            job
            for job in self._jobs.values()
            if owner is None or not job.owner or job.owner == owner
        ]
        ordered = sorted(jobs, key=lambda job: job.created_at, reverse=True)
        return ordered[:limit]

    def mark_running(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        self.publish(job_id, JobEvent(JobEventType.STARTED, "Investigation started"))

    def mark_succeeded(self, job_id: str, result: dict) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.SUCCEEDED
        job.result = result
        job.finished_at = datetime.now(UTC)
        self.publish(job_id, JobEvent(JobEventType.COMPLETED, "Investigation complete"))
        self._close(job_id)

    def mark_failed(
        self,
        job_id: str,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.error = error
        if result is not None:
            job.result = result
        job.finished_at = datetime.now(UTC)
        self.publish(job_id, JobEvent(JobEventType.FAILED, error))
        self._close(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.CANCELLED
        job.finished_at = datetime.now(UTC)
        self.publish(job_id, JobEvent(JobEventType.CANCELLED, "Investigation cancelled"))
        self._close(job_id)

    def request_cancel(self, job_id: str) -> bool:
        """Flag the job and notify whoever is running it.

        In this store "whoever" is always this process, so the listeners fire
        synchronously. The distributed store publishes a message instead; the
        endpoint calling this cannot tell the difference, which is the point.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_requested = True
        self._notify_cancel(job_id)
        return True

    def on_cancel(self, listener: CancelListener) -> None:
        self._cancel_listeners.append(listener)

    def enqueue(self, job_id: str) -> None:
        """No queue here: the submitting process runs the job itself."""

    def _notify_cancel(self, job_id: str) -> None:
        for listener in self._cancel_listeners:
            try:
                listener(job_id)
            except Exception as exc:  # a broken listener must not block the cancel
                logger.opt(exception=exc).warning(
                    "Cancellation listener failed for {id}", id=job_id
                )

    def publish(self, job_id: str, event: JobEvent) -> None:
        """Record an event and fan it out to live subscribers.

        Never blocks: a subscriber that is not draining fast enough loses the
        event rather than stalling the investigation.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

        sequence = self._sequences.get(job_id, 0) + 1
        self._sequences[job_id] = sequence
        event = replace(event, seq=sequence)

        job.events.append(event)

        for queue in self._subscribers.get(job_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for slow subscriber on job {id}", id=job_id)

    async def subscribe(
        self,
        job_id: str,
        heartbeat: float | None = None,
        after_seq: int = 0,
    ) -> AsyncIterator[JobEvent | None]:
        """Yield the events so far, then live events until the job finishes.

        Replaying the backlog first means a client that connects after the run
        started still sees the whole timeline. `after_seq` resumes from a
        position the caller already has, so a reconnecting browser is not sent
        the timeline twice. When `heartbeat` is set, `None` is yielded on each
        idle interval so the caller can keep the connection alive through
        intermediary proxies.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

        sequencer = EventSequencer(after_seq)

        # Registering before the replay is what the distributed store must do
        # to avoid a gap; doing it here too keeps the two behaviours identical.
        queue: asyncio.Queue[JobEvent | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.setdefault(job_id, []).append(queue)

        try:
            for event in list(job.events):
                if sequencer.accept(event):
                    yield event

            if job.status.terminal:
                return

            while True:
                if heartbeat is None:
                    event = await queue.get()
                else:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except TimeoutError:
                        yield None
                        continue
                if event is None:
                    return
                if sequencer.accept(event):
                    yield event
        finally:
            subscribers = self._subscribers.get(job_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

    def _close(self, job_id: str) -> None:
        for queue in self._subscribers.get(job_id, []):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.warning("Could not signal completion to subscriber on {id}", id=job_id)

    def _evict(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return

        terminal = sorted(
            (job for job in self._jobs.values() if job.status.terminal),
            key=lambda job: job.finished_at or job.created_at,
        )
        for job in terminal[: len(self._jobs) - self.max_jobs]:
            self._jobs.pop(job.id, None)
            self._subscribers.pop(job.id, None)
            self._sequences.pop(job.id, None)


# The original name, kept because it is what the tests and the docs call it.
InvestigationJobStore = InMemoryJobStore

_default_store: InMemoryJobStore | None = None


def get_job_store():
    """FastAPI dependency; overridden in tests.

    Returns the in-process store unless application startup installed a
    distributed one. Constructing it lazily rather than at import keeps
    `python -m pytest` free of any dependency on Postgres or Redis.
    """
    global _default_store
    if _default_store is None:
        _default_store = InMemoryJobStore()
    return _default_store


def set_job_store(store) -> None:
    """Install the process-wide store. Called from application startup.

    `None` un-installs it, so the next caller gets a fresh in-process store
    rather than one whose backing connections have been closed.
    """
    global _default_store
    _default_store = store
