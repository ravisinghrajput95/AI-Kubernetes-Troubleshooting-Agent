"""Measure the objectives in docs/SLO.md against a real Prometheus.

    python scripts/slo_attainment.py --start 2026-09-18T13:22:00Z --end 2026-09-18T15:07:00Z \
        --runs soak.json --enrolled 1

**The expressions are read out of `docs/SLO.md`, not copied here.** That
document says every objective is "measurable the day it is adopted", and until
this script nothing had ever evaluated one: a copy kept here could be quietly
corrected while the published one stayed broken, which is the shape of the four
Prometheus queries that parsed, returned `success` and matched nothing (M9.1).
Only the window changes — 28 days becomes the run — and the few forms that
cannot be evaluated as written are transformed in the open, each for a stated
reason.

**Every objective needs a denominator, or it is refused.** A success rate over
zero investigations, or a soundness rate from a deployment with no model
configured, is not a measurement; printing it would be the vacuous pass every
harness here has had to be taught out of.

SLO 3 has no platform metric by design — the document says to serve it from an
ingress — so it is taken from the load generator's own record of every
submission, which is the same vantage point.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLO_DOC = ROOT / "docs" / "SLO.md"

TARGETS = {
    1: ("Investigation success rate", ">=", 0.99),
    2: ("Investigation latency p95 (s)", "<", 30.0),
    3: ("Submission availability", ">=", 0.999),
    4: ("Evidence completeness", ">=", 0.95),
    5: ("Grounding rejection rate", "<", 0.05),
    6: ("Fleet visibility", ">=", 0.99),
    7: ("Queue depth below capacity (share of samples)", ">=", 0.99),
}

# Subquery resolution for the objectives that are instantaneous ratios. The
# scrape interval, so every sample Prometheus holds is counted once.
STEP = "15s"


@dataclass
class Result:
    number: int
    name: str
    target: str
    value: float | None
    sample: str
    verdict: str  # MET, MISSED or REFUSED
    note: str = ""


def published_expressions(text: str) -> dict[int, str]:
    """The first ```promql block under each `### N.` heading."""
    expressions: dict[int, str] = {}
    for match in re.finditer(r"^### (\d+)\.(.*?)(?=^### |\Z)", text, re.S | re.M):
        block = re.search(r"```promql\n(.*?)```", match.group(2), re.S)
        if block:
            expressions[int(match.group(1))] = block.group(1).strip()
    return expressions


def windowed(expression: str, seconds: int) -> str:
    """The published expression over the run instead of its 28 or 7 days."""
    return re.sub(r"\[\d+[smhdw]\]", f"[{seconds}s]", expression)


def ratio_parts(expression: str) -> tuple[str, str]:
    """Numerator and denominator of an `a / b` objective, split on the lone `/`
    line the document writes them with."""
    parts = re.split(r"^\s*/\s*$", expression, flags=re.M)
    if len(parts) != 2:
        raise ValueError(f"not a two-part ratio: {expression!r}")
    return parts[0].strip(), parts[1].strip()


def verdict(number: int, value: float) -> str:
    _, op, target = TARGETS[number]
    return "MET" if (value >= target if op == ">=" else value < target) else "MISSED"


def target_text(number: int) -> str:
    _, op, target = TARGETS[number]
    return f"{op} {target:g}" if number == 2 else f"{op} {target:.1%}"


class Prometheus:
    def __init__(self, base: str, at: float) -> None:
        self.base = base.rstrip("/")
        self.at = at

    def scalar(self, query: str) -> float | None:
        """One number, or None when the query matched nothing."""
        url = f"{self.base}/api/v1/query?" + urllib.parse.urlencode(
            {"query": query, "time": f"{self.at:.3f}"}
        )
        with urllib.request.urlopen(url, timeout=30) as response:
            body = json.load(response)
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus refused {query!r}: {body}")
        result = body["data"]["result"]
        if not result:
            return None
        if len(result) != 1:
            raise RuntimeError(f"{query!r} returned {len(result)} series; expected one")
        value = float(result[0]["value"][1])
        return None if value != value else value  # NaN is 0/0: nothing to divide


