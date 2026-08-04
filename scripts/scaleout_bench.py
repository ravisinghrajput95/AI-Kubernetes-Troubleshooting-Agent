#!/usr/bin/env python3
"""Does throughput keep rising as workers are added past two?

Tier-5 item 44. `docs/PERFORMANCE_ENVELOPE.md` measured workers 1 to 2 at
12.1 to 23.0/s and called it linear, then said so honestly: scale-out beyond
two workers is unmeasured. This measures it.

    docker compose up -d postgres redis
    python scripts/scaleout_bench.py --workers 1,2,3,4 --investigations 240

**The published throughput figure has been wrong twice**, and both retractions
came from a harness that could not see its own limits. So this one refuses to
report a scaling factor it cannot support:

  * The **submit rate is measured separately** from the completion rate. If the
    harness cannot offer work faster than the fleet drains it, the fleet's
    ceiling has not been found and saying otherwise is how ~10/s was first
    published as a platform limit.
  * Every run reports **queue drain time**, so a run where the queue was empty
    most of the time is visible as the under-load run it was.
  * A worker count whose throughput is within noise of the previous one is
    reported as *flat*, not as a ceiling — one run cannot tell the two apart.

The workload is a cluster that refuses connections, so `collect` fails fast and
what is measured is the platform's own work: analysis, report rendering,
persistence and the queue round trip. That is deliberate. Collection time is
the customer's cluster, not the platform, and including it would measure the
network.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
PYTHON = BACKEND / ".venv" / "bin" / "python"

DATABASE_URL = os.environ.get(
    "SCALEOUT_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/k8sagent_scaleout",
)
REDIS_URL = os.environ.get("SCALEOUT_REDIS_URL", "redis://127.0.0.1:6379/8")
TOKEN = "scaleout-token"
BASE_PORT = 8820

# Refused, not stalled: collection fails in milliseconds so the measurement is
# of platform work rather than of a kubectl timeout.
REFUSING_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1", insecure-skip-tls-verify: true}
  name: refused
contexts:
- context: {cluster: refused, user: nobody}
  name: refused
current-context: refused
users:
- name: nobody
  user: {token: scaleout-placeholder}
"""


@dataclass
class Run:
    workers: int
    submitted: int
    completed: int
    submit_seconds: float
    drain_seconds: float

    @property
    def throughput(self) -> float:
        return self.completed / self.drain_seconds if self.drain_seconds > 0 else 0.0

    @property
    def submit_rate(self) -> float:
        return self.submitted / self.submit_seconds if self.submit_seconds > 0 else 0.0


def request(url: str, method: str = "GET", body: dict | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}


