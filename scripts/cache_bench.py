"""What a second investigation of the same cluster actually costs.

`docs/PERFORMANCE_ENVELOPE.md` measured `collect` at 65% of an investigation,
and until F18 every investigation paid it in full: ~20 reads of the whole
cluster, each one a `kubectl` subprocess, repeated from scratch however recently
the last investigation ran. This measures the claim that reusing them is worth
having, and — more importantly — that what it serves is not stale in a way that
would corrupt a citation.

    python scripts/cache_bench.py --context kind-my-cluster

**It refuses to run against a fake.** A cache saves a subprocess and a cluster
round trip; a fake executor has neither, so a fake would measure `json.loads`
against `json.loads` and report a number that means nothing. That is the same
mistake `fleet_bench.py` made when it printed a platform ceiling from a
saturated client, so the harness names its cluster and reads it for real.

Three numbers, and the third is the one that matters:

- **subprocesses**, counted by wrapping `subprocess.run`. Not the timing —
  timings on a laptop are a coin flip, and the count is the thing that scales
  with a fleet and with a WAN.
- **collect wall time**, reported for scale rather than as the claim.
- **the age of the oldest evidence** in the warm run. A cache that served
  everything and dated it `now` would post an excellent number here and be
  exactly the defect this feature was written to avoid, so the harness asserts
  the warm run's evidence is *visibly older* than the run itself.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


class Counter:
    """How many kubectl processes the platform actually spawned.

    Wrapping `subprocess.run` in the executor's own module rather than counting
    `executed_commands`: that list deliberately keeps cached reads, so counting
    it would report the same number warm and cold and quietly prove nothing.
    """

    def __init__(self) -> None:
        self.calls = 0

    def install(self):
        from app.kubernetes import kubectl_executor

        original = kubectl_executor.subprocess.run

        def counted(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)

        kubectl_executor.subprocess.run = counted
        return lambda: setattr(kubectl_executor.subprocess, "run", original)


async def one_run(context: str, namespace: str | None, refresh: bool) -> dict:
    from app.services.investigation_service import InvestigationService

    counter = Counter()
    restore = counter.install()
    started = time.perf_counter()
    try:
        service = InvestigationService(context=context, namespace=namespace, refresh=refresh)
        investigation = await service.run()
    finally:
        restore()

    cache = investigation.get("collection_cache") or {}
    evidence = investigation.get("evidence") or []
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "kubectl_processes": counter.calls,
        "cache_hits": cache.get("hits", 0),
        "cache_misses": cache.get("misses", 0),
        "oldest_evidence_seconds": cache.get("oldest_evidence_seconds"),
        "evidence_records": len(evidence),
        "usable_records": sum(1 for item in evidence if item.get("status") in {"ok", "empty"}),
        "commands_recorded": len(investigation.get("executed_commands") or []),
    }


def check(label: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", required=True, help="A real kubeconfig context.")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--ttl", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)

    probe = subprocess.run(
        ["kubectl", "--context", arguments.context, "get", "nodes", "-o", "name"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        print(f"Cannot reach {arguments.context}: {probe.stderr.strip()}")
        return 2

    from app.core.config import settings
    from app.providers.cache import get_collection_cache, reset_collection_cache

    settings.collection_cache_ttl_seconds = arguments.ttl
    reset_collection_cache()

    cold = asyncio.run(one_run(arguments.context, arguments.namespace, refresh=False))
    warm = asyncio.run(one_run(arguments.context, arguments.namespace, refresh=False))
    forced = asyncio.run(one_run(arguments.context, arguments.namespace, refresh=True))
    stats = get_collection_cache().stats()

    report = {
        "context": arguments.context,
        "ttl_seconds": arguments.ttl,
        "cold": cold,
        "warm": warm,
        "refresh": forced,
        "cache": stats,
        "reduction": {
            "kubectl_processes_pct": round(
                (1 - warm["kubectl_processes"] / cold["kubectl_processes"]) * 100, 1
            )
            if cold["kubectl_processes"]
            else 0.0,
            "collect_seconds_pct": round((1 - warm["seconds"] / cold["seconds"]) * 100, 1)
            if cold["seconds"]
            else 0.0,
        },
    }

    if arguments.json:
        print(json.dumps(report, indent=2))

    print(f"\nCluster {arguments.context}, cache TTL {arguments.ttl:.0f}s\n")
    header = f"  {'':<10}{'kubectl':>9}{'seconds':>9}{'hits':>7}{'misses':>8}{'evidence':>10}"
    print(header)
    for name in ("cold", "warm", "refresh"):
        run = report[name]
        print(
            f"  {name:<10}{run['kubectl_processes']:>9}{run['seconds']:>9.2f}"
            f"{run['cache_hits']:>7}{run['cache_misses']:>8}{run['usable_records']:>10}"
        )
    print(
        f"\n  second investigation spawns {report['reduction']['kubectl_processes_pct']:.0f}% "
        f"fewer kubectl processes, {report['reduction']['collect_seconds_pct']:.0f}% less collect time"
    )
    print(f"  cache holds {stats['entries']} reads, {stats['bytes'] / 1024:.0f} KB\n")

    # The assertions. A benchmark that cannot fail publishes confident nonsense.
    passed = [
        check(
            "the cold run really read the cluster",
            cold["kubectl_processes"] > 0 and cold["usable_records"] > 0,
            f"{cold['kubectl_processes']} processes, {cold['usable_records']} usable records",
        ),
        check(
            "the warm run does measurably less work",
            warm["kubectl_processes"] < cold["kubectl_processes"],
            f"{warm['kubectl_processes']} vs {cold['kubectl_processes']} processes",
        ),
        check(
            "the warm run sees the same cluster",
            warm["usable_records"] == cold["usable_records"],
            f"{warm['usable_records']} usable records both times",
        ),
        check(
            "reused evidence is dated when it was read, not when it was served",
            (warm["oldest_evidence_seconds"] or 0) >= cold["seconds"],
            f"oldest fact is {warm['oldest_evidence_seconds']}s old, "
            f"the cold run took {cold['seconds']}s",
        ),
        check(
            "a reused read is still reproducible",
            warm["commands_recorded"] >= cold["commands_recorded"],
            f"{warm['commands_recorded']} commands recorded vs {cold['commands_recorded']}",
        ),
        check(
            "refresh really re-reads the cluster",
            forced["kubectl_processes"] >= cold["kubectl_processes"] and forced["cache_hits"] == 0,
            f"{forced['kubectl_processes']} processes, {forced['cache_hits']} hits",
        ),
    ]

    print()
    if not all(passed):
        print(f"{passed.count(False)} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
