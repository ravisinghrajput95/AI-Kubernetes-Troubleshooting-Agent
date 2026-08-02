"""One suite, both job stores.

The in-process store and the Postgres/Redis store are selected by
configuration, so the API and the runner cannot tell them apart — which is only
true for as long as they actually behave the same. These are the guarantees
both must provide.

The in-memory backend runs always. The distributed backend runs when
`K8S_AGENT_INTEGRATION=1`; see `tests/distributed_backend.py`. That means an
ordinary `python -m pytest` still needs no database, and a divergence between
the two is caught by the same assertions rather than by a second suite that
drifts.
"""

import asyncio

import pytest

from app.jobs.models import JobEvent, JobEventType, JobStatus
from app.jobs.store import InMemoryJobStore
from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, DistributedBackend


@pytest.fixture(params=["memory", "distributed"])
async def store(request):
    if request.param == "memory":
        yield InMemoryJobStore()
        return

    if not INTEGRATION_ENABLED:
        pytest.skip(SKIP_REASON)

    backend = DistributedBackend()
    try:
        yield backend.store()
    finally:
        await backend.close()


async def drain(store, job_id, **kwargs) -> list[JobEvent]:
    return [event async for event in store.subscribe(job_id, **kwargs) if event is not None]


class TestLifecycle:
    async def test_a_new_job_is_pending_with_a_queued_event(self, store):
        job = store.create({"context": "prod"}, owner="alice")

        assert job.status is JobStatus.PENDING
        assert job.owner == "alice"

        fetched = store.get(job.id)
        assert fetched is not None
        assert fetched.events[0].type is JobEventType.QUEUED

    async def test_transitions_record_timestamps_and_result(self, store):
        job = store.create({})

        store.mark_running(job.id)
        running = store.get(job.id)
        assert running.status is JobStatus.RUNNING
        assert running.started_at is not None

        store.mark_succeeded(job.id, {"diagnosis": {"root_cause": "x"}})
        done = store.get(job.id)
        assert done.status is JobStatus.SUCCEEDED
        assert done.finished_at is not None
        assert done.to_dict()["diagnosis"] == {"root_cause": "x"}

    async def test_failure_is_recorded_not_raised(self, store):
        job = store.create({})
        store.mark_failed(job.id, "cluster unreachable")

        failed = store.get(job.id)
        assert failed.status is JobStatus.FAILED
        assert failed.to_dict()["error"] == "cluster unreachable"

    async def test_a_failure_can_carry_what_the_run_did_produce(self, store):
        """A collection failure still has degraded evidence behind it."""
        job = store.create({})
        store.mark_failed(job.id, "nothing usable", {"investigation": {"evidence": []}})

        failed = store.get(job.id)
        assert failed.status is JobStatus.FAILED
        body = failed.to_dict()
        assert body["error"] == "nothing usable"
        assert body["investigation"] == {"evidence": []}

    async def test_a_later_failure_does_not_erase_an_earlier_result(self, store):
        """A worker lost after finishing must not blank what it recorded."""
        job = store.create({})
        store.mark_failed(job.id, "nothing usable", {"investigation": {"evidence": []}})
        store.mark_failed(job.id, "worker lost")

        assert store.get(job.id).to_dict()["investigation"] == {"evidence": []}

    async def test_cancellation_is_terminal(self, store):
        job = store.create({})
        store.mark_running(job.id)
        store.mark_cancelled(job.id)

        assert store.get(job.id).status is JobStatus.CANCELLED

    async def test_an_unknown_job_is_none_not_an_error(self, store):
        assert store.get("2f1c9a4e-0000-4000-8000-000000000000") is None

    async def test_the_result_can_be_excluded_from_serialisation(self, store):
        job = store.create({})
        store.mark_succeeded(job.id, {"investigation": {"big": "payload"}})
        fetched = store.get(job.id)

        assert "investigation" not in fetched.to_dict(include_result=False)
        assert "investigation" in fetched.to_dict(include_result=True)


class TestOwnership:
    async def test_listing_is_scoped_to_one_owner(self, store):
        mine = store.create({}, owner="alice")
        theirs = store.create({}, owner="bob")

        ids = {job.id for job in store.list(owner="alice")}
        assert mine.id in ids
        assert theirs.id not in ids

    async def test_listing_without_an_owner_returns_everything(self, store):
        store.create({}, owner="alice")
        store.create({}, owner="bob")

        assert len(store.list(owner=None)) >= 2

    async def test_the_principal_survives_for_a_worker_that_did_not_receive_it(self, store):
        """Impersonation must still name the original caller on another worker."""
        job = store.create(
            {},
            owner="alice",
            principal={"subject": "alice", "groups": ["platform"], "email": "a@example.com"},
        )
        assert store.get(job.id).principal["groups"] == ["platform"]


class TestSequencing:
    async def test_every_published_event_gets_an_increasing_sequence(self, store):
        job = store.create({})
        for index in range(3):
            store.publish(job.id, JobEvent(JobEventType.PROGRESS, f"step {index}"))

        sequences = [event.seq for event in store.get(job.id).events]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        assert all(seq > 0 for seq in sequences)


