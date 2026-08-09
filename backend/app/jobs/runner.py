import asyncio
import contextlib
import time

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.core.correlation import bind, correlation_scope
from app.jobs.models import InvestigationJob, JobEvent, JobEventType
from app.models.investigation import InvestigationRequest
from app.notify import announce
from app.observability import metrics
from app.observability.tracing import span
from app.providers.base import ClusterUnreachable
from app.services.investigation_runner import (
    FAILURE_DETAIL,
    collection_failure,
    run_investigation,
)

WORKER_LOST = "Investigation worker stopped before the run finished."


def agent_affinity(request: InvestigationRequest | None) -> str:
    """The one worker that can collect this cluster, or "" for anyone.

    A gRPC stream belongs to whichever worker holds the socket, so an
    investigation of an agent-connected cluster has exactly one worker able to
    run it. Before M8a nothing expressed that: the job went on the shared
    queue, any worker claimed it, and a worker without the stream fell back to
    the local kubeconfig — so on three replicas roughly two thirds of
    agent-cluster investigations were answered by the platform's own kubeconfig
    rather than by the cluster.

    Returning "" is always safe. It means "the shared queue", which is correct
    for kubeconfig clusters and merely slower for the rest, since
    `select_provider` refuses rather than reading the wrong cluster.

    Imported lazily so a deployment with no gateway never loads the presence
    index to answer a question that can only be "no".
    """
    if not settings.agent_gateway_enabled:
        return ""

    context = (request.context if request else "") or ""
    if not context:
        # No cluster named means the local kubeconfig's current context, which
        # no agent can serve.
        return ""

    from app.gateway.presence import get_agent_presence
    from app.gateway.session import get_agent_registry
    from app.tenancy import current_tenant

    presence = get_agent_presence()
    if presence is None:
        # Single-process, or no Redis: this worker is the fleet, so affinity is
        # already satisfied and naming it would only add a queue.
        return ""

    # **Ask the local registry first, exactly as `select_provider` does.**
    #
    # `holder()` deliberately returns nothing when the presence record names
    # *this* worker — for `select_provider` that is right, because it only
    # reaches `holder()` after the local registry has said no, so a record
    # still claiming us is stale. This function had no such check, so a submit
    # that landed on the worker actually holding the stream got "" and went to
    # the **shared** queue, where any worker could claim it.
    #
    # Measured in-cluster on two replicas: one investigation in three reached
    # the agent, the rest were refused with "attached to worker X, not this
    # one" — where X was the worker that had accepted the submission. The
    # refusal is correct and the routing that should make it rare was
    # inverted: landing on the right worker was precisely the case that
    # un-pinned the job.
    if get_agent_registry().get(context) is not None:
        return presence.worker_id

    return presence.holder(current_tenant(), context) or ""


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

        # From here on the submitting request is the investigation: its
        # remaining log lines and the `X-Correlation-ID` it returns both use
        # this id, so a user quoting the header finds the collection and the
        # report under it too. The background task does not depend on this —
        # `_execute` re-establishes the id from the job, which is the only
        # thing that works on a worker that never saw the request.
        bind(job.id)

        metrics.investigation_submitted()
        if self.store.distributed:
            self.store.enqueue(job.id, agent_affinity(request))
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
        metrics.running(len(self._tasks))
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

    async def drain(self, timeout: float) -> int:
        """Let in-flight investigations finish. Returns how many did not.

        `_stopping` is set first, so nothing new starts while we wait — a drain
        that kept accepting work would never end on a busy worker.

        The deadline is a bound, not a promise. An investigation blocked on an
        unreachable cluster will still be running when it expires, and
        `shutdown()` cancels it exactly as it did before. What the wait buys is
        the common case: at a measured 0.223s per investigation, everything
        in flight is finished long before a 30s deadline, and the user gets a
        result rather than "worker stopped before the run finished".

        Deliberately does not cancel on timeout. That is `shutdown()`'s job,
        and keeping the two separate is what lets a caller drain without
        committing to tearing down — which is what makes this testable at all.
        """
        self._stopping = True
        pending = [task for task in self._tasks.values() if not task.done()]
        if not pending or timeout <= 0:
            return len(pending)

        logger.info(
            "Draining {count} in-flight investigation(s), up to {timeout:.0f}s",
            count=len(pending),
            timeout=timeout,
        )
        done, still_running = await asyncio.wait(pending, timeout=timeout)

        if still_running:
            logger.warning(
                "Drain deadline reached with {count} investigation(s) still running; "
                "they will be recorded as a lost worker",
                count=len(still_running),
            )
        else:
            logger.info("Drained {count} investigation(s) cleanly", count=len(done))

        return len(still_running)

    async def shutdown(self) -> None:
        """Stop in-flight investigations before their backing store goes away.

        Whatever is still running here is recorded as a lost worker rather than
        a cancellation: nobody asked for these to stop, and calling a deploy a
        user cancellation would misreport it. It is the same outcome another
        worker's reaper would reach on its own once the lease expired — this
        just gets there immediately.

        Draining happens in `StateBackend.shutdown()` before this is called,
        and not here, because it has to be sequenced against the queue consumer
        stopping. Waiting here while the consumer is still claiming would
        refill the worker as fast as it emptied.
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
        metrics.running(len(self._tasks))
        # A task cancelled before its coroutine ever started has no chance to
        # record the outcome itself, and the job would sit pending forever.
        if task.cancelled():
            # Only the status is read; the payload would be pure transfer.
            job = self.store.get_summary(job_id)
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
        # Belt and braces with `submit`'s `bind`, and the only thing covering
        # the distributed path: a job claimed from the queue runs on a worker
        # that never saw the request, so the id has to be re-established from
        # the job rather than inherited. Cheap enough to do in both places, and
        # doing it only in `submit` would silently lose the id on every
        # multi-worker deployment — which is the deployment where interleaved
        # logs make it matter most.
        with correlation_scope(job_id):
            await self._run_execute(job_id, request, principal, already_running)

    async def _run_execute(
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
        # Timed here rather than from the stored row: this is the process that
        # actually ran it, and the row's timestamps are the database's clock on
        # a worker that may not be this one.
        started = time.perf_counter()

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
                metrics.investigation_finished("worker_lost", time.perf_counter() - started)
            else:
                logger.info("Investigation job {id} cancelled", id=job_id)
                self.store.mark_cancelled(job_id)
                metrics.investigation_finished("cancelled", time.perf_counter() - started)
            raise
        except ClusterUnreachable as exc:
            # Surfaced verbatim. The generic detail tells an operator to check
            # their kubeconfig, which is exactly the wrong thing to do when the
            # cluster is reachable and simply attached to another worker.
            logger.warning("Investigation job {id} refused: {reason}", id=job_id, reason=str(exc))
            self.store.mark_failed(job_id, str(exc))
            metrics.investigation_finished("unreachable", time.perf_counter() - started)
            return
        except Exception as exc:
            logger.opt(exception=exc).error("Investigation job {id} failed", id=job_id)
            self.store.mark_failed(job_id, FAILURE_DETAIL)
            metrics.investigation_finished("failed", time.perf_counter() - started)
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
            metrics.investigation_finished("no_evidence", time.perf_counter() - started)
            announce(job_id, "failed", result.get("investigation"), result.get("diagnosis"))
            return

        # The result carries the whole investigation; writing it can be slow
        # enough to matter, so keep it off the event loop.
        with span("persist"):
            await asyncio.to_thread(self.store.mark_succeeded, job_id, result)
        metrics.investigation_finished("succeeded", time.perf_counter() - started)
        # After the result is durable, never before: an announcement describes
        # something that happened, and firing it first would let a receiver
        # follow a link to an investigation that had not been written yet.
        announce(job_id, "succeeded", result.get("investigation"), result.get("diagnosis"))

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
