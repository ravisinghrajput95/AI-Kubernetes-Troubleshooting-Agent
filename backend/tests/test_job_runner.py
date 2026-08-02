"""Runner behaviour that does not need a database.

The runner is the piece that differs most between the two deployments: with the
in-process store it runs what it accepts, and with the distributed store it
queues the work and runs whatever it is handed. Both branches are checked here
with a stub store, so the dispatch decision is covered without Postgres.
"""

import asyncio

import pytest

from app.jobs.models import JobStatus
from app.jobs.runner import WORKER_LOST, InvestigationJobRunner
from app.jobs.store import InMemoryJobStore


class QueueingStore(InMemoryJobStore):
    """An in-memory store that claims to be distributed.

    Enough to exercise the runner's dispatch branch: a submitted job should be
    handed to the queue and *not* started locally.
    """

    distributed = True

    def __init__(self) -> None:
        super().__init__()
        self.queued: list[str] = []
        self.affinities: list[str] = []

    def enqueue(self, job_id: str, worker_id: str = "") -> None:
        # The affinity is recorded, not ignored: M8a routes agent-cluster work
        # to the worker holding the stream, and a double that dropped it would
        # let that regress silently.
        self.queued.append(job_id)
        self.affinities.append(worker_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.get(job_id)
        return bool(job and job.cancel_requested)

    def renew_lease(self, job_id: str, worker: str, lease_seconds: int) -> None:
        pass


@pytest.fixture
def never_finishes(monkeypatch):
    started = asyncio.Event()

    async def forever(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.jobs.runner.run_investigation", forever)
    return started


class TestDispatch:
    async def test_the_in_process_store_runs_the_job_where_it_was_submitted(self, never_finishes):
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)

        job = runner.submit(None)
        await asyncio.wait_for(never_finishes.wait(), timeout=2)

        assert runner.busy == 1
        assert store.get(job.id).status is JobStatus.RUNNING
        await runner.shutdown()

    async def test_the_distributed_store_queues_instead_of_running_locally(self):
        store = QueueingStore()
        runner = InvestigationJobRunner(store)

        job = runner.submit(None)
        await asyncio.sleep(0.05)

        assert store.queued == [job.id]
        assert runner.busy == 0, "a queued job must not also run on the submitting worker"
        assert store.get(job.id).status is JobStatus.PENDING


class TestCancellation:
    async def test_a_cancel_request_stops_a_locally_running_job(self, never_finishes):
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)

        job = runner.submit(None)
        await asyncio.wait_for(never_finishes.wait(), timeout=2)

        store.request_cancel(job.id)
        await asyncio.sleep(0.05)

        assert store.get(job.id).status is JobStatus.CANCELLED

    async def test_cancelling_a_job_owned_by_another_worker_is_not_an_error(self):
        """The message reaches every worker; only one of them owns the task."""
        store = QueueingStore()
        runner = InvestigationJobRunner(store)
        job = store.create({})

        assert runner.cancel(job.id) is False

    async def test_a_job_cancelled_before_it_starts_still_reaches_a_terminal_state(self):
        """Otherwise it sits pending forever, with nothing left to record it."""
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)

        job = store.create({})
        task = runner.start(job.id, None)
        task.cancel()  # before the coroutine has had a chance to run
        await asyncio.sleep(0.05)

        assert store.get(job.id).status is JobStatus.CANCELLED


class TestShutdown:
    async def test_in_flight_jobs_are_recorded_as_a_lost_worker_not_a_cancellation(
        self, never_finishes
    ):
        """A deploy is not a user cancellation, and must not be reported as one."""
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)

        job = runner.submit(None)
        await asyncio.wait_for(never_finishes.wait(), timeout=2)

        await runner.shutdown()

        finished = store.get(job.id)
        assert finished.status is JobStatus.FAILED
        assert finished.error == WORKER_LOST

    async def test_shutdown_leaves_no_running_tasks_behind(self, never_finishes):
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)

        runner.submit(None)
        runner.submit(None)
        await asyncio.wait_for(never_finishes.wait(), timeout=2)

        await runner.shutdown()
        assert runner.busy == 0
