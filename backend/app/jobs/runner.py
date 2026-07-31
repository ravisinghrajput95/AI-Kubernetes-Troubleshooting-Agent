import asyncio
import contextlib

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.jobs.models import InvestigationJob, JobEvent, JobEventType
from app.models.investigation import InvestigationRequest
from app.services.investigation_runner import (
    FAILURE_DETAIL,
    collection_failure,
    run_investigation,
)

WORKER_LOST = "Investigation worker stopped before the run finished."


class JobProgressReporter:
    """Bridges collection progress into a job's event stream."""

    def __init__(self, store, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def report(self, message: str, **data) -> None:
        self._store.publish(
            self._job_id,
            JobEvent(JobEventType.PROGRESS, message, data=data),
        )


class InvestigationJobRunner:
    """Runs investigations as background tasks and tracks their lifecycle.

    Where the work runs depends on the store. With the in-process store the
    submitting worker runs it immediately. With the distributed store the job
    is queued and whichever worker claims it runs it — which is why
    cancellation cannot be a method call any more, and arrives as a message
    that this runner turns back into a `Task.cancel()` locally.
    """

    def __init__(self, store) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = False
        # A cancel for a job this process owns must reach this process's event
        # loop. Registering here is what closes that loop, for both stores.
        store.on_cancel(self.cancel)

    @property
    def busy(self) -> int:
        """How many investigations this worker is currently running."""
        return len(self._tasks)

    def submit(
        self,
        request: InvestigationRequest | None,
        principal: Principal | None = None,
    ) -> InvestigationJob:
        payload = request.model_dump() if request else {}
        job = self.store.create(
            payload,
            owner=principal.subject if principal else "",
            principal=principal.to_dict() if principal else None,
        )

        if self.store.distributed:
            # Any worker may pick this up, including this one.
            self.store.enqueue(job.id)
        else:
            self.start(job.id, request, principal)

        return job

    def start(
        self,
        job_id: str,
        request: InvestigationRequest | None,
        principal: Principal | None = None,
        already_running: bool = False,
    ) -> asyncio.Task:
        """Run this job in this process. Requires the event loop thread."""
        task = asyncio.create_task(self._execute(job_id, request, principal, already_running))
        self._tasks[job_id] = task
        task.add_done_callback(lambda finished: self._finished(job_id, finished))
        return task

    def cancel(self, job_id: str) -> bool:
        """Cancel the task if this process is the one running it.

        Returns False when the job belongs to another worker, which is not an
        error: that worker's own runner receives the same message.
        """
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        """Stop in-flight investigations before their backing store goes away.

        Recorded as a lost worker rather than a cancellation: nobody asked for
        these to stop, and calling a deploy a user cancellation would misreport
        it. It is the same outcome another worker's reaper would reach on its
        own once the lease expired — this just gets there immediately.
        """
        self._stopping = True
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Done callbacks are scheduled on the loop rather than run inline, so
        # clear here instead of waiting for them to catch up.
        self._tasks.clear()

    def _finished(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(job_id, None)
        # A task cancelled before its coroutine ever started has no chance to
        # record the outcome itself, and the job would sit pending forever.
        if task.cancelled():
            job = self.store.get(job_id)
            if job is not None and not job.status.terminal:
                if self._stopping:
                    self.store.mark_failed(job_id, WORKER_LOST)
                else:
                    self.store.mark_cancelled(job_id)

    async def _execute(
        self,
        job_id: str,
        request: InvestigationRequest | None,
        principal: Principal | None = None,
        already_running: bool = False,
    ) -> None:
        if not already_running:
            self.store.mark_running(job_id)
        reporter = JobProgressReporter(self.store, job_id)
        watchdog = self._start_watchdog(job_id)

        try:
            result = await run_investigation(
                request,
                reporter=reporter,
                investigation_id=job_id,
                principal=principal,
            )
        except asyncio.CancelledError:
            # Recorded synchronously: this task is being torn down, so there is
            # no opportunity to await anything after this point.
            if self._stopping:
                logger.info("Investigation job {id} stopped by shutdown", id=job_id)
                self.store.mark_failed(job_id, WORKER_LOST)
            else:
                logger.info("Investigation job {id} cancelled", id=job_id)
                self.store.mark_cancelled(job_id)
            raise
        except Exception as exc:
            logger.opt(exception=exc).error("Investigation job {id} failed", id=job_id)
            self.store.mark_failed(job_id, FAILURE_DETAIL)
            return
        finally:
            # Cancelled, not awaited: this block also runs while a cancellation
            # is propagating, and a new await point there is a good way to lose
            # the exception that is being delivered.
            if watchdog is not None:
                watchdog.cancel()

        failure = collection_failure(result["investigation"])
        if failure:
            # The result goes with the failure. Collection produced degraded
            # evidence and a reason for every gap, and that is exactly what an
            # operator needs to see; it is also what the persisted-report
            # fallback already returns, so the same id cannot answer with two
            # different shapes depending on whether the job is still in memory.
            await asyncio.to_thread(self.store.mark_failed, job_id, failure, result)
            return

        # The result carries the whole investigation; writing it can be slow
        # enough to matter, so keep it off the event loop.
        await asyncio.to_thread(self.store.mark_succeeded, job_id, result)

    def _start_watchdog(self, job_id: str) -> asyncio.Task | None:
        """Poll for a cancellation whose message never arrived, and hold the lease.

        Only meaningful for the distributed store. The Redis control message
        cancels in milliseconds; this bounds the worst case when that message
        is lost, which is what makes cancellation a guarantee rather than a
        best effort.
        """
        if not self.store.distributed:
            return None
        return asyncio.create_task(self._watch(job_id))

    async def _watch(self, job_id: str) -> None:
        interval = max(0.25, settings.job_cancel_poll_seconds)
        lease = max(interval, settings.job_lease_seconds / 3)
        since_renewal = 0.0

        while True:
            await asyncio.sleep(interval)
            try:
                if await asyncio.to_thread(self.store.is_cancel_requested, job_id):
                    logger.info("Cancellation for {id} found by watchdog", id=job_id)
                    self.cancel(job_id)
                    return

                since_renewal += interval
                if since_renewal >= lease:
                    since_renewal = 0.0
                    await asyncio.to_thread(
                        self.store.renew_lease,
                        job_id,
                        settings.worker_id,
                        settings.job_lease_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A watchdog that dies must not take the investigation with it.
                logger.opt(exception=exc).warning("Job watchdog error for {id}", id=job_id)


_default_runner: InvestigationJobRunner | None = None


def get_job_runner() -> InvestigationJobRunner:
    """FastAPI dependency; overridden in tests."""
    global _default_runner
    if _default_runner is None:
        from app.jobs.store import get_job_store

        _default_runner = InvestigationJobRunner(get_job_store())
    return _default_runner


def set_job_runner(runner: InvestigationJobRunner | None) -> None:
    """Install the process-wide runner. Called from application startup."""
    global _default_runner
    _default_runner = runner
