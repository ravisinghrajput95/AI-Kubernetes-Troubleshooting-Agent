"""The properties that only exist across processes.

`test_job_store_contract.py` proves the two stores behave alike. These are the
behaviours the in-process store cannot have at all, and which a single
`TestClient` would never exercise — every one of them involves two workers.

Opt-in: `K8S_AGENT_INTEGRATION=1` with Postgres and Redis running. See
`tests/distributed_backend.py`.
"""

import asyncio

import pytest

from app.jobs.consumer import JobConsumer
from app.jobs.models import JobEvent, JobEventType, JobStatus
from app.jobs.runner import WORKER_LOST, InvestigationJobRunner
from tests.distributed_backend import DistributedBackend, requires_backend

pytestmark = requires_backend

LEASE = 30


@pytest.fixture
async def backend():
    resource = DistributedBackend()
    try:
        yield resource
    finally:
        await resource.close()


@pytest.fixture
def worker_a(backend):
    """The worker that receives the HTTP request."""
    return backend.store()


@pytest.fixture
def worker_b(backend):
    """A different worker, sharing only Postgres and Redis."""
    return backend.store()


class TestJobsAreVisibleEverywhere:
    async def test_a_job_created_on_one_worker_is_found_on_another(self, worker_a, worker_b):
        job = worker_a.create({"context": "prod"}, owner="alice")

        seen = worker_b.get(job.id)
        assert seen is not None
        assert seen.request == {"context": "prod"}
        assert seen.owner == "alice"

    async def test_state_survives_the_process_that_created_it(self, backend, worker_a):
        """A restart loses the worker, not the investigation."""
        job = worker_a.create({})
        worker_a.mark_running(job.id)
        worker_a.publish(job.id, JobEvent(JobEventType.PROGRESS, "Retrieved Pods"))

        # A brand new store, as a restarted process would build.
        restarted = backend.store()
        recovered = restarted.get(job.id)

        assert recovered.status is JobStatus.RUNNING
        assert "Retrieved Pods" in [event.message for event in recovered.events]

    async def test_a_report_written_by_one_worker_is_served_by_another(self, backend):
        writer = backend.reports()
        reader = backend.reports()
        investigation_id = "3f2a1b4c-0000-4000-8000-00000000abcd"

        writer.ensure(investigation_id, "alice")
        writer.write(investigation_id, "pdf", b"%PDF-1.4 fake")

        assert reader.read(investigation_id, "pdf") == b"%PDF-1.4 fake"

    async def test_history_is_shared_and_owner_scoped(self, backend):
        writer = backend.reports()
        reader = backend.reports()
        writer.ensure("3f2a1b4c-0000-4000-8000-00000000ab01", "alice")
        writer.upsert_index({"id": "3f2a1b4c-0000-4000-8000-00000000ab01", "owner": "alice"})
        writer.ensure("3f2a1b4c-0000-4000-8000-00000000ab02", "bob")
        writer.upsert_index({"id": "3f2a1b4c-0000-4000-8000-00000000ab02", "owner": "bob"})

        ids = {item["id"] for item in reader.read_index(owner="alice")}
        assert ids == {"3f2a1b4c-0000-4000-8000-00000000ab01"}


