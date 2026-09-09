"""Where does the per-worker ceiling bind? Attribute it to the event loop.

`docs/PERFORMANCE_ENVELOPE.md` recorded a per-worker throughput ceiling for
nine milestones and, beside it, the one thing it could not say: *which* of the
event loop, the GIL or the gRPC stream handling that ceiling was. Stack
sampling showed a saturated worker roughly 92% idle with every non-idle sample
in a Postgres or Redis socket wait and no CPU hotspot — a profile consistent
with all three, which is why it settled nothing.

This answers it, by measuring the thing the sampler could not see: **how long
the event loop is unable to advance any task at all**, and how much of that is
spent inside the synchronous job store.

    docker compose up -d postgres redis
    python scripts/loop_bench.py --investigations 48 --slots 8

The cluster is fake, deliberately. The subject is the platform's own overhead
per investigation — the queue writes, the progress events, the lifecycle
transitions — and a real cluster would put a kubectl subprocess in front of it
and measure that instead.

**Two arms measured against different amounts of data are not comparable.**
Every investigation writes around fifty rows to `investigation_events`, so a
few runs leave tens of thousands behind and the arm that runs second is
measured against a bigger table. That confound cost this harness a full
before/after comparison — an A/B that ran uncontrolled reported 2.0x where the
controlled one reports 3.0x. It prints the starting row count for that reason,
and `--reset` empties the tables so a comparison is honest. Alternate the arms
as well: a machine that drifts should drift across both.
"""

import argparse
import asyncio
import collections
import copy
import json
import os
import resource
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/k8sagent"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Sampled often enough to see a block, rarely enough not to be the load.
HEARTBEAT_SECONDS = 0.005

# Every store method that reaches the network. Named rather than discovered so
# a method added later shows up as an absence in the table below instead of
# being silently excluded from the attribution.
INSTRUMENTED = (
    "publish",
    "create",
    "enqueue",
    "get",
    "get_summary",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
)


class Attribution:
    """Who blocked the loop, and for how long."""

    def __init__(self) -> None:
        self.loop_thread: threading.Thread | None = None
        self.calls: collections.Counter = collections.Counter()
        self.seconds: collections.Counter = collections.Counter()
        self.blocked = 0.0

    def instrument(self, store):
        for name in INSTRUMENTED:
            original = getattr(store, name, None)
            if original is None:
                continue

            def wrapped(*args, _original=original, _name=name, **kwargs):
                on_loop = threading.current_thread() is self.loop_thread
                started = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - started
                    self.calls[(_name, "loop" if on_loop else "thread")] += 1
                    if on_loop:
                        self.blocked += elapsed
                        self.seconds[_name] += elapsed

            setattr(store, name, wrapped)
        return store

    @property
    def on_loop(self) -> int:
        return sum(n for (_, where), n in self.calls.items() if where == "loop")

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def build_fake_cluster(pods: int, log_lines: int):
    """A fake cluster, so the subject is the platform and not kubectl."""
    from tests.test_investigation_service import PODS, FakeKubectl

    template = PODS["items"][0]
    items = []
    for index in range(pods):
        pod = copy.deepcopy(template)
        pod["metadata"]["name"] = f"web-{index}"
        pod["spec"]["nodeName"] = f"node-{index % 8}"
        items.append(pod)
    scaled = {"items": items}
    log = "".join(
        f"2026-09-09T10:00:{line % 60:02d}Z ERROR request failed id={line}\n"
        for line in range(log_lines)
    )

    class Scaled(FakeKubectl):
        def run(self, args, parse_json: bool = False):
            result = super().run(args, parse_json)
            if args[0] == "logs":
                return type(result)(result.command, True, log, "", 0)
            if (
                args[0] == "get"
                and len(args) > 1
                and args[1] in {"pods", "pod"}
                and not (len(args) > 2 and not args[2].startswith("-"))
            ):
                return type(result)(result.command, True, json.dumps(scaled), "", 0, data=scaled)
            return result

    return Scaled()


async def heartbeat(stop: asyncio.Event, lags: list[float]) -> None:
    """Stands in for everything else the worker owes its callers.

    An HTTP request, an SSE frame, an agent's stream message: each is a task
    waiting for the loop. Overshoot beyond the requested sleep is time the loop
    could not advance it.
    """
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(HEARTBEAT_SECONDS)
        lags.append(time.perf_counter() - started - HEARTBEAT_SECONDS)


