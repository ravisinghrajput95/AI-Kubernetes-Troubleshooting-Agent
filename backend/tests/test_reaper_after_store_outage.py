"""An expired lease proves a dead worker only if the worker could have renewed it.

Found by `docker pause` on Postgres for 94 seconds while an investigation ran
on a two-worker deployment. No worker could write a renewal, so every running
lease expired; the first reaper tick after the store came back failed the job
"Investigation worker stopped before the run finished". The worker was alive,
finished a moment later, and saved a diagnosis onto a job the console already
showed as Failed — and whose record now read `succeeded` with that error.

Every reaping test kills the worker, and a killed worker and an unreachable
store expire a lease identically. What differs is whether the store was
reachable to be renewed into, which only the reaper can observe.
"""

import asyncio
import time

import pytest

from app.core.config import settings
from app.jobs import consumer as consumer_module
from app.jobs.consumer import JobConsumer


class FlakyStore:
    """Answers or not, and records every reap."""

    def __init__(self) -> None:
        self.reachable = True
        self.hang_seconds = 0.0
        self.reaps: list[float] = []

    def requeue_unclaimed(self, older_than_seconds: int) -> list[str]:
        if not self.reachable:
            raise ConnectionError("store unreachable")
        if self.hang_seconds:
            # A query already in flight when the store paused: no error, just
            # an answer that arrives when the store comes back.
            time.sleep(self.hang_seconds)
            self.hang_seconds = 0.0
        return []

    def reap_expired(self, error: str) -> list[str]:
        if not self.reachable:
            raise ConnectionError("store unreachable")
        self.reaps.append(time.monotonic())
        return []

    def queue_depths(self):  # sampled on the same tick
        return {}


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(consumer_module, "REAPER_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(settings, "job_lease_seconds", 0.3)


async def run_reaper(consumer: JobConsumer, seconds: float) -> None:
    task = asyncio.create_task(consumer._reap())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def reaper(store) -> JobConsumer:
    built = JobConsumer.__new__(JobConsumer)
    built._store = store
    built._store_answering_since = None
    built._sample_queues = lambda: None
    return built


async def test_nothing_is_reaped_until_the_store_has_answered_for_a_lease(fast):
    store = FlakyStore()
    consumer = reaper(store)

    store.reachable = False
    await run_reaper(consumer, 0.1)
    assert store.reaps == []

    store.reachable = True
    recovered = time.monotonic()
    await run_reaper(consumer, 0.2)
    # Recovered 0.2s ago against a 0.3s lease: a live worker may not have
    # renewed yet, so the expired leases prove nothing.
    assert store.reaps == []

    # The control: once the store has answered for a whole lease, reaping
    # resumes — a gate that never reaps would pass everything above.
    await run_reaper(consumer, 0.3)
    assert store.reaps, "reaping never resumed after the store recovered"
    assert store.reaps[0] - recovered >= settings.job_lease_seconds


async def test_a_worker_that_starts_reaps_once_it_has_seen_the_store_for_a_lease(fast):
    store = FlakyStore()
    consumer = reaper(store)
    await run_reaper(consumer, 0.5)
    assert store.reaps


async def test_a_query_that_hung_and_then_answered_is_an_outage_too(fast):
    """The live case that got past the first version: the reaper's query was
    in flight on an open connection when Postgres paused, so nothing raised.
    It waited out the pause, returned, and the same tick reaped a live job."""
    store = FlakyStore()
    consumer = reaper(store)
    await run_reaper(consumer, 0.45)  # healthy for longer than a lease
    reaps_before = len(store.reaps)

    store.hang_seconds = 0.25  # longer than a renewal interval (0.1s)
    await run_reaper(consumer, 0.35)
    assert len(store.reaps) == reaps_before, "reaped straight after a stall"
