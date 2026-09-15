"""The background loops that make a multi-worker deployment work.

Three of them, started at application startup when distributed state is
configured and cancelled at shutdown:

- **queue** — claim jobs and run them here.
- **control** — turn a cancel message into a `Task.cancel()` on this loop.
- **reaper** — finish jobs whose worker died, re-offer jobs the queue lost.

None of them exist in the single-process deployment, where the worker that
accepts a submission is by definition the worker that runs it.
"""

import asyncio
import contextlib
import time

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.jobs.distributed import PostgresRedisJobStore
from app.jobs.runner import WORKER_LOST, InvestigationJobRunner
from app.models.investigation import InvestigationRequest
from app.observability import metrics
from app.persistence.redis_bus import RedisBus
from app.tenancy import DEFAULT_TENANT, system_scope, tenant_scope

REAPER_INTERVAL_SECONDS = 15.0
# How long a job may sit `pending` before the queue message is presumed lost.
# Comfortably longer than a normal claim, short enough that a dropped message
# is a hiccup rather than an outage.
#
# Since M8a it is also what un-strands a job routed to a worker that then died:
# the re-offer goes to the **shared** queue, never back to a worker queue. That
# is only terminating because `PRESENCE_TTL_SECONDS` (45) is smaller — by the
# time this fires, the dead worker's agents have lapsed from the presence index,
# so nothing routes the job back to it. Raising the TTL above this value would
# turn recovery into a loop; `tests/test_agent_routing.py` asserts the ordering.
UNCLAIMED_GRACE_SECONDS = 60