class TestClaiming:
    async def test_only_one_worker_can_claim_a_job(self, worker_a, worker_b):
        """The conditional UPDATE is the mutual exclusion.

        Both workers can pop the same id off the queue; only one may run it.
        """
        job = worker_a.create({})

        first = worker_a.claim(job.id, "worker-a", LEASE)
        second = worker_b.claim(job.id, "worker-b", LEASE)

        assert first is not None
        assert second is None
        assert worker_b.get(job.id).status is JobStatus.RUNNING

    async def test_a_job_cancelled_before_it_starts_is_never_claimed(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.request_cancel(job.id)

        assert worker_b.claim(job.id, "worker-b", LEASE) is None
        assert worker_b.get(job.id).status is JobStatus.PENDING

    async def test_the_consumer_settles_a_job_cancelled_while_queued(self, backend, worker_b):
        """Nothing ran, but the record still has to reach a terminal state."""
        store = backend.store()
        job = store.create({})
        store.request_cancel(job.id)

        consumer = JobConsumer(
            worker_b, InvestigationJobRunner(worker_b), backend.bus, worker_id="worker-b"
        )
        await consumer._claim_and_run(job.id)

        assert worker_b.get(job.id).status is JobStatus.CANCELLED


class TestIdleConsumer:
    """An idle worker is the normal state, and must survive it quietly.

    Found by running two workers rather than by a test: a blocking read that
    expires with no work raises, and letting that escape crashed and restarted
    the queue loop every few seconds. It never showed up in the suite because
    no test left a consumer idle for longer than one timeout.
    """

    async def test_a_full_length_idle_block_returns_nothing_rather_than_raising(self, backend):
        """Deliberately the real block length, not a fast one.

        The bug only appears when the blocking read is long enough to reach the
        client's own socket deadline, so a short timeout here would pass
        against the broken version and prove nothing. Worth the seconds.
        """
        assert await backend.bus.dequeue() is None

    async def test_the_queue_loop_still_works_after_several_idle_cycles(
        self, backend, worker_a, worker_b, monkeypatch
    ):
        async def brief_dequeue(timeout: float = 5.0, worker_id: str = ""):
            # `worker_id` is forwarded, not dropped: since M8a the consumer
            # reads its own queue before the shared one, and a double that
            # ignored it would quietly test the wrong loop.
            return await type(backend.bus).dequeue(backend.bus, timeout=0.05, worker_id=worker_id)

        monkeypatch.setattr(backend.bus, "dequeue", brief_dequeue)

        async def forever(*args, **kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr("app.jobs.runner.run_investigation", forever)

        runner_b = InvestigationJobRunner(worker_b)
        consumer = JobConsumer(worker_b, runner_b, backend.bus, worker_id="worker-b")
        consumer.start()
        try:
            # Several times longer than the blocking read, so the loop has
            # expired and gone round again repeatedly before any work arrives.
            await asyncio.sleep(0.6)

            job = worker_a.create({})
            worker_a.enqueue(job.id)

            for _ in range(60):
                await asyncio.sleep(0.05)
                if worker_a.get(job.id).status is JobStatus.RUNNING:
                    break

            assert worker_a.get(job.id).status is JobStatus.RUNNING
        finally:
            await runner_b.shutdown()
            await consumer.stop()


class TestLeaseReaping:
    async def test_a_job_whose_worker_died_is_failed_not_left_running(self, worker_a, worker_b):
        job = worker_a.create({})
        # A lease that has already expired is what a dead worker leaves behind.
        worker_a.claim(job.id, "worker-a", -1)

        reaped = worker_b.reap_expired(WORKER_LOST)

        assert job.id in reaped
        recovered = worker_b.get(job.id)
        assert recovered.status is JobStatus.FAILED
        assert recovered.error == WORKER_LOST

    async def test_a_live_lease_is_left_alone(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.claim(job.id, "worker-a", LEASE)

        assert job.id not in worker_b.reap_expired(WORKER_LOST)
        assert worker_b.get(job.id).status is JobStatus.RUNNING

    async def test_renewing_a_lease_keeps_a_slow_investigation_alive(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.claim(job.id, "worker-a", -1)
        worker_a.renew_lease(job.id, "worker-a", LEASE)

        assert job.id not in worker_b.reap_expired(WORKER_LOST)

    async def test_another_workers_lease_cannot_be_renewed(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.claim(job.id, "worker-a", -1)
        worker_b.renew_lease(job.id, "worker-b", LEASE)

        assert job.id in worker_b.reap_expired(WORKER_LOST)

    async def test_a_queue_message_that_was_lost_is_re_offered(self, worker_a):
        job = worker_a.create({})
        assert job.id in worker_a.requeue_unclaimed(older_than_seconds=0)


class TestProgressAcrossWorkers:
    async def test_a_subscriber_on_one_worker_sees_another_workers_events(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.mark_running(job.id)

        received: list[JobEvent] = []

        async def consume():
            async for event in worker_b.subscribe(job.id, heartbeat=0.05):
                if event is not None:
                    received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)

        worker_a.publish(job.id, JobEvent(JobEventType.PROGRESS, "Retrieved Pods"))
        worker_a.mark_succeeded(job.id, {})
        await asyncio.wait_for(task, timeout=10)

        assert "Retrieved Pods" in [event.message for event in received]
        assert received[-1].type is JobEventType.COMPLETED

    async def test_an_event_published_during_the_backlog_read_is_not_lost(
        self, backend, worker_a, worker_b
    ):
        """The interleaving that decides whether the handoff is correct.

        `subscribe()` opens the Redis subscription before it reads the backlog.
        Slowing the read down and publishing inside that window reproduces the
        exact race; the event must arrive exactly once.
        """
        job = worker_a.create({})
        worker_a.mark_running(job.id)

        original = worker_b.events_since
        published = asyncio.Event()

        def slow_read(job_id, after_seq=0):
            rows = original(job_id, after_seq)
            if not published.is_set():
                # Publish from the *other* worker while this read is in flight.
                worker_a.publish(job_id, JobEvent(JobEventType.PROGRESS, "mid-read"))
                published.set()
            return rows

        worker_b.events_since = slow_read

        received: list[JobEvent] = []

        async def consume():
            async for event in worker_b.subscribe(job.id, heartbeat=0.05):
                if event is not None:
                    received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.5)
        worker_a.mark_succeeded(job.id, {})
        await asyncio.wait_for(task, timeout=10)

        messages = [event.message for event in received]
        assert messages.count("mid-read") == 1, f"lost or duplicated: {messages}"

    async def test_no_event_is_delivered_twice_across_workers(self, worker_a, worker_b):
        job = worker_a.create({})
        worker_a.mark_running(job.id)

        received: list[JobEvent] = []

        async def consume():
            async for event in worker_b.subscribe(job.id, heartbeat=0.05):
                if event is not None:
                    received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)

        for index in range(10):
            worker_a.publish(job.id, JobEvent(JobEventType.PROGRESS, f"step {index}"))
        worker_a.mark_succeeded(job.id, {})
        await asyncio.wait_for(task, timeout=10)

        sequences = [event.seq for event in received]
        assert len(sequences) == len(set(sequences)), f"duplicates: {sequences}"
        assert sequences == sorted(sequences)


class TestDistributedCancellation:
    """Cancelling a job that belongs to another process.

    `Task.cancel()` only works inside the process that owns the task, so the
    decision travels as a message and the owning worker turns it back into a
    cancel. Both halves are checked here: the fast path over Redis, and the
    watchdog that makes it a guarantee when the message never arrives.
    """

    @staticmethod
    def _never_finishes(monkeypatch):
        async def forever(*args, **kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr("app.jobs.runner.run_investigation", forever)

    async def test_a_cancel_on_one_worker_stops_a_job_running_on_another(
        self, backend, worker_a, worker_b, monkeypatch
    ):
        self._never_finishes(monkeypatch)

        runner_b = InvestigationJobRunner(worker_b)
        consumer_b = JobConsumer(worker_b, runner_b, backend.bus, worker_id="worker-b")
        consumer_b.start()
        try:
            job = worker_a.create({})
            claimed = worker_b.claim(job.id, "worker-b", LEASE)
            assert claimed is not None
            runner_b.start(job.id, None, already_running=True)
            await asyncio.sleep(0.2)

            # The cancel arrives at the worker that did *not* start the job.
            worker_a.request_cancel(job.id)

            for _ in range(100):
                await asyncio.sleep(0.05)
                if worker_a.get(job.id).status.terminal:
                    break

            assert worker_a.get(job.id).status is JobStatus.CANCELLED
        finally:
            await consumer_b.stop()

    async def test_the_message_alone_cancels_without_waiting_for_the_watchdog(
        self, backend, worker_a, worker_b, monkeypatch
    ):
        """The fast path, with the backstop deliberately out of reach.

        The watchdog is set to poll an hour from now, so anything that happens
        here happened because the control message arrived. Without this the two
        mechanisms are indistinguishable: a broken Redis control channel would
        degrade every cancel to a poll interval and no test would notice.
        """
        self._never_finishes(monkeypatch)
        monkeypatch.setattr("app.jobs.runner.settings.job_cancel_poll_seconds", 3600)

        runner_b = InvestigationJobRunner(worker_b)
        consumer_b = JobConsumer(worker_b, runner_b, backend.bus, worker_id="worker-b")
        consumer_b.start()
        try:
            job = worker_a.create({})
            assert worker_b.claim(job.id, "worker-b", LEASE) is not None
            runner_b.start(job.id, None, already_running=True)
            await asyncio.sleep(0.3)

            worker_a.request_cancel(job.id)

            for _ in range(40):
                await asyncio.sleep(0.05)
                if worker_a.get(job.id).status.terminal:
                    break

            assert worker_a.get(job.id).status is JobStatus.CANCELLED
        finally:
            await consumer_b.stop()

    async def test_the_watchdog_cancels_when_the_message_is_never_delivered(
        self, worker_a, worker_b, monkeypatch
    ):
        """The backstop, with the control loop deliberately absent.

        No consumer runs here, so nothing is listening on Redis — exactly what
        a dropped pub/sub connection looks like. The committed flag is what
        makes the cancel happen anyway.
        """
        self._never_finishes(monkeypatch)
        monkeypatch.setattr("app.jobs.runner.settings.job_cancel_poll_seconds", 0.25)

        runner_b = InvestigationJobRunner(worker_b)
        job = worker_a.create({})
        worker_b.claim(job.id, "worker-b", LEASE)
        runner_b.start(job.id, None, already_running=True)
        await asyncio.sleep(0.2)

        worker_a.request_cancel(job.id)

        for _ in range(100):
            await asyncio.sleep(0.05)
            if worker_a.get(job.id).status.terminal:
                break

        assert worker_a.get(job.id).status is JobStatus.CANCELLED


class TestMigrations:
    async def test_applying_twice_changes_nothing(self, backend):
        assert backend.database.migrate() == []

    async def test_concurrent_startup_does_not_race(self, backend):
        """Ten replicas booting at once must apply each migration exactly once.

        This has to start from an empty schema to mean anything: against an
        already-migrated database every worker short-circuits and the lock is
        never exercised. From scratch, unserialised workers all see no applied
        versions, all run the same SQL, and all try to record the same version
        row — which the advisory lock is what prevents.
        """
        backend.drop_schema()

        results = await asyncio.gather(
            *(asyncio.to_thread(backend.database.migrate) for _ in range(10)),
            return_exceptions=True,
        )

        failures = [item for item in results if isinstance(item, BaseException)]
        assert not failures, f"concurrent migration raised: {failures}"

        # Derived rather than listed: a new migration is a normal event, and a
        # test that has to be edited for one would eventually be edited without
        # being thought about.
        from app.persistence.migrator import discover_migrations

        expected = [version for version, _ in discover_migrations()]

        applied = [version for item in results for version in item]
        assert applied == expected, f"applied more than once: {applied}"


class TestAListingDoesNotReadResults:
    """M8b's first finding, and the cheapest fix in it.

    `result` holds the whole investigation and diagnosis — measured at 2.7 MB
    on a cluster at the `MAX_LIST_ITEMS` ceiling (`scripts/payload_bench.py`).
    The listing query selected it for every row and the API then discarded it
    in Python with `to_dict(include_result=False)`, so a 25-row dashboard load
    pulled 67.5 MB out of Postgres and returned none of it.

    Only observable against a real database: the in-memory store hands back the
    same objects it holds, so there is no wire for anything to be wasted on.
    That is exactly why this lives here and not in the store contract.
    """

    async def test_listed_jobs_carry_no_result(self, worker_a):
        job = worker_a.create({"context": "prod"})
        worker_a.mark_running(job.id)
        worker_a.mark_succeeded(job.id, {"investigation": {"padding": "x" * 200_000}})

        assert worker_a.get(job.id).result is not None, "the result must still be stored"

        listed = next(one for one in worker_a.list(limit=25) if one.id == job.id)
        assert listed.result is None, (
            "list() is selecting `result` again. Every caller discards it, so "
            "reading it only moves megabytes out of Postgres to be thrown away."
        )

    async def test_the_summary_query_still_carries_everything_else(self, worker_a):
        """Excluding a column must not quietly drop the fields a listing needs."""
        job = worker_a.create({"context": "prod"}, owner="alice")
        worker_a.mark_running(job.id)
        worker_a.mark_failed(job.id, "something broke")

        listed = next(one for one in worker_a.list(limit=25) if one.id == job.id)

        assert listed.owner == "alice"
        assert listed.request == {"context": "prod"}
        assert listed.status is JobStatus.FAILED
        assert listed.error == "something broke"
        assert listed.created_at is not None
        assert listed.started_at is not None
        assert listed.finished_at is not None

    def test_the_two_column_lists_stay_in_step(self):
        """A column added to one and not the other is a silent absence."""
        from app.jobs.distributed import _JOB_COLUMNS, _JOB_SUMMARY_COLUMNS

        full = {name.strip() for name in _JOB_COLUMNS.split(",")}
        summary = {name.strip() for name in _JOB_SUMMARY_COLUMNS.split(",")}

        assert full - summary == {"result"}, (
            f"the summary query differs from the full one by {full - summary}; "
            f"it should differ by `result` alone."
        )
