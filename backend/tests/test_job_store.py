import asyncio

from app.jobs.models import JobEvent, JobEventType, JobStatus
from app.jobs.store import SUBSCRIBER_QUEUE_SIZE, InvestigationJobStore


def test_new_job_starts_pending_with_a_queued_event():
    store = InvestigationJobStore()
    job = store.create({"context": "prod"})

    assert job.status is JobStatus.PENDING
    assert job.events[0].type is JobEventType.QUEUED
    assert store.get(job.id) is job


def test_lifecycle_transitions_record_timestamps():
    store = InvestigationJobStore()
    job = store.create({})

    store.mark_running(job.id)
    assert job.status is JobStatus.RUNNING
    assert job.started_at is not None

    store.mark_succeeded(job.id, {"diagnosis": {"root_cause": "x"}})
    assert job.status is JobStatus.SUCCEEDED
    assert job.finished_at is not None
    assert job.to_dict()["diagnosis"] == {"root_cause": "x"}


def test_failure_is_recorded_not_raised():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_failed(job.id, "cluster unreachable")

    assert job.status is JobStatus.FAILED
    assert job.error == "cluster unreachable"
    assert job.to_dict()["error"] == "cluster unreachable"


def test_result_can_be_excluded_from_serialization():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_succeeded(job.id, {"investigation": {"big": "payload"}})

    assert "investigation" not in job.to_dict(include_result=False)
    assert "investigation" in job.to_dict(include_result=True)


async def test_subscriber_receives_backlog_then_live_events():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_running(job.id)

    received = []

    async def consume():
        async for event in store.subscribe(job.id):
            received.append(event.message)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    store.publish(job.id, JobEvent(JobEventType.PROGRESS, "Retrieved Pods"))
    store.mark_succeeded(job.id, {})
    await asyncio.wait_for(task, timeout=1)

    assert "Investigation queued" in received
    assert "Investigation started" in received
    assert "Retrieved Pods" in received
    assert "Investigation complete" in received


async def test_late_subscriber_to_finished_job_gets_backlog_and_stops():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_running(job.id)
    store.mark_succeeded(job.id, {})

    received = [event.message async for event in store.subscribe(job.id)]
    assert received[-1] == "Investigation complete"


async def test_slow_subscriber_never_blocks_the_producer():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_running(job.id)

    async def idle():
        async for _ in store.subscribe(job.id):
            await asyncio.sleep(10)

    task = asyncio.create_task(idle())
    await asyncio.sleep(0)

    # Far more events than the subscriber queue can hold.
    for index in range(SUBSCRIBER_QUEUE_SIZE * 2):
        store.publish(job.id, JobEvent(JobEventType.PROGRESS, f"step {index}"))

    assert len(job.events) == SUBSCRIBER_QUEUE_SIZE * 2 + 2
    task.cancel()


async def test_heartbeat_yields_none_while_idle():
    store = InvestigationJobStore()
    job = store.create({})
    store.mark_running(job.id)

    ticks = []

    async def consume():
        async for event in store.subscribe(job.id, heartbeat=0.01):
            ticks.append(event)
            if len(ticks) > 3:
                return

    await asyncio.wait_for(asyncio.create_task(consume()), timeout=1)
    assert None in ticks


def test_store_evicts_oldest_terminal_jobs():
    store = InvestigationJobStore(max_jobs=3)
    ids = []
    for _ in range(5):
        job = store.create({})
        store.mark_succeeded(job.id, {})
        ids.append(job.id)

    assert len(store.list(limit=100)) <= 3
    assert store.get(ids[-1]) is not None
    assert store.get(ids[0]) is None


def test_running_jobs_are_not_evicted():
    store = InvestigationJobStore(max_jobs=2)
    running = store.create({})
    store.mark_running(running.id)

    for _ in range(5):
        job = store.create({})
        store.mark_succeeded(job.id, {})

    assert store.get(running.id) is not None


def test_operations_on_unknown_job_are_silent():
    store = InvestigationJobStore()
    store.mark_running("missing")
    store.mark_succeeded("missing", {})
    store.publish("missing", JobEvent(JobEventType.PROGRESS, "noop"))
    assert store.get("missing") is None
