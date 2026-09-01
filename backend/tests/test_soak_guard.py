"""The soak harness's vacuity guard, held to the run that got past it.

`scripts/soak_bench.py` publishes memory trends, storage growth and renewal
counts from an hour of continuous operation. Every one of those numbers is only
worth reading if the platform was *working* for that hour, and a soak of a
platform doing nothing reports beautifully: flat memory, no errors, no leak.

The guard is therefore the only thing making the published numbers mean
anything, and it has already been too weak once. A 60-minute run in which
Docker Desktop killed the kind cluster four minutes in produced 81 usable
investigations out of 1,172 — a platform failing 93% of the time — cleared an
absolute floor of 60 and published its trends.

These tests exist because that guard is a harness, and a harness is the thing
in this repository most likely to be confidently wrong. Each asserts that one
check refuses a run the other two accept, and the last asserts the guard does
not refuse a healthy one — an over-strict guard is not a safe direction here,
it just means no soak ever publishes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SOAK = Path(__file__).resolve().parents[2] / "scripts" / "soak_bench.py"


def _load():
    spec = importlib.util.spec_from_file_location("soak_bench", SOAK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_bench"] = module
    spec.loader.exec_module(module)
    return module


soak = _load()

HOUR = 3600.0


def make_run(finished_at: float, *, usable: int = 0, error: str = ""):
    run = soak.Run(worker="worker-1", transport="sse", refresh=False)
    run.status = "succeeded" if usable else "failed"
    run.usable = usable
    run.error = error
    run.finished_at = finished_at
    run.seconds = 1.0
    return run


def make_state(runs, *, elapsed: float = HOUR, floor: int = 60, min_share: float = 0.80):
    return {
        "runs": runs,
        "samples": [],
        "started": 0.0,
        "elapsed": elapsed,
        "worker_names": [],
        "findings": {},
        "certificates": {"enabled": False},
        "floor": floor,
        "min_share": min_share,
        "retention": {},
    }


def healthy(n: int = 1200, elapsed: float = HOUR):
    """Investigations spread evenly across the whole run, all seeing the cluster."""
    return [make_run(i * (elapsed / n), usable=12) for i in range(n)]


def summarise(state, capsys) -> tuple[int, str]:
    code = soak.summarise(state)
    return code, capsys.readouterr().out


class TestTheGuardRefuses:
    def test_a_run_that_failed_most_of_the_time(self, capsys):
        """Run 3, as it actually happened: the share check is what catches it."""
        runs = [make_run(i * 3.0, usable=12) for i in range(81)]
        runs += [
            make_run(240.0 + i * 2.5, error="Unable to connect to the server: dial tcp ...")
            for i in range(1091)
        ]
        code, out = summarise(make_state(runs), capsys)
        assert code == 1
        assert "REFUSED" in out
        # 81 clears the floor of 60. Only the share and continuity checks can
        # see this run, which is the defect these two were added for.
        assert "below the floor" not in out
        assert "7% of investigations" in out

    def test_a_run_that_stopped_working_part_way(self, capsys):
        """Continuity alone. Enough runs, and 84% of them good — but the good
        ones all landed in the first ten minutes.

        This is the shape the share check cannot see, and it is the more
        dangerous of the two: offered load drops when a cluster dies slowly, so
        a run can keep a high success *rate* while measuring nothing after
        minute ten."""
        runs = [make_run(i * 0.5, usable=12) for i in range(1200)]
        runs += [make_run(600.0 + i, error="context deadline exceeded") for i in range(220)]
        state = make_state(runs)
        code, out = summarise(state, capsys)
        assert code == 1
        # The two cheaper checks are satisfied, so this is continuity or nothing.
        assert "below the floor" not in out
        assert "below the required" not in out
        assert "stopped working part way" in out
        assert "50.0 minutes without a usable investigation" in out

    def test_a_run_too_small_to_trend_from(self, capsys):
        """The original floor, still doing its own job: ten investigations at a
        100% success rate spread across the hour pass both new checks."""
        runs = [make_run(i * 360.0, usable=12) for i in range(10)]
        code, out = summarise(make_state(runs), capsys)
        assert code == 1
        assert "below the floor of 60" in out
        assert "below the required" not in out
        assert "stopped working part way" not in out

    def test_succeeding_without_seeing_the_cluster_is_not_success(self, capsys):
        """Every investigation returned `succeeded` and collected nothing.

        This is what an hour against a cluster the caller has no RBAC on looks
        like: the platform correctly reports a locked door, 1,200 times."""
        runs = [make_run(i * 3.0, usable=0) for i in range(1200)]
        for run in runs:
            run.status = "succeeded"
        code, out = summarise(make_state(runs), capsys)
        assert code == 1
        assert "0% of investigations" in out


class TestTheGuardPublishes:
    def test_a_healthy_hour(self, capsys):
        """The false-positive guard. A guard that refuses every run is exactly
        as useful as no guard, and rather harder to notice."""
        code, out = summarise(make_state(healthy()), capsys)
        assert code == 0, out
        assert "REFUSED" not in out
        assert "1200/1200 runs collected usable evidence (100%)" in out

    def test_a_realistic_hour_with_a_few_failures(self, capsys):
        """95% good, the failures scattered. Real runs are not perfect and the
        guard must not require them to be."""
        runs = healthy(1200)
        for index in range(0, 1200, 20):
            runs[index] = make_run(runs[index].finished_at, error="collection timed out")
        code, out = summarise(make_state(runs), capsys)
        assert code == 0, out
        assert "collection timed out" in out  # still reported, just not fatal


class TestTheReportSaysWhy:
    def test_failure_reasons_are_grouped_by_shape(self):
        """`failed 1091` with no breakdown is what let run 3 look publishable.

        Asserted on the counts, not on the printed text: the point is that a
        thousand lines differing only in a port collapse to one finding."""
        runs = [
            make_run(
                float(i),
                error=f"Unable to connect to the server: dial tcp 127.0.0.1:{6443 + i}: refused",
            )
            for i in range(500)
        ]
        runs += [make_run(float(i), error="context deadline exceeded") for i in range(30)]
        runs += [make_run(float(i), usable=12) for i in range(200)]
        reasons = soak.failure_reasons(runs)
        assert sum(reasons.values()) == 530
        assert len(reasons) == 2
        top, count = reasons.most_common(1)[0]
        assert count == 500
        assert "Unable to connect" in top

    def test_a_success_with_no_evidence_is_its_own_reason(self):
        """It is neither a failure nor a usable investigation, and reporting it
        as either loses the one fact that identifies it."""
        run = make_run(1.0, usable=0)
        run.status = "succeeded"
        reasons = soak.failure_reasons([run])
        assert list(reasons) == ["succeeded, but collected no usable evidence"]

    def test_the_reasons_are_printed_even_when_the_run_is_refused(self, capsys):
        """A refused run is precisely the run whose failures someone must read,
        so the breakdown is printed above the guard rather than after it."""
        runs = [make_run(float(i), error="Unable to connect to the server") for i in range(200)]
        _, out = summarise(make_state(runs), capsys)
        assert out.index("Unable to connect") < out.index("REFUSED")


class TestTheTimeline:
    def test_the_trailing_gap_counts(self):
        """The first version built its gap list only *between* good runs, so a
        run whose last usable investigation was at minute 4 of 60 reported a
        longest gap of six seconds. The check was present, correct-looking and
        inert."""
        runs = [make_run(i * 3.0, usable=12) for i in range(81)]
        timeline = soak.usable_timeline(runs, 0.0, HOUR)
        assert timeline["last"] == pytest.approx(240.0)
        assert timeline["longest_gap"] == pytest.approx(HOUR - 240.0)

    def test_the_leading_gap_counts(self):
        """A platform that produced nothing for its first forty minutes was not
        soaking for them either."""
        runs = [make_run(2400.0 + i, usable=12) for i in range(600)]
        timeline = soak.usable_timeline(runs, 0.0, HOUR)
        assert timeline["longest_gap"] == pytest.approx(2400.0)

    def test_no_usable_investigations_is_a_gap_the_length_of_the_run(self):
        timeline = soak.usable_timeline([make_run(1.0, error="x")], 0.0, HOUR)
        assert timeline["count"] == 0
        assert timeline["longest_gap"] == HOUR


class TestTheShapeCollapse:
    def test_a_correlation_id_collapses(self):
        """Every log line the platform writes carries one, and its 4-character
        groups survive an 8-or-more hex rule. A five-minute smoke run reported
        six findings at "1x" each — six investigations, not six findings."""
        lines = [
            f"| WARNING | 4b1e9c3d-0f99-446f-{n:04x}-9c3d4d98b796 | "
            "app.ai.providers.base:complete:31 - No API key configured"
            for n in range(200)
        ]
        shapes = {soak.collapse(line) for line in lines}
        assert len(shapes) == 1
        assert "<uuid>" in shapes.pop()

    def test_distinct_messages_stay_distinct(self):
        """The inverse, and the one that matters: a collapse aggressive enough
        to merge unlike lines reports one finding for two problems."""
        a = soak.collapse("| ERROR | 4b1e9c3d-0f99-446f-8a2b-9c3d4d98b796 | connection refused")
        b = soak.collapse("| ERROR | 4b1e9c3d-0f99-446f-8a2b-9c3d4d98b796 | deadline exceeded")
        assert a != b
