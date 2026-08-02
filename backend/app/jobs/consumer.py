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

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.jobs.distributed import PostgresRedisJobStore
from app.jobs.runner import WORKER_LOST, InvestigationJobRunner
from app.models.investigation import InvestigationRequest
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
        max_concurrent: int = 4,
    ) -> None:
        self._store = store
        self._runner = runner
        self._bus = bus
        self._worker = worker_id
        self._max_concurrent = max_concurrent
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._forever("queue", self._consume_queue)),
            asyncio.create_task(self._forever("control", self._consume_control)),
            asyncio.create_task(self._forever("reaper", self._reap)),
        ]
        logger.info("Job consumer started as worker {worker}", worker=self._worker)

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

    # --- reaper -------------------------------------------------------------

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            # The reaper cannot know a tenant either: it is looking for jobs
            # whose worker died, across everyone.
            with system_scope():
                await asyncio.to_thread(self._store.reap_expired, WORKER_LOST)
                await asyncio.to_thread(self._store.requeue_unclaimed, UNCLAIMED_GRACE_SECONDS)
