import asyncio

from loguru import logger

from app.auth.models import Principal
from app.jobs.models import InvestigationJob, JobEvent, JobEventType
from app.jobs.store import InvestigationJobStore
from app.models.investigation import InvestigationRequest
from app.services.investigation_runner import (
    FAILURE_DETAIL,
    collection_failure,
    run_investigation,
)


class JobProgressReporter:
    """Bridges collection progress into a job's event stream."""

    def __init__(self, store: InvestigationJobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def report(self, message: str, **data) -> None:
        self._store.publish(
            self._job_id,
            JobEvent(JobEventType.PROGRESS, message, data=data),
        )


class InvestigationJobRunner:
    """Submits investigations as background tasks and tracks their lifecycle."""

    def __init__(self, store: InvestigationJobStore) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(
        self,
        request: InvestigationRequest | None,
        principal: Principal | None = None,
    ) -> InvestigationJob:
        payload = request.model_dump() if request else {}
        job = self.store.create(payload, owner=principal.subject if principal else "")

        task = asyncio.create_task(self._execute(job.id, request, principal))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))

        return job

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def _execute(
        self,
        job_id: str,
        request: InvestigationRequest | None,
        principal: Principal | None = None,
    ) -> None:
        self.store.mark_running(job_id)
        reporter = JobProgressReporter(self.store, job_id)

        try:
            result = await run_investigation(
                request,
                reporter=reporter,
                investigation_id=job_id,
                principal=principal,
            )
        except asyncio.CancelledError:
            logger.info("Investigation job {id} cancelled", id=job_id)
            self.store.mark_cancelled(job_id)
            raise
        except Exception as exc:
            logger.opt(exception=exc).error("Investigation job {id} failed", id=job_id)
            self.store.mark_failed(job_id, FAILURE_DETAIL)
            return

        failure = collection_failure(result["investigation"])
        if failure:
            self.store.mark_failed(job_id, failure)
            return

        self.store.mark_succeeded(job_id, result)


_default_runner: InvestigationJobRunner | None = None


def get_job_runner() -> InvestigationJobRunner:
    """FastAPI dependency; overridden in tests."""
    global _default_runner
    if _default_runner is None:
        from app.jobs.store import get_job_store

        _default_runner = InvestigationJobRunner(get_job_store())
    return _default_runner
