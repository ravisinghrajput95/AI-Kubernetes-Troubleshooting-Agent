"""A refusal resting on a Redis-only fact must not fail a job at once.

Presence records are rewritten on each agent heartbeat. `FLUSHDB` on the
platform's Redis during a batch of investigations deleted all of them, and the
next batch — inside the fifteen seconds before the holder re-announced — had
one investigation of `api-cut` claimed by the worker *not* holding its stream,
which failed it for good with "that agent is not connected to any worker right
now". The agent was connected and healthy on the other worker the whole time.
Losing Redis is meant to make the platform slower, never wrong.

These drive the runner, not `select_provider`: the defect was never in the
refusal, which is correct on what it can see, but in failing the job on it.
"""

import asyncio

import pytest

from app.jobs.models import JobStatus
from app.jobs.runner import InvestigationJobRunner
from app.jobs.store import InMemoryJobStore
from app.providers.base import AgentAway, AgentElsewhere

AWAY = "Cluster 'api-cut' is reached through its agent, and that agent is not connected"
RESULT = {"investigation": {}, "diagnosis": {}}


class HandingStore(InMemoryJobStore):
    def __init__(self, accepts: bool = True) -> None:
        super().__init__()
        self.accepts = accepts
        self.hand_offs: list[tuple[str, str, str]] = []

    def hand_off(self, job_id: str, worker: str, to_worker: str) -> bool:
        self.hand_offs.append((job_id, worker, to_worker))
        return self.accepts

    def renew_lease(self, job_id: str, worker: str, lease_seconds: int) -> None:
        pass

    def is_cancel_requested(self, job_id: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr("app.jobs.runner.AGENT_RECONNECT_GRACE_SECONDS", 0.4)
    monkeypatch.setattr("app.jobs.runner.AGENT_RECONNECT_POLL_SECONDS", 0.05)
    monkeypatch.setattr("app.jobs.runner.announce", lambda *args, **kwargs: None)


def investigation_raising(monkeypatch, *failures):
    """run_investigation that raises each of `failures` in turn, then succeeds."""
    calls = []

    async def fake(*args, **kwargs):
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return RESULT

    monkeypatch.setattr("app.jobs.runner.run_investigation", fake)
    return calls


async def run(store, lease_worker="worker-b"):
    runner = InvestigationJobRunner(store)
    job = store.create({"context": "api-cut"})
    await runner.start(job.id, None, already_running=True, lease_worker=lease_worker)
    return store.get(job.id)


class TestAnAgentNobodyCanSeeIsWaitedFor:
    async def test_it_succeeds_once_the_agent_is_seen_again(self, monkeypatch):
        calls = investigation_raising(monkeypatch, AgentAway(AWAY), AgentAway(AWAY))

        job = await run(HandingStore())

        assert job.status is JobStatus.SUCCEEDED, job.error
        assert len(calls) == 3

    async def test_the_wait_is_said_once_on_the_timeline(self, monkeypatch):
        investigation_raising(monkeypatch, AgentAway(AWAY), AgentAway(AWAY), AgentAway(AWAY))

        job = await run(HandingStore())

        waiting = [event for event in job.events if "Waiting up to" in event.message]
        assert len(waiting) == 1

    async def test_an_agent_that_stays_away_is_still_refused_with_its_reason(self, monkeypatch):
        investigation_raising(monkeypatch, *[AgentAway(AWAY)] * 100)

        started = asyncio.get_running_loop().time()
        job = await run(HandingStore())

        assert job.status is JobStatus.FAILED
        assert job.error.startswith(AWAY)
        assert asyncio.get_running_loop().time() - started >= 0.4


class TestAnAgentOnAnotherWorkerIsHandedThere:
    async def test_a_claimed_job_goes_to_the_worker_holding_the_stream(self, monkeypatch):
        investigation_raising(
            monkeypatch, AgentElsewhere("attached to worker-a", holder="worker-a")
        )
        store = HandingStore()

        job = await run(store)

        assert store.hand_offs == [(job.id, "worker-b", "worker-a")]
        assert job.status is not JobStatus.FAILED, job.error

    async def test_a_hand_off_that_did_not_happen_refuses_as_before(self, monkeypatch):
        investigation_raising(
            monkeypatch, AgentElsewhere("attached to worker-a", holder="worker-a")
        )

        job = await run(HandingStore(accepts=False))

        assert job.status is JobStatus.FAILED
        assert "attached to worker-a" in job.error

    async def test_a_job_with_no_lease_has_nothing_to_hand_off(self, monkeypatch):
        investigation_raising(
            monkeypatch, AgentElsewhere("attached to worker-a", holder="worker-a")
        )
        store = HandingStore()

        job = await run(store, lease_worker="")

        assert store.hand_offs == []
        assert job.status is JobStatus.FAILED
