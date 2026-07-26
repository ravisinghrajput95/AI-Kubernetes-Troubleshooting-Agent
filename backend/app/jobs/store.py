"""In-process store for investigation jobs, with per-job event fan-out.

Scope and limits — deliberate, and important to understand before deploying:

- State lives in the process. Jobs do not survive a restart, and a deployment
  running multiple uvicorn workers will not find a job created by another
  worker. Single-process deployment is the supported topology today.
- Replacing this with Redis or a database means implementing `create/get/list/
  publish/subscribe`; nothing above this layer knows how state is stored.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from loguru import logger

from app.jobs.models import InvestigationJob, JobEvent, JobEventType, JobStatus

DEFAULT_MAX_JOBS = 100
SUBSCRIBER_QUEUE_SIZE = 256


class InvestigationJobStore:
    def __init__(self, max_jobs: int = DEFAULT_MAX_JOBS) -> None:
        self.max_jobs = max_jobs
        self._jobs: dict[str, InvestigationJob] = {}
        self._subscribers: dict[str, list[asyncio.Queue[JobEvent | None]]] = {}

    def create(self, request: dict, owner: str = "") -> InvestigationJob:
        job = InvestigationJob(id=str(uuid4()), request=request, owner=owner)
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

    def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.error = error
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

    def publish(self, job_id: str, event: JobEvent) -> None:
        """Record an event and fan it out to live subscribers.

        Never blocks: a subscriber that is not draining fast enough loses the
        event rather than stalling the investigation.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

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
    ) -> AsyncIterator[JobEvent | None]:
        """Yield the events so far, then live events until the job finishes.

        Replaying the backlog first means a client that connects after the run
        started still sees the whole timeline. When `heartbeat` is set, `None` is
        yielded on each idle interval so the caller can keep the connection
        alive through intermediary proxies.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return

        for event in list(job.events):
            yield event

        if job.status.terminal:
            return

        queue: asyncio.Queue[JobEvent | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.setdefault(job_id, []).append(queue)

        try:
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


_default_store = InvestigationJobStore()


def get_job_store() -> InvestigationJobStore:
    """FastAPI dependency; overridden in tests."""
    return _default_store
