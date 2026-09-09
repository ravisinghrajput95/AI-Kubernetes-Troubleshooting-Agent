"""Progress reporting must reach the stream without stopping the worker.

Two properties, and the second is the one that had never held.

Every existing progress test calls `store.publish()` directly, so the *bridge*
— a collector finishing, becoming an event on the job's stream — was covered at
neither end. `TestProgressReachesTheStream` closes that.

`TestTheEventLoopKeepsRunning` is the regression guard for the defect. On the
distributed store a progress event is a committed Postgres row plus a Redis
publish, and every call site is a coroutine on the event loop, so reporting it
inline stopped the worker advancing *any* task — HTTP, SSE, and every attached
agent's gRPC stream — for two network round trips, around fifty times per
investigation. Measured against real Postgres and Redis before the fix: the
loop was blocked 94% of wall clock, `publish` was 89.6% of that, and event-loop
lag ran to a p50 of 75 ms.

It asserts on **lag observed by a task that was trying to run**, not on where
the call was made from. A test that checked the calling thread would pass for a
store method that blocks the loop some other way, and would have to name each
method — which is the second table this codebase keeps refusing to write.
"""

import asyncio
import time

import pytest

from app.jobs.models import JobEventType
from app.jobs.runner import InvestigationJobRunner
from app.jobs.store import InMemoryJobStore
from app.tenancy import current_tenant, tenant_scope

# One simulated round trip. Long enough that blocking is unmissable against the
# heartbeat below, short enough that the whole test stays well under a second.
PUBLISH_SECONDS = 0.02
PUBLISHES = 12


class SlowStore(InMemoryJobStore):
    """A store whose publish costs what a real one costs.

    `InMemoryJobStore` writes to a dict, so with it every arrangement here
    passes — the defect is only observable when publishing is I/O, which is
    exactly the deployment the in-memory store is not.
    """

    def __init__(self) -> None:
        super().__init__()
        self.published: list[str] = []

    def publish(self, job_id: str, event) -> None:
        time.sleep(PUBLISH_SECONDS)
        if event.type is JobEventType.PROGRESS:
            self.published.append(event.message)
        return super().publish(job_id, event)


@pytest.fixture
def reporting_investigation(monkeypatch):
    """Replace the investigation with one that does nothing but report."""

    async def only_reports(request, reporter=None, investigation_id=None, principal=None):
        for index in range(PUBLISHES):
            await reporter.report(f"step {index}", step=index)
        return {"investigation": {"health": {"status": "ok"}}, "diagnosis": {}}

    monkeypatch.setattr("app.jobs.runner.run_investigation", only_reports)
    monkeypatch.setattr("app.jobs.runner.collection_failure", lambda investigation: None)


class TestProgressReachesTheStream:
    async def test_a_collectors_progress_becomes_an_event_on_the_job(self, reporting_investigation):
        """The bridge itself: every report published, in order, with its data."""
        store = SlowStore()
        runner = InvestigationJobRunner(store)

        job = await runner.submit(None)
        await _settle(runner)

        assert store.published == [f"step {index}" for index in range(PUBLISHES)]
        events = [
            event for event in store.get(job.id).events if event.type is JobEventType.PROGRESS
        ]
        assert [event.message for event in events] == store.published
        assert events[3].data == {"step": 3}

    async def test_the_order_a_caller_reported_in_is_the_order_it_is_stored_in(
        self, reporting_investigation
    ):
        """Awaiting is what preserves this.

        The publish goes to a worker thread; the caller waits for it. Dispatch
        without awaiting would be faster and would let two events land out of
        order — and the sequence is the SSE frame id that `Last-Event-ID`
        resumes from, so out-of-order events are a stream that cannot resume.
        """
        store = SlowStore()
        runner = InvestigationJobRunner(store)

        job = await runner.submit(None)
        await _settle(runner)

        events = [
            event for event in store.get(job.id).events if event.type is JobEventType.PROGRESS
        ]
        assert [event.seq for event in events] == sorted(event.seq for event in events)


class TestTheEventLoopKeepsRunning:
    async def test_reporting_progress_does_not_stall_every_other_task(
        self, reporting_investigation
    ):
        """The worker must keep serving while an investigation reports.

        The heartbeat stands in for everything else a worker owes its callers:
        an HTTP request, an SSE frame, an agent's stream. It asks for a 5 ms
        sleep; anything beyond that is time the loop could not advance it.
        """
        store = SlowStore()
        runner = InvestigationJobRunner(store)
        lags: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                asked = 0.005
                started = time.perf_counter()
                await asyncio.sleep(asked)
                lags.append(time.perf_counter() - started - asked)

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)  # the heartbeat has to be running to observe anything
        await runner.submit(None)
        await _settle(runner)
        stop.set()
        await beat

        blocked = sum(lag for lag in lags if lag > 0)
        spent_publishing = PUBLISHES * PUBLISH_SECONDS

        # The control: this run has to have done the work whose cost is being
        # measured. Without it a run that reported nothing passes perfectly.
        assert len(store.published) == PUBLISHES

        # Measured both ways on a development machine. With the publish inline
        # the heartbeat ran **4 times** for the whole investigation and waited
        # 305 ms to be scheduled once; dispatched, it ran 54 times and was
        # blocked for 57 ms in total against 240 ms of publishing. Both
        # thresholds sit halfway between those, which is a margin of at least
        # 2x on each side rather than a line drawn against one of them.
        expected_ticks = spent_publishing / 0.005
        assert len(lags) > expected_ticks / 4, (
            f"the heartbeat ran only {len(lags)} times while {PUBLISHES} progress "
            f"events were published — a task that cannot be scheduled is a worker "
            f"that has stopped serving HTTP, SSE and its agent streams."
        )
        assert blocked < spent_publishing / 2, (
            f"the event loop was blocked for {blocked * 1000:.0f} ms while "
            f"publishing {PUBLISHES} progress events costing "
            f"{spent_publishing * 1000:.0f} ms — progress reporting is back on "
            f"the loop, so this worker stops serving HTTP, SSE and its agent "
            f"streams for the duration of every investigation's progress."
        )


async def _settle(runner: InvestigationJobRunner) -> None:
    for _ in range(400):
        await asyncio.sleep(0.01)
        if runner.busy == 0:
            return
    raise AssertionError("the investigation never finished")


class TestTheTenantSurvivesTheDispatch:
    """`submit` writes from a worker thread now, and the tenant is ambient.

    A worker thread gets a *copy* of the context, which is the mechanism that
    made every request run as tenant `default` from M6 until M6.5. Reading a
    copy is the safe direction where writing to one is not — but "safe in
    principle" is exactly what `require_principal` was, so this asserts on the
    tenant observed **inside the dispatched call**, which is the value
    `Database.cursor()` would hand to `set_config`. Asserting it either side of
    the dispatch passes with the context lost in the middle.
    """

    async def test_the_tenant_reaches_the_dispatched_write(self):
        seen: list[str] = []

        class TenantRecordingStore(InMemoryJobStore):
            def create(self, *args, **kwargs):
                seen.append(current_tenant())
                return super().create(*args, **kwargs)

        runner = InvestigationJobRunner(TenantRecordingStore())
        with tenant_scope("acme"):
            await runner.submit(None)
        await runner.shutdown()

        assert seen == ["acme"], (
            f"the row would have been written for tenant {seen!r} rather than "
            f"'acme' — the ambient tenant did not survive the thread dispatch, "
            f"which is one tenant's investigations landing in another's."
        )
