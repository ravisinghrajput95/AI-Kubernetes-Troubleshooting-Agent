"""Where the bytes are in an investigation, and what re-reads them.

M8b's brief is "evidence payloads to object storage". Before designing a
storage layer this measures the thing it would be storing: how large a result
actually is, which parts of it dominate, and how often each part crosses a
wire. Building object storage for a payload that turns out to be 40 KB would be
answering a question nobody asked.

    docker compose up -d postgres redis      # only for --db
    python scripts/payload_bench.py --pods 200

The cluster is a fake, driven through the real pipeline — the same collectors,
scheduler, redaction, analysis and report composition an investigation uses. It
is scaled by pod count because pods and their logs are what grow with a
cluster; nodes and services do not grow the same way.

`MAX_LIST_ITEMS` (default 2000) is the ceiling a real investigation hits, so
`--pods 2000` is the worst case the platform currently allows itself.
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import tracemalloc
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def kilobytes(payload) -> float:
    return len(json.dumps(payload, default=str).encode()) / 1024


def build_fake(pods: int, log_lines: int):
    """A fake cluster of `pods` pods, each with `log_lines` of logs."""
    from tests.test_investigation_service import PODS, FakeKubectl

    template = PODS["items"][0]
    items = []
    for index in range(pods):
        pod = copy.deepcopy(template)
        pod["metadata"]["name"] = f"web-{index}"
        pod["spec"]["nodeName"] = f"node-{index % 50}"
        items.append(pod)
    scaled = {"items": items}

    # Realistic log volume: a crash-looping container emits far more than the
    # two lines the unit fixture carries, and logs are the payload that grows
    # fastest with cluster size.
    log = "".join(
        f"2026-08-02T10:00:{line % 60:02d}Z ERROR request failed id={line} "
        f"upstream=payments-api latency_ms={line * 3}\n"
        for line in range(log_lines)
    )

    class Scaled(FakeKubectl):
        def run(self, args, parse_json: bool = False):
            result = super().run(args, parse_json)
            if args[0] == "logs":
                return type(result)(result.command, True, log, "", 0)
            if (
                args[0] in {"get"}
                and len(args) > 1
                and args[1] in {"pods", "pod"}
                and not (len(args) > 2 and not args[2].startswith("-"))
            ):
                return type(result)(result.command, True, json.dumps(scaled), "", 0, data=scaled)
            return result

    return Scaled()


async def run_investigation(pods: int, log_lines: int) -> dict:
    from app.ai.root_cause_analyzer import RootCauseAnalyzer
    from app.providers.local_kubectl import LocalKubectlProvider
    from app.services.investigation_service import InvestigationService

    service = InvestigationService(context="bench-cluster")
    service.provider = LocalKubectlProvider(
        context="bench-cluster", executor=build_fake(pods, log_lines)
    )
    investigation = await service.run()
    diagnosis = await asyncio.to_thread(RootCauseAnalyzer().analyze, investigation)
    return {"investigation": investigation, "diagnosis": diagnosis}


def breakdown(result: dict, top: int = 12) -> list[tuple[str, float]]:
    """Every section of the stored result, largest first."""
    sizes: list[tuple[str, float]] = []
    for outer, payload in result.items():
        if isinstance(payload, dict):
            for key, value in payload.items():
                sizes.append((f"{outer}.{key}", kilobytes(value)))
        else:
            sizes.append((outer, kilobytes(payload)))
    return sorted(sizes, key=lambda pair: -pair[1])[:top]


# What a caller polling for progress actually reads. Everything else in the
# response is payload it already has or does not want yet.
#
# Note what this is *not* claiming. `result` is NULL until the job finishes, so
# a poll during the run is already cheap — the waste is on the paths that read
# a *finished* result without wanting it: the listing, and any client that keeps
# polling an id after it reached a terminal state.
STATUS_KEYS = {
    "id",
    "owner",
    "status",
    "request",
    "created_at",
    "started_at",
    "finished_at",
    "duration_ms",
    "progress",
    "timeline",
    "error",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pods", type=int, default=200)
    parser.add_argument("--log-lines", type=int, default=200)
    parser.add_argument("--list-size", type=int, default=25, help="Rows a listing returns.")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Also report peak heap held during the run, which sizes JOB_MAX_CONCURRENT.",
    )
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)

    os.environ.setdefault("OPENAI_API_KEY", "")

    peak_bytes = 0
    if arguments.memory:
        # A throwaway run first: the first investigation in a process pays for
        # every module import, and tracing that would report the interpreter
        # rather than the investigation.
        asyncio.run(run_investigation(20, 20))
        tracemalloc.start()

    result = asyncio.run(run_investigation(arguments.pods, arguments.log_lines))

    if arguments.memory:
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    total = kilobytes(result)
    sections = breakdown(result)

    # The status projection: what a poll needs, versus what it is served today.
    from app.jobs.models import InvestigationJob

    job = InvestigationJob(id="bench", request={}, result=result)
    full = kilobytes(job.to_dict())
    status_only = kilobytes({k: v for k, v in job.to_dict().items() if k in STATUS_KEYS})

    report = {
        "cluster": {"pods": arguments.pods, "log_lines_per_pod": arguments.log_lines},
        "stored_result_kb": round(total, 1),
        "largest_sections_kb": {name: round(size, 1) for name, size in sections},
        "finished_job_read": {
            "served_kb": round(full, 1),
            "status_projection_kb": round(status_only, 1),
            "ratio": round(full / status_only, 1) if status_only else 0,
        },
        "concurrency": (
            {
                # Peak heap for one investigation. Roughly 5x the stored result,
                # measured flat across cluster sizes — so this is what one slot
                # of JOB_MAX_CONCURRENT costs a worker.
                "peak_heap_mb": round(peak_bytes / 1024 / 1024, 1),
                "peak_over_stored": round(peak_bytes / 1024 / total, 1) if total else 0,
                "investigations_per_gb": int(1024 / (peak_bytes / 1024 / 1024))
                if peak_bytes
                else 0,
            }
            if arguments.memory
            else {}
        ),
        "listing": {
            "rows": arguments.list_size,
            # `list()` SELECTs `result` for every row and the API then calls
            # `to_dict(include_result=False)`. The blobs cross the wire from
            # Postgres and are discarded in Python.
            "results_read_from_postgres_kb": round(total * arguments.list_size, 1),
            "results_returned_to_caller_kb": 0.0,
        },
    }

    if arguments.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"\nFake cluster: {arguments.pods} pods, {arguments.log_lines} log lines each")
    print(f"Stored result (investigations.result jsonb): {total:.1f} KB\n")
    print("  largest sections")
    for name, size in sections:
        share = size / total * 100 if total else 0
        print(f"    {name:<38} {size:>9.1f} KB  {share:>5.1f}%")

    read = report["finished_job_read"]
    print(f"\n  reading one finished job serves   {read['served_kb']:>9.1f} KB")
    print(f"  its status projection alone       {read['status_projection_kb']:>9.1f} KB")
    print(f"  ratio                             {read['ratio']:>9.1f}x")
    print("  (legitimate once, when the console renders the report; waste on")
    print("   every read after that, and on any client still polling the id)")

    if arguments.memory:
        memory = report["concurrency"]
        print(f"\n  peak heap for one run             {memory['peak_heap_mb']:>9.1f} MB")
        print(f"  as a multiple of the result       {memory['peak_over_stored']:>9.1f}x")
        print(
            f"  fits per GB of worker memory      {memory['investigations_per_gb']:>9}"
            "   (sizes JOB_MAX_CONCURRENT)"
        )

    listing = report["listing"]
    print(
        f"\n  a {listing['rows']}-row listing reads          "
        f"{listing['results_read_from_postgres_kb'] / 1024:>9.1f} MB of results from Postgres"
    )
    print(f"  and returns                       {0.0:>9.1f} KB of them")
    print("  (list() SELECTs `result`; the API discards it in Python)\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