def measure_ratio(number: int, expression: str, prom: Prometheus, seconds: int) -> Result:
    name = TARGETS[number][0]
    numerator, denominator = ratio_parts(windowed(expression, seconds))
    over = prom.scalar(denominator)
    if not over:
        return Result(
            number,
            name,
            target_text(number),
            None,
            "0",
            "REFUSED",
            "the denominator is empty over the window",
        )
    under = prom.scalar(numerator) or 0.0
    value = under / over
    # rate() is per second; the count is what a reader needs to judge the ratio.
    count = f"{over * seconds:,.0f}"
    return Result(number, name, target_text(number), value, count, verdict(number, value))


def measure_latency(expression: str, prom: Prometheus, seconds: int) -> Result:
    # Published as `histogram_quantile(...) < 30`: a comparison returns nothing
    # when it is false, which reads exactly like "no data". The quantile itself
    # is what is measured, and the comparison is made here.
    quantile = re.sub(r"\)\s*<\s*[\d.]+\s*$", ")", windowed(expression, seconds).strip())
    observed = prom.scalar(
        f"sum(increase(k8sagent_investigation_duration_seconds_count[{seconds}s]))"
    )
    if not observed:
        return Result(
            2,
            TARGETS[2][0],
            target_text(2),
            None,
            "0",
            "REFUSED",
            "no investigation durations were observed",
        )
    value = prom.scalar(quantile)
    if value is None:
        return Result(
            2,
            TARGETS[2][0],
            target_text(2),
            None,
            f"{observed:,.0f}",
            "REFUSED",
            "the quantile matched nothing",
        )
    return Result(2, TARGETS[2][0], target_text(2), value, f"{observed:,.0f}", verdict(2, value))


def measure_soundness(expression: str, prom: Prometheus, seconds: int) -> Result:
    # The objective is over answers the model gave, so a window with none has
    # nothing to measure — a deployment without a model, or one whose provider
    # was down throughout. Its first evaluation, gated on `outcome!="skipped"`,
    # reported 100% rejections for exactly that window: the platform never
    # emitted `skipped`, so the gate matched everything.
    answered = prom.scalar(
        f'sum(increase(k8sagent_llm_calls_total{{outcome="succeeded"}}[{seconds}s]))'
    )
    if not answered:
        return Result(
            5,
            TARGETS[5][0],
            target_text(5),
            None,
            "0",
            "REFUSED",
            "the model answered nothing in this window",
        )
    return measure_ratio(5, expression, prom, seconds)


def measure_fleet(expression: str, prom: Prometheus, seconds: int, enrolled: int) -> Result:
    if enrolled <= 0:
        return Result(
            6,
            TARGETS[6][0],
            target_text(6),
            None,
            "0",
            "REFUSED",
            "no enrolled clusters were given (--enrolled)",
        )
    instant = expression.replace("<enrolled cluster count>", str(enrolled))
    # An instantaneous ratio; attainment is its average over the window's samples.
    samples = prom.scalar(f"count_over_time(({instant})[{seconds}s:{STEP}])")
    value = prom.scalar(f"avg_over_time(({instant})[{seconds}s:{STEP}])")
    if not samples or value is None:
        return Result(
            6,
            TARGETS[6][0],
            target_text(6),
            None,
            "0",
            "REFUSED",
            "k8sagent_agents_connected was never scraped",
        )
    return Result(
        6, TARGETS[6][0], target_text(6), value, f"{samples:,.0f} samples", verdict(6, value)
    )


def measure_queue(expression: str, prom: Prometheus, seconds: int) -> Result:
    # `a < b` filters rather than answers; `bool` makes it 1 or 0 per sample so
    # the share of samples that held can be averaged.
    as_bool = re.sub(r"\s<\s", " < bool ", expression.strip(), count=1)
    samples = prom.scalar(f"count_over_time(({as_bool})[{seconds}s:{STEP}])")
    value = prom.scalar(f"avg_over_time(({as_bool})[{seconds}s:{STEP}])")
    if not samples or value is None:
        return Result(
            7,
            TARGETS[7][0],
            target_text(7),
            None,
            "0",
            "REFUSED",
            "queue depth or worker capacity was never scraped",
        )
    return Result(
        7, TARGETS[7][0], target_text(7), value, f"{samples:,.0f} samples", verdict(7, value)
    )


