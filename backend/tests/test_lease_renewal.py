"""A running investigation must keep the lease it was claimed under.

The claim records `worker_identity()` — `WORKER_ID` if set, otherwise
`hostname:pid`. The watchdog renewed with `settings.worker_id`, and `WORKER_ID`
is set by nothing in this repository: not the Helm chart, not compose. So the
renewal's `WHERE lease_worker = ''` matched no row, and every distributed
investigation that outlived `JOB_LEASE_SECONDS` was reaped as a dead worker
while the worker was alive and still running it.

Reproduced live against an agent frozen with SIGSTOP: each collector hit its
60-second budget as designed, and at 71 seconds the reaper failed the job with
"Investigation worker stopped before the run finished" — half a second before
the investigation completed and saved its report onto a job already marked
failed.

**The test goes through the claim, because the defect was the seam between two
correct halves.** `renew_lease` was tested with matching ids called directly;
the runner's tests stubbed it as a no-op. Neither could see the runner passing
an identity different from the one the claim wrote.
"""

import asyncio

import pytest

from app.core.config import settings
from app.jobs.consumer import JobConsumer
from app.jobs.models import InvestigationJob, JobStatus
from app.jobs.runner import InvestigationJobRunner
from app.jobs.store import InMemoryJobStore

CLAIMER = "worker-that-claimed:4242"


class LeaseRecordingStore(InMemoryJobStore):
    """Holds a lease the way the Postgres store does: renewable only by its holder."""

    distributed = True

    def __init__(self) -> None:
        super().__init__()
        self.lease_holder: dict[str, str] = {}
        self.renewals: list[str] = []
        self.refused_renewals: list[str] = []

    def claim(self, job_id: str, worker: str, lease_seconds: int) -> InvestigationJob | None:
        self.lease_holder[job_id] = worker
        self.mark_running(job_id)
        return self.get(job_id)

    def renew_lease(self, job_id: str, worker: str, lease_seconds: int) -> None:
        # The real store's `WHERE lease_worker = %s`: a mismatched identity
        # renews nothing, silently. Recording the refusal is what makes it
        # observable here.
        if self.lease_holder.get(job_id) == worker:
            self.renewals.append(worker)
        else:
            self.refused_renewals.append(worker)

    def is_cancel_requested(self, job_id: str) -> bool:
        return False


@pytest.fixture
def short_lease(monkeypatch):
    # A 0.3s lease renewed every 0.1s, so a one-second investigation outlives
    # it several times over — the shape of the frozen-agent run, compressed.
    monkeypatch.setattr(settings, "job_lease_seconds", 0.3)
    monkeypatch.setattr(settings, "job_cancel_poll_seconds", 0.1)
    # The trigger for the defect: nothing sets WORKER_ID.
    monkeypatch.setattr(settings, "worker_id", "")


@pytest.fixture
def slow_investigation(monkeypatch):
    async def takes_a_while(*args, **kwargs):
        await asyncio.sleep(1.0)
        return {"investigation": {"health": {"status": "ok"}}, "diagnosis": {}}

    monkeypatch.setattr("app.jobs.runner.run_investigation", takes_a_while)
    monkeypatch.setattr("app.jobs.runner.collection_failure", lambda investigation: None)


async def test_the_lease_is_renewed_under_the_identity_that_claimed_it(
    short_lease, slow_investigation
):
    store = LeaseRecordingStore()
    runner = InvestigationJobRunner(store)
    consumer = JobConsumer(store, runner, bus=None, worker_id=CLAIMER)

    job = store.create({}, owner="", principal=None)
    await consumer._claim_and_run(job.id)
    for _ in range(40):
        await asyncio.sleep(0.05)
        if runner.busy == 0:
            break

    # The control: the run outlived its lease, so renewal had to happen.
    assert store.get(job.id).status is JobStatus.SUCCEEDED
    assert store.renewals, (
        "no lease was renewed during a run that outlived its lease several times over"
    )
    assert not store.refused_renewals, (
        f"the watchdog renewed as {store.refused_renewals[0]!r} while the claim was made as "
        f"{CLAIMER!r} — no row matches, the lease lapses, and the reaper fails a live "
        f"investigation as a dead worker"
    )
    assert set(store.renewals) == {CLAIMER}
