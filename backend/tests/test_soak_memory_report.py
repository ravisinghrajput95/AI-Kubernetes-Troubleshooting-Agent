"""The soak's memory report, held against the run that broke it.

`scripts/soak_bench.py` publishes resident memory as start / peak / end and a
trend fitted over the second half, and `docs/PERFORMANCE_ENVELOPE.md` quotes
those trends as evidence of no leak. A 60-minute run on 2026-09-08 produced two
numbers that cannot both describe the same process:

    worker-2   start 118.5 MB   end 77.2 MB   trend(2nd half) +8.4 MB/h

It ended 41 MB *below* where it started and was credited with growth. Both
workers had stepped down together at minute 15 — worker-2 as low as 35 MB
against a 124 MB peak — wandered for eight minutes and settled on a new
baseline, and the slope was being fitted to the recovery.

Two independent processes do not release memory together for a reason of their
own, so a simultaneous fall is the host reclaiming pages. `ps rss` is not the
noisy part: sampled every two seconds for a minute under the same workload it
did not move by a single kilobyte.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from soak_bench import host_disturbances  # noqa: E402

WORKERS = ["worker-1", "worker-2"]


@dataclass
class Sample:
    at: float
    rss: dict = field(default_factory=dict)


def series(*columns):
    """Build samples from one column of readings per worker."""
    return [
        Sample(at=float(i * 10), rss=dict(zip(WORKERS, row, strict=True)))
        for i, row in enumerate(zip(*columns, strict=True))
    ]


class TestASimultaneousFallIsTheHost:
    def test_the_run_that_broke_the_report_is_detected(self):
        """The real shape: a level stretch, a step down together, a new baseline."""
        one = [121, 130, 133, 135, 135, 91, 83, 75, 42, 118, 120, 122, 125, 127]
        two = [119, 123, 123, 124, 124, 75, 69, 68, 40, 70, 71, 72, 74, 77]

        hits = host_disturbances(series(one, two), WORKERS)

        assert hits, "the step down at index 5 is the whole reason this exists"
        assert 5 in hits

    def test_one_worker_falling_alone_is_not_the_host(self):
        """The control, and the one that matters.

        A single process releasing memory is exactly what a bounded cache
        evicting looks like, and calling that a host event would discard a real
        measurement — the over-strict direction.
        """
        one = [121, 130, 133, 135, 60, 121, 125]
        two = [119, 123, 123, 124, 124, 124, 125]

        assert host_disturbances(series(one, two), WORKERS) == []

    def test_a_steady_run_reports_nothing(self):
        one = [121, 122, 123, 124, 125, 126, 127]
        two = [119, 119, 120, 120, 121, 121, 122]

        assert host_disturbances(series(one, two), WORKERS) == []

    def test_a_gentle_decline_together_is_not_a_disturbance(self):
        """Only a *sharp* simultaneous fall qualifies; drift is measurement."""
        one = [120, 118, 116, 114, 112]
        two = [120, 118, 116, 114, 112]

        assert host_disturbances(series(one, two), WORKERS) == []

    def test_a_worker_with_no_reading_does_not_manufacture_one(self):
        """A probe that failed is absent, not zero — `process_memory` returns 0.0."""
        samples = series([121, 130, 40], [119, 123, 0])

        assert host_disturbances(samples, WORKERS) == []