def prepare_database() -> None:
    import psycopg

    admin = DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    name = DATABASE_URL.rsplit("/", 1)[1]
    with (
        psycopg.connect(admin, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if not cursor.fetchone():
            cursor.execute(f'CREATE DATABASE "{name}"')


def truncate() -> None:
    """Each run starts from an empty queue and an empty table.

    Without this, run N's drain time includes run N-1's leftovers and the
    scaling curve is an artefact of accumulation.
    """
    import psycopg
    import redis

    # First run: the workers apply migrations at startup, so there may be
    # nothing to clear yet.
    with (
        psycopg.connect(DATABASE_URL, autocommit=True) as connection,
        connection.cursor() as cursor,
        contextlib.suppress(psycopg.errors.UndefinedTable),
    ):
        cursor.execute(
            "TRUNCATE investigation_events, investigation_reports, investigations CASCADE"
        )
    redis.Redis.from_url(REDIS_URL).flushdb()


def start_workers(count: int, kubeconfig: Path, workdir: Path, concurrency: int) -> list:
    processes = []
    for index in range(count):
        environment = {
            **os.environ,
            "DATABASE_URL": DATABASE_URL,
            "REDIS_URL": REDIS_URL,
            "REDIS_KEY_PREFIX": "scaleout",
            "AUTH_MODE": "token",
            "API_TOKENS": f"{TOKEN}:bench@example.com",
            "KUBECONFIG": str(kubeconfig),
            "KUBECTL_TIMEOUT_SECONDS": "5",
            "JOB_MAX_CONCURRENT": str(concurrency),
            "WORKER_ID": f"scale-{index}",
            "REPORT_RETENTION_DAYS": "0",
            "RATE_LIMIT_PER_MINUTE": "100000",
            "LOG_LEVEL": "WARNING",
        }
        log = (workdir / f"w{count}-{index}.log").open("w")
        processes.append(
            subprocess.Popen(
                [
                    str(PYTHON),
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(BASE_PORT + index),
                    "--log-level",
                    "warning",
                ],
                cwd=BACKEND,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        )
    return processes


def await_all_ready(count: int, timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(
            request(f"http://127.0.0.1:{BASE_PORT + index}/health/ready", timeout=5)[0] == 200
            for index in range(count)
        ):
            return True
        time.sleep(0.5)
    return False


def terminal_count() -> int:
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM investigations WHERE status IN "
            "('succeeded', 'failed', 'cancelled')"
        )
        return cursor.fetchone()[0]


def measure(workers: int, investigations: int, kubeconfig: Path, workdir: Path, concurrency: int):
    truncate()
    processes = start_workers(workers, kubeconfig, workdir, concurrency)
    try:
        if not await_all_ready(workers):
            print(f"  {workers} worker(s): did not become ready", flush=True)
            return None

        endpoints = [f"http://127.0.0.1:{BASE_PORT + i}/investigations" for i in range(workers)]

        # Submitted from a thread pool wider than the fleet, so the harness is
        # not the thing being measured. Its own rate is reported below.
        submit_started = time.perf_counter()
        accepted = 0
        with ThreadPoolExecutor(max_workers=min(32, investigations)) as pool:
            futures = [
                pool.submit(
                    request,
                    endpoints[index % workers],
                    "POST",
                    {"namespace": "default"},
                    60,
                )
                for index in range(investigations)
            ]
            for future in futures:
                status, _ = future.result()
                if status in (200, 202):
                    accepted += 1
        submit_seconds = time.perf_counter() - submit_started

        # Timed from the *first* submission, not from the last. Workers begin
        # draining the moment the first id is queued, so starting the clock
        # after submission credits the fleet with none of the work it did
        # during it and understates throughput by the submit window.
        drain_started = submit_started
        deadline = time.perf_counter() + 600
        completed = 0
        while time.perf_counter() < deadline:
            completed = terminal_count()
            if completed >= accepted:
                break
            time.sleep(0.25)
        drain_seconds = time.perf_counter() - drain_started

        return Run(workers, accepted, completed, submit_seconds, drain_seconds)
    finally:
        for process in processes:
            process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()


def report(runs: list[Run]) -> None:
    print(f"\n{'=' * 74}")
    print("workers   completed   submit/s   throughput/s   per worker   vs 1 worker")
    print("-" * 74)
    baseline = runs[0].throughput if runs else 0.0
    for run in runs:
        factor = run.throughput / baseline if baseline else 0.0
        print(
            f"{run.workers:>7}   {run.completed:>9}   {run.submit_rate:>8.1f}   "
            f"{run.throughput:>12.1f}   {run.throughput / run.workers:>10.1f}   "
            f"{factor:>10.2f}x"
        )
    print("-" * 74)

    print("\nreading:")
    for previous, current in itertools.pairwise(runs):
        gain = current.throughput / previous.throughput if previous.throughput else 0.0
        expected = current.workers / previous.workers
        efficiency = gain / expected if expected else 0.0
        if efficiency >= 0.85:
            verdict = "linear"
        elif efficiency >= 0.5:
            verdict = "sublinear"
        else:
            verdict = "flat — adding workers bought little"
        print(
            f"  {previous.workers} -> {current.workers} workers: "
            f"{gain:.2f}x against {expected:.2f}x ideal ({efficiency:.0%}) — {verdict}"
        )

    print(
        "\n  Every worker here runs on ONE host and, on the local-kubeconfig path,\n"
        "  shells out to kubectl for each read. Process spawning is a host resource,\n"
        "  not a per-worker one, so a flat curve above may be this machine rather than\n"
        "  the platform. `docs/PERFORMANCE_ENVELOPE.md` measured 1->2 workers as linear\n"
        "  on the *agent* path, where collection spawns nothing. Before quoting a\n"
        "  ceiling, check that throughput still rises with --concurrency: if it does\n"
        "  not, the limit is not the worker count."
    )

    saturated = [run for run in runs if run.submit_rate < run.throughput * 1.5]
    if saturated:
        print(
            "\n  WARNING: at "
            + ", ".join(f"{run.workers}w" for run in saturated)
            + " the harness submitted barely faster than the fleet drained. "
            "Throughput there is a floor, not a ceiling — raise --investigations "
            "before quoting it."
        )
    else:
        print(
            "\n  Submission outpaced completion at every point, so the queue was "
            "backed up throughout and these are fleet limits rather than offered-load limits."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workers", default="1,2,3,4", help="Comma-separated worker counts.")
    parser.add_argument("--investigations", type=int, default=240)
    parser.add_argument("--concurrency", type=int, default=4, help="JOB_MAX_CONCURRENT per worker.")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per worker count; median wins.")
    arguments = parser.parse_args()

    counts = [int(value) for value in arguments.workers.split(",")]
    prepare_database()

    with tempfile.TemporaryDirectory(prefix="scaleout-") as raw:
        workdir = Path(raw)
        kubeconfig = workdir / "kubeconfig"
        kubeconfig.write_text(REFUSING_KUBECONFIG)

        runs = []
        for count in counts:
            samples = []
            for attempt in range(arguments.repeat):
                print(
                    f"\n--- {count} worker(s), {arguments.investigations} investigations "
                    f"(run {attempt + 1}/{arguments.repeat}) ---",
                    flush=True,
                )
                run = measure(
                    count,
                    arguments.investigations,
                    kubeconfig,
                    workdir,
                    arguments.concurrency,
                )
                if run is None:
                    continue
                print(
                    f"  submitted {run.submitted} in {run.submit_seconds:.1f}s "
                    f"({run.submit_rate:.1f}/s), drained in {run.drain_seconds:.1f}s "
                    f"-> {run.throughput:.1f}/s",
                    flush=True,
                )
                samples.append(run)
            if samples:
                samples.sort(key=lambda item: item.throughput)
                runs.append(samples[len(samples) // 2])

        if not runs:
            print("no runs completed")
            return 1
        report(runs)
        return 0


if __name__ == "__main__":
    sys.exit(main())