async def measure(args, attribution: Attribution) -> dict:
    from app.jobs.distributed import PostgresRedisJobStore
    from app.jobs.runner import InvestigationJobRunner
    from app.models.investigation import InvestigationRequest
    from app.persistence.postgres import Database
    from app.persistence.redis_bus import RedisBus
    from app.providers.local_kubectl import LocalKubectlProvider
    from app.services import investigation_service

    attribution.loop_thread = threading.current_thread()

    database = Database(args.database_url)
    database.migrate()
    if args.reset:
        with database.cursor() as cursor:
            cursor.execute(
                "truncate investigation_events, investigation_reports, investigations cascade"
            )
    with database.cursor() as cursor:
        cursor.execute("select count(*) from investigation_events")
        events_before = cursor.fetchone()[0]

    store = attribution.instrument(PostgresRedisJobStore(database, RedisBus(args.redis_url)))

    fake = build_fake_cluster(args.pods, args.log_lines)
    investigation_service.select_provider = lambda *a, **k: LocalKubectlProvider(
        context="loop-bench", executor=fake
    )

    runner = InvestigationJobRunner(store)
    lags: list[float] = []
    stop = asyncio.Event()
    beat = asyncio.create_task(heartbeat(stop, lags))
    await asyncio.sleep(0.02)  # the heartbeat has to be running to observe anything

    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    inflight: set[asyncio.Task] = set()
    for _ in range(args.investigations):
        while len(inflight) >= args.slots:
            _, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
        request = InvestigationRequest(cluster="loop-bench", namespace="default")
        # The real request path, then the consumer's: `submit` queues the job
        # on the distributed store, so something has to stand in for the worker
        # that would claim it.
        submitted = runner.submit(request)
        job = await submitted if asyncio.iscoroutine(submitted) else submitted
        inflight.add(runner.start(job.id, request))
    if inflight:
        await asyncio.wait(inflight)
    elapsed = time.perf_counter() - started
    usage_after = resource.getrusage(resource.RUSAGE_SELF)

    stop.set()
    await beat

    completed = sum(1 for job in store.list(limit=args.investigations * 2) if job.status.terminal)
    database.close()

    cpu = (usage_after.ru_utime - usage_before.ru_utime) + (
        usage_after.ru_stime - usage_before.ru_stime
    )
    return {
        "investigations": args.investigations,
        "completed": completed,
        "elapsed_seconds": elapsed,
        "throughput_per_second": args.investigations / elapsed,
        "loop_blocked_seconds": sum(lag for lag in lags if lag > 0),
        "loop_blocked_share": sum(lag for lag in lags if lag > 0) / elapsed,
        "blocked_in_store_seconds": attribution.blocked,
        "heartbeat_samples": len(lags),
        "lag_ms": {
            "p50": statistics.median(lags) * 1000 if lags else 0.0,
            "p99": sorted(lags)[int(len(lags) * 0.99)] * 1000 if lags else 0.0,
            "max": max(lags) * 1000 if lags else 0.0,
        },
        "cpu_seconds": cpu,
        "cpu_share_of_one_core": cpu / elapsed,
        "store_calls_on_loop": attribution.on_loop,
        "store_calls_total": attribution.total,
        "events_rows_before": events_before,
        "slots": args.slots,
    }


def refuse(report: dict, attribution: Attribution) -> str | None:
    """A clean measurement that means nothing is the failure mode here.

    Each of these describes a run that prints beautifully and measures
    something other than what it claims — the same shape as a soak of a
    platform doing nothing, or a differential of two providers that failed
    identically.
    """
    if report["completed"] < report["investigations"]:
        return (
            f"only {report['completed']} of {report['investigations']} investigations "
            f"reached a terminal state — this measured a platform that was failing, "
            f"and the loop of a worker doing nothing is never blocked."
        )
    if report["heartbeat_samples"] < 10:
        return (
            f"the heartbeat ran {report['heartbeat_samples']} times — too few to "
            f"describe a distribution. Either the run was too short or the loop "
            f"was blocked so completely that the probe itself could not sample it."
        )
    if attribution.total == 0:
        return (
            "no job-store calls were seen at all, so the attribution is empty and "
            "the harness — not the platform — is what changed. Check INSTRUMENTED "
            "against the store's method names."
        )
    return None


def render(report: dict, attribution: Attribution) -> None:
    print()
    print("=" * 72)
    print(
        f"  {report['investigations']} investigations in {report['elapsed_seconds']:.2f}s"
        f"  ->  {report['throughput_per_second']:.1f}/s   (slots={report['slots']})"
    )
    print("=" * 72)
    blocked = report["loop_blocked_seconds"]
    print(
        f"  event loop BLOCKED     {blocked:7.2f}s of {report['elapsed_seconds']:.2f}s"
        f"   ({100 * report['loop_blocked_share']:.0f}% of wall clock)"
    )
    share = report["blocked_in_store_seconds"] / blocked if blocked else 0.0
    print(
        f"  ...inside store calls  {report['blocked_in_store_seconds']:7.2f}s"
        f"              ({100 * share:.0f}% of the blocking)"
    )
    print(
        f"  CPU                    {report['cpu_seconds']:7.2f}s"
        f"              ({100 * report['cpu_share_of_one_core']:.0f}% of one core)"
    )
    lag = report["lag_ms"]
    print(f"  loop lag p50/p99/max   {lag['p50']:.2f} / {lag['p99']:.1f} / {lag['max']:.1f} ms")
    print()
    print(
        f"  store calls on the event loop: {report['store_calls_on_loop']}"
        f" of {report['store_calls_total']}"
    )
    for (name, where), count in sorted(attribution.calls.items(), key=lambda kv: -kv[1]):
        seconds = attribution.seconds.get(name, 0.0) if where == "loop" else 0.0
        of_blocking = (
            f"{100 * seconds / attribution.blocked:5.1f}% of loop-blocking"
            if where == "loop" and attribution.blocked
            else ""
        )
        print(f"    {count:5d}  {name:<15} {where:<7} {seconds:6.2f}s  {of_blocking}")
    print()
    print(
        f"  (investigation_events held {report['events_rows_before']} rows at start;"
        f" compare arms at the same size)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--investigations", type=int, default=48)
    parser.add_argument("--slots", type=int, default=8)
    parser.add_argument("--pods", type=int, default=60)
    parser.add_argument("--log-lines", type=int, default=40)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="empty the investigation tables first, so two arms are comparable",
    )
    parser.add_argument("--json", type=Path, help="write the report here as well")
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
    args = parser.parse_args(argv)

    os.environ.setdefault("AUTH_MODE", "disabled")
    os.environ.setdefault("ALLOW_INSECURE_NO_AUTH", "true")
    os.environ["DATABASE_URL"] = args.database_url
    os.environ["REDIS_URL"] = args.redis_url

    attribution = Attribution()
    report = asyncio.run(measure(args, attribution))
    render(report, attribution)

    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\n  wrote {args.json}")

    refusal = refuse(report, attribution)
    if refusal:
        print(f"\n  REFUSED: {refusal}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