class TestSubscription:
    async def test_a_late_subscriber_gets_the_backlog_and_stops(self, store):
        job = store.create({})
        store.mark_running(job.id)
        store.mark_succeeded(job.id, {})

        received = await drain(store, job.id)
        assert [event.type for event in received][-1] is JobEventType.COMPLETED

    async def test_a_resume_position_skips_what_was_already_delivered(self, store):
        job = store.create({})
        store.mark_running(job.id)
        store.mark_succeeded(job.id, {})

        everything = await drain(store, job.id)
        resumed = await drain(store, job.id, after_seq=everything[0].seq)

        assert len(resumed) == len(everything) - 1
        assert everything[0].seq not in {event.seq for event in resumed}

    async def test_a_subscriber_receives_the_backlog_then_live_events(self, store):
        job = store.create({})
        store.mark_running(job.id)

        received: list[JobEvent] = []

        async def consume():
            async for event in store.subscribe(job.id, heartbeat=0.05):
                if event is not None:
                    received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)

        store.publish(job.id, JobEvent(JobEventType.PROGRESS, "Retrieved Pods"))
        store.mark_succeeded(job.id, {})
        await asyncio.wait_for(task, timeout=5)

        messages = [event.message for event in received]
        assert "Investigation queued" in messages
        assert "Investigation started" in messages
        assert "Retrieved Pods" in messages
        assert "Investigation complete" in messages

    async def test_no_event_is_delivered_twice(self, store):
        """The property the replay-then-live handoff exists to provide."""
        job = store.create({})
        store.mark_running(job.id)

        received: list[JobEvent] = []

        async def consume():
            async for event in store.subscribe(job.id, heartbeat=0.05):
                if event is not None:
                    received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)

        for index in range(5):
            store.publish(job.id, JobEvent(JobEventType.PROGRESS, f"step {index}"))
        store.mark_succeeded(job.id, {})
        await asyncio.wait_for(task, timeout=5)

        sequences = [event.seq for event in received]
        assert len(sequences) == len(set(sequences)), f"duplicate delivery: {sequences}"

    async def test_subscribing_to_an_unknown_job_yields_nothing(self, store):
        assert await drain(store, "2f1c9a4e-0000-4000-8000-000000000000") == []


class TestCancellationRequests:
    async def test_a_request_is_recorded_on_the_job(self, store):
        job = store.create({})
        store.mark_running(job.id)

        assert store.request_cancel(job.id) is True
        assert store.get(job.id).cancel_requested is True

    async def test_an_unknown_job_cannot_be_cancelled(self, store):
        assert store.request_cancel("2f1c9a4e-0000-4000-8000-000000000000") is False

    async def test_listeners_registered_before_the_request_are_told(self, store):
        """The in-process half of a distributed cancel.

        Whichever worker is running the job turns this callback into a
        `Task.cancel()`. Here it only has to fire.
        """
        seen: list[str] = []
        store.on_cancel(seen.append)

        job = store.create({})
        store.mark_running(job.id)
        store.request_cancel(job.id)

        if store.distributed:
            # The distributed store announces over Redis; the consumer's
            # control loop is what invokes listeners. Simulate that step.
            store.notify_cancel(job.id)

        assert seen == [job.id]

    async def test_a_broken_listener_does_not_block_the_cancel(self, store):
        def explode(job_id: str) -> None:
            raise RuntimeError("listener is broken")

        seen: list[str] = []
        store.on_cancel(explode)
        store.on_cancel(seen.append)

        job = store.create({})
        store.mark_running(job.id)
        store.request_cancel(job.id)
        if store.distributed:
            store.notify_cancel(job.id)

        assert seen == [job.id]
        assert store.get(job.id).cancel_requested is True


class TestASummaryReadCarriesNoPayload:
    """M8b: most reads of an investigation want a fact, not the investigation.

    Cancellation reads an owner and a status. The stream handler reads an
    owner. The consumer's settle path reads a boolean. Serving those from the
    full row moved 2.7 MB out of Postgres to answer them
    (`scripts/payload_bench.py`).

    Held to both stores so `result` cannot be present on one backend and absent
    on the other — a caller that read it would work single-process and return
    `None` distributed, which is the divergence this suite exists to catch.
    """

    async def test_a_summary_omits_the_result(self, store):
        job = store.create({"context": "prod"})
        store.mark_running(job.id)
        store.mark_succeeded(job.id, {"investigation": {"padding": "x" * 100_000}})

        assert store.get(job.id).result is not None, "the full read must still carry it"
        assert store.get_summary(job.id).result is None

    async def test_a_summary_carries_everything_a_caller_decides_on(self, store):
        job = store.create({"context": "prod"}, owner="alice")
        store.mark_running(job.id)
        store.request_cancel(job.id)

        summary = store.get_summary(job.id)

        # Exactly the fields the four internal call sites read.
        assert summary.owner == "alice"
        assert summary.status is JobStatus.RUNNING
        assert summary.cancel_requested is True
        assert summary.request == {"context": "prod"}

    async def test_a_summary_carries_the_timeline(self, store):
        """A status read is mostly the timeline; omitting it would make the
        cheap endpoint useless and send callers back to the expensive one."""
        job = store.create({})
        store.publish(job.id, JobEvent(JobEventType.PROGRESS, "Collecting evidence"))

        assert [event.message for event in store.get_summary(job.id).events] == [
            "Investigation queued",
            "Collecting evidence",
        ]

    async def test_an_unknown_id_summarises_as_nothing(self, store):
        assert store.get_summary("does-not-exist") is None

    async def test_a_summary_does_not_mutate_the_stored_job(self, store):
        """The in-memory store hands out its own objects; stripping the result
        in place would delete it for everyone."""
        job = store.create({})
        store.mark_running(job.id)
        store.mark_succeeded(job.id, {"investigation": {"kept": True}})

        store.get_summary(job.id)

        assert store.get(job.id).result == {"investigation": {"kept": True}}