class JobConsumer:
    def __init__(
        self,
        store: PostgresRedisJobStore,
        runner: InvestigationJobRunner,
        bus: RedisBus,
        worker_id: str,
        max_concurrent: int | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._bus = bus
        self._worker = worker_id
        # `None` means "whatever this deployment is configured for", so the
        # value lives in one place instead of being a constructor default that
        # nothing overrides — which is how it stayed at 4 and became the
        # platform's concurrency ceiling.
        self._max_concurrent = (
            max_concurrent if max_concurrent is not None else settings.job_max_concurrent
        )
        self._tasks: list[asyncio.Task] = []
        # When the store was last seen answering after being unreachable, or
        # `None` while it has not been. See `_reap`.
        self._store_answering_since: float | None = None

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._forever("queue", self._consume_queue)),
            asyncio.create_task(self._forever("control", self._consume_control)),
            asyncio.create_task(self._forever("reaper", self._reap)),
        ]
        metrics.capacity(self._max_concurrent)
        logger.info(
            "Job consumer started as worker {worker}, running up to {limit} investigations at once",
            worker=self._worker,
            limit=self._max_concurrent,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _forever(self, name: str, loop) -> None:
        """Restart a loop that raises, rather than losing it silently.

        A consumer that dies leaves a worker that accepts submissions and never
        runs them, which looks like a healthy replica.
        """
        while True:
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.opt(exception=exc).error("Job {name} loop failed; restarting", name=name)
                await asyncio.sleep(1.0)

    # --- queue --------------------------------------------------------------

    async def _consume_queue(self) -> None:
        while True:
            if self._runner.busy >= self._max_concurrent:
                # Leave the id on the queue for a worker with capacity.
                await asyncio.sleep(0.25)
                continue

            # This worker's own queue first, then the shared one. `BLPOP` takes
            # both keys and returns from the first non-empty, so affinity gets
            # its priority in the same round trip that was already happening.
            job_id = await self._bus.dequeue(worker_id=self._worker)
            if job_id is None:
                continue
            await self._claim_and_run(job_id)

    async def _claim_and_run(self, job_id: str) -> None:
        # Claiming is genuinely cross-tenant: the queue hands out an id, and
        # the row that names a tenant is the one being claimed. This is one of
        # the two `system_scope()` callers, and both are here.
        with system_scope():
            job = await asyncio.to_thread(
                self._store.claim, job_id, self._worker, settings.job_lease_seconds
            )
        if job is None:
            # Another worker won the claim, the job already finished, or it was
            # cancelled before anyone started it. Only the last needs settling.
            await self._settle_unclaimable(job_id)
            return

        logger.info("Worker {worker} claimed investigation {id}", worker=self._worker, id=job_id)
        request = InvestigationRequest(**job.request) if job.request else None
        principal = Principal.from_dict(job.principal)

        # Back into the owning tenant before anything runs. The runner creates
        # a task, which copies this context — so every read and write the
        # investigation makes is scoped to the tenant that submitted it, on a
        # worker that never saw their request.
        with tenant_scope(principal.tenant if principal else DEFAULT_TENANT):
            self._runner.start(
                job.id,
                request,
                principal,
                already_running=True,
                # The identity this claim was made under, so the watchdog renews
                # the lease it actually holds. See `InvestigationJobRunner._watch`.
                lease_worker=self._worker,
            )

    async def _settle_unclaimable(self, job_id: str) -> None:
        with system_scope():
            job = await asyncio.to_thread(self._store.get_summary, job_id)
        if job is None or job.status.terminal:
            return
        if job.cancel_requested:
            # Cancelled while queued: nothing ever ran, but the record still
            # has to reach a terminal state.
            with system_scope():
                await asyncio.to_thread(self._store.mark_cancelled, job_id)

    # --- control ------------------------------------------------------------

    async def _consume_control(self) -> None:
        async for message in self._bus.watch_control():
            if message.get("op") != "cancel":
                continue
            job_id = message.get("id")
            if not job_id:
                continue
            # Fires the runner's listener. If the job belongs to another
            # worker this is a no-op here and takes effect there.
            self._store.notify_cancel(job_id)

    def _sample_queues(self) -> None:
        """Publish queue depth by role. Never fails the reaper."""
        try:
            metrics.queue("shared", self._bus.queue_depth())
            metrics.queue("worker", self._bus.queue_depth(self._worker))
        except Exception as exc:  # pragma: no cover - a gauge must not kill the loop
            logger.debug("Could not sample queue depth: {error}", error=exc)

    # --- reaper -------------------------------------------------------------

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            # The reaper cannot know a tenant either: it is looking for jobs
            # whose worker died, across everyone.
            with system_scope():
                probe_started = time.monotonic()
                try:
                    await asyncio.to_thread(self._store.requeue_unclaimed, UNCLAIMED_GRACE_SECONDS)
                except Exception as exc:
                    self._store_answering_since = None
                    logger.warning("Reaper could not reach the job store: {error}", error=exc)
                    continue
                answered = time.monotonic()
                # A call that hung and then succeeded is an outage too. The
                # first version recorded only calls that *raised*, and on the
                # worker whose query was already in flight on an open
                # connection when Postgres paused, nothing raised — the query
                # waited ninety-five seconds, returned, and that worker's
                # reaper failed the live job on the same tick. Longer than a
                # renewal interval is long enough for a renewal to have been
                # blocked, so it restarts the wait.
                if (
                    self._store_answering_since is None
                    or answered - probe_started > settings.job_lease_seconds / 3
                ):
                    self._store_answering_since = answered

                # **An expired lease proves a dead worker only if the worker
                # could have renewed it.** With Postgres paused for 94 seconds
                # no worker could write a renewal, every running lease expired,
                # and the first reaper tick after the store came back failed a
                # live investigation "Investigation worker stopped before the
                # run finished" — which then completed on its still-running
                # worker, leaving a job `succeeded` with that error, and a
                # console reading Failed for good. Found by pausing the
                # container mid-investigation, not by a test: every reaping
                # test kills the worker, where the two cases look the same.
                #
                # So nothing is reaped until the store has answered this reaper
                # for a whole lease. A live worker renews every third of a
                # lease, so by then its lease is back in force; a dead one's is
                # reaped one lease later than before, which is the whole cost.
                if time.monotonic() - self._store_answering_since >= settings.job_lease_seconds:
                    await asyncio.to_thread(self._store.reap_expired, WORKER_LOST)
            # Sampled on the reaper's tick rather than polled on its own timer:
            # queue depth is the envelope's alarm signal, and this loop already
            # runs at a cadence an operator would want to alarm at.
            await asyncio.to_thread(self._sample_queues)