def measure_submission(runs: list[dict], start: float, end: float) -> Result:
    """From the load generator's record: every POST /investigations it made."""
    counted = unavailable = 0
    for run in runs:
        if not (start <= float(run.get("finished_at") or 0) <= end):
            continue
        status = str(run.get("status") or "")
        code = status.removeprefix("submit-") if status.startswith("submit-") else ""
        # 429 is the rate limiter working and 409 a correct refusal; the
        # document excludes both.
        if code in ("429", "409"):
            continue
        counted += 1
        if code == "0" or code.startswith("5"):
            unavailable += 1
    if not counted:
        return Result(
            3,
            TARGETS[3][0],
            target_text(3),
            None,
            "0",
            "REFUSED",
            "no submissions were recorded in the window",
        )
    value = 1 - unavailable / counted
    return Result(
        3,
        TARGETS[3][0],
        target_text(3),
        value,
        f"{counted:,}",
        verdict(3, value),
        f"{unavailable} unavailable",
    )


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def render(results: list[Result], start: str, end: str, seconds: int) -> str:
    lines = [
        f"SLO attainment {start} -> {end} ({seconds / 3600:.2f} h)",
        "",
        f"{'#':>2}  {'objective':<46} {'target':>9} {'measured':>10} {'sample':>16}  verdict",
    ]
    for r in results:
        shown = (
            "—" if r.value is None else (f"{r.value:.2f}" if r.number == 2 else f"{r.value:.2%}")
        )
        lines.append(
            f"{r.number:>2}  {r.name:<46} {r.target:>9} {shown:>10} {r.sample:>16}  {r.verdict}"
            + (f"  ({r.note})" if r.note else "")
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--start", required=True, help="ISO-8601, UTC")
    parser.add_argument("--end", required=True, help="ISO-8601, UTC")
    parser.add_argument("--runs", default="", help="soak_bench.py --json output, for SLO 3")
    parser.add_argument("--enrolled", type=int, default=0, help="enrolled clusters, for SLO 6")
    parser.add_argument("--only", default="", help="comma-separated objective numbers")
    parser.add_argument("--json", default="", help="write the results here as well")
    args = parser.parse_args()

    start, end = parse_time(args.start), parse_time(args.end)
    seconds = int(end - start)
    if seconds <= 0:
        print("--end must be after --start")
        return 2

    expressions = published_expressions(SLO_DOC.read_text())
    missing = sorted(set(TARGETS) - {3} - set(expressions))
    if missing:
        print(f"docs/SLO.md has no promql block for objective(s) {missing}")
        return 2

    wanted = {int(n) for n in args.only.split(",") if n} or set(TARGETS)
    prom = Prometheus(args.prometheus, end)
    runs = json.loads(Path(args.runs).read_text()).get("runs", []) if args.runs else []

    measures = {
        1: lambda: measure_ratio(1, expressions[1], prom, seconds),
        2: lambda: measure_latency(expressions[2], prom, seconds),
        3: lambda: measure_submission(runs, start, end),
        4: lambda: measure_ratio(4, expressions[4], prom, seconds),
        5: lambda: measure_soundness(expressions[5], prom, seconds),
        6: lambda: measure_fleet(expressions[6], prom, seconds, args.enrolled),
        7: lambda: measure_queue(expressions[7], prom, seconds),
    }
    results = [measures[n]() for n in sorted(wanted)]
    print(render(results, args.start, args.end, seconds))

    if args.json:
        Path(args.json).write_text(json.dumps([r.__dict__ for r in results], indent=2))

    # A refusal is not a pass: the run could not answer what it was asked.
    if any(r.verdict == "REFUSED" for r in results):
        return 2
    return 1 if any(r.verdict == "MISSED" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
