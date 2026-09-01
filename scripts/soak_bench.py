"""Run the platform continuously against a real cluster, and report what moved.

Every other harness in `scripts/` finishes in seconds and answers "how fast".
This one answers "what happens if you leave it on", which is a different
question and the one `docs/PERFORMANCE_ENVELOPE.md` records as unmeasured:

    **Sustained operation.** The longest run here is under a minute. Nothing
    says what happens over hours: no leak test, no certificate rotation under
    load, no Postgres growth over a retention period.

Four things are watched, because each is a claim the platform makes that no
short run can test:

- **Resident memory with the collection cache on.** F18 added a per-process
  LRU holding raw cluster reads. `COLLECTION_CACHE_MAX_BYTES` bounds the cache;
  nothing bounds what a leak elsewhere does over an hour, and nothing had run
  long enough to tell the two apart.
- **The retention sweep.** It runs in-process on a timer of at least thirty
  minutes, so a test suite never sees it fire. Its cost against real rows is
  unknown.
- **Certificate renewal under load.** Renewal at two thirds of life is pinned
  by unit tests against a thirty-second certificate in one process. Whether a
  real agent renews repeatedly, through a real gateway, while collections are
  flowing, is not the same claim.
- **The two transports.** SSE and polling are meant to be interchangeable. A
  divergence would show as a difference in *outcomes*, not in the transport's
  own account of itself.

## The vacuity guard, which is the whole reason this is trustworthy

A soak that measures a platform doing nothing measures nothing, and it reports
beautifully: flat memory, no errors, no leak. Every failure mode of this
repository's harnesses has been of that shape — a passing run that asserted
nothing. So the run **fails** unless it produced a floor of investigations that
actually succeeded *and* collected usable evidence from the cluster. Memory
trends computed from fewer are refused rather than published.

    docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 postgres:17-alpine
    docker compose up -d redis
    python scripts/soak_bench.py --minutes 60 --context kind-aiops-test --agent
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
PYTHON = BACKEND / ".venv" / "bin" / "python"

TOKEN = "soak-token"
SUBJECT = "soak@example.com"
DATABASE_URL = os.environ.get(
    "SOAK_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/k8sagent_soak"
)
REDIS_URL = os.environ.get("SOAK_REDIS_URL", "redis://127.0.0.1:6379/4")
PREFIX = "soak"


# --- plumbing ----------------------------------------------------------------


def request(url: str, method: str = "GET", body: dict | None = None, timeout: float = 120):
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
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"detail": payload}
    except Exception as exc:
        return 0, {"detail": f"{type(exc).__name__}: {exc}"}


def scrape(url: str, timeout: float = 10) -> dict[str, float]:
    """Parse the Prometheus exposition into {series_with_labels: value}."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            continue
    return out


def process_memory(pid: int) -> tuple[float, int]:
    """RSS in MB and thread count, from ps.

    Deliberately not psutil: this repository adds a dependency for a reason,
    and a soak harness is not one. `ps -M` lists one line per thread on macOS;
    on Linux it is not supported and the thread count is read from /proc.
    """
    rss = 0.0
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        )
        if out.stdout.strip():
            rss = int(out.stdout.strip().split()[0]) / 1024.0
    except Exception:
        pass

    threads = 0
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("Threads:"):
                threads = int(line.split()[1])
    else:
        try:
            out = subprocess.run(
                ["ps", "-M", "-p", str(pid)], capture_output=True, text=True, timeout=10
            )
            threads = max(0, len(out.stdout.strip().splitlines()) - 1)
        except Exception:
            pass
    return rss, threads


def open_files(pid: int) -> int:
    try:
        out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=25)
        return max(0, len(out.stdout.strip().splitlines()) - 1)
    except Exception:
        return 0


def prepare_database(reset: bool) -> None:
    """Create the soak database, optionally from scratch.

    From scratch by default: every storage figure here is a *growth* figure,
    and a database carrying a previous run's rows reports the sum of two runs
    as the trend of one. The same applies to `agent_certificates`, which is how
    many certificates the run's own renewals produced.
    """
    sys.path.insert(0, str(BACKEND))
    import psycopg

    admin = DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    name = DATABASE_URL.rsplit("/", 1)[1]
    with (
        psycopg.connect(admin, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        if reset:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if not cursor.fetchone():
            cursor.execute(f'CREATE DATABASE "{name}"')


class ProbeFailed(Exception):
    """The harness could not read something. Not the platform's problem."""


def sql(statement: str, args: tuple = ()) -> list[tuple]:
    import psycopg

    try:
        with (
            psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10) as connection,
            connection.cursor() as cur,
        ):
            cur.execute(statement, args)
            if cur.description is None:
                return []
            return cur.fetchall()
    except Exception as exc:
        raise ProbeFailed(f"{type(exc).__name__}: {exc}") from exc


def _database_reachable() -> bool:
    try:
        sql("SELECT 1")
    except ProbeFailed:
        return False
    return True


def _try(call, *args, default):
    """Run a probe, or give up on it. Never end the run over instrumentation."""
    try:
        return call(*args)
    except ProbeFailed:
        return default


def pinned_kubeconfig(context: str, workdir: Path) -> Path:
    """Flatten one context into its own file.

    The agent has no `--context` flag and follows current-context, and the
    platform's own reads would otherwise depend on whatever the ambient
    kubeconfig happens to say at the moment a collector runs. An hour is long
    enough for that to change under us.
    """
    path = workdir / "kubeconfig"
    out = subprocess.run(
        ["kubectl", "config", "view", "--minify", "--flatten", f"--context={context}"],
        capture_output=True,
        text=True,
        check=True,
    )
    path.write_text(out.stdout)
    return path


# --- the workers -------------------------------------------------------------


@dataclass
class Worker:
    name: str
    port: int
    process: subprocess.Popen
    log: Path
    gateway_port: int = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_worker(name: str, port: int, kubeconfig: Path, workdir: Path, **extra) -> Worker:
    environment = {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "REDIS_URL": REDIS_URL,
        "REDIS_KEY_PREFIX": PREFIX,
        "AUTH_MODE": "token",
        "API_TOKENS": f"{TOKEN}:{SUBJECT}",
        "KUBECONFIG": str(kubeconfig),
        "KUBECTL_TIMEOUT_SECONDS": "30",
        "JOB_MAX_CONCURRENT": "4",
        "WORKER_ID": name,
        "METRICS_ENABLED": "true",
        # Deliberately out of the way. A soak is meant to load the platform;
        # a 429 measures the quota an operator set, which is already covered
        # by `tests/test_rate_limiting.py` and is not what an hour buys.
        "RATE_LIMIT_PER_MINUTE": "100000",
        # The subject of the run: the cache on at its shipped defaults.
        "COLLECTION_CACHE_TTL_SECONDS": "60",
        "COLLECTION_CACHE_MAX_BYTES": str(64 * 1024 * 1024),
        # Retention at its floor, so the sweep fires inside an hour rather than
        # every six. One day is the smallest value that still prunes: 0 disables.
        "REPORT_RETENTION_DAYS": "1",
        "REPORT_RETENTION_SWEEP_HOURS": "0.5",
        "AGENT_IDENTITY_DIR": str(workdir / "identity"),
        **{key: str(value) for key, value in extra.items()},
    }
    log = workdir / f"{name}.log"
    handle = log.open("w")
    process = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return Worker(
        name=name,
        port=port,
        process=process,
        log=log,
        gateway_port=int(extra.get("AGENT_GATEWAY_PORT", 0) or 0),
    )


def await_ready(worker: Worker, timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = request(f"{worker.base}/health/ready", timeout=5)
        if status == 200:
            return True
        if worker.process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


# --- the load ----------------------------------------------------------------


@dataclass
class Run:
    """One investigation, from submit to a terminal answer."""

    worker: str
    transport: str
    refresh: bool
    job_id: str = ""
    status: str = ""
    seconds: float = 0.0
    usable: int = 0
    provider: str = ""
    cache_hits: int = 0
    cache_misses: int = 0
    events: int = 0
    out_of_order: int = 0
    duplicate_events: int = 0
    error: str = ""
    finished_at: float = 0.0


TERMINAL = {"succeeded", "failed", "cancelled"}


def consume_sse(worker: Worker, job_id: str, run: Run, deadline: float) -> None:
    """Read the event stream to its end, checking the sequence as it goes.

    The sequence is the SSE frame id and the store's de-duplication key, so a
    repeat or a step backwards here is the M3 subscribe-before-backlog property
    failing — which is invisible to a client that only waits for a terminal
    status.

    What is **not** checked is contiguity, and that was the first version of
    this: `investigation_events.seq` is a `bigserial` shared by every
    investigation in the database, so one stream's ids are naturally sparse.
    Under load that reported a 10% "gap rate" that was nothing but other
    investigations interleaving. The properties that exist are monotonicity and
    uniqueness.
    """
    url = f"{worker.base}/investigations/{job_id}/events"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    seen: set[int] = set()
    last = -1
    try:
        with urllib.request.urlopen(req, timeout=max(5, deadline - time.time())) as stream:
            for raw in stream:
                line = raw.decode(errors="replace").strip()
                if line.startswith("id:"):
                    seq = int(line.split(":", 1)[1].strip())
                    if seq in seen:
                        run.duplicate_events += 1
                    elif last >= 0 and seq < last:
                        run.out_of_order += 1
                    seen.add(seq)
                    last = max(last, seq)
                elif line.startswith("data:"):
                    run.events += 1
                    payload = line.split(":", 1)[1].strip()
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if event.get("type") == "completed" or event.get("status") in TERMINAL:
                        return
                if time.time() > deadline:
                    return
    except Exception as exc:
        run.error = run.error or f"sse: {type(exc).__name__}: {exc}"


def drive(
    worker: Worker, transport: str, refresh: bool, namespace: str | None, context: str
) -> Run:
    run = Run(worker=worker.name, transport=transport, refresh=refresh)
    started = time.time()
    body: dict = {"refresh": refresh, "context": context}
    if namespace:
        body["namespace"] = namespace

    status, payload = request(f"{worker.base}/investigations", "POST", body, timeout=60)
    if status not in (200, 202):
        run.status = f"submit-{status}"
        run.error = str(payload.get("detail", ""))[:200]
        run.seconds = time.time() - started
        run.finished_at = time.time()
        return run

    run.job_id = payload.get("id", "")
    deadline = started + 300

    if transport == "sse":
        consume_sse(worker, run.job_id, run, deadline)

    # Both transports converge on the same terminal read, which is the point:
    # a divergence must show up in the answer, not in how it was watched.
    while time.time() < deadline:
        code, body = request(f"{worker.base}/investigations/{run.job_id}/status", timeout=30)
        if code == 200 and body.get("status") in TERMINAL:
            run.status = body["status"]
            break
        if code != 200:
            run.status = f"status-{code}"
            run.error = run.error or str(body.get("detail", ""))[:200]
            break
        time.sleep(0.5 if transport == "poll" else 1.0)
    else:
        run.status = "timeout"

    run.seconds = time.time() - started
    run.finished_at = time.time()

    if run.status == "succeeded":
        code, full = request(f"{worker.base}/investigations/{run.job_id}", timeout=60)
        if code == 200:
            body_or_result = full.get("result") or full
            investigation = body_or_result.get("investigation") or {}
            coverage = investigation.get("evidence_coverage") or {}
            run.usable = int(coverage.get("usable") or 0)
            run.provider = str((investigation.get("cluster_access") or {}).get("provider") or "")
            cache = investigation.get("collection_cache") or {}
            run.cache_hits = int(cache.get("hits") or 0)
            run.cache_misses = int(cache.get("misses") or 0)
        else:
            run.error = run.error or f"fetch-{code}"
    return run


def load_thread(
    workers: list[Worker],
    stop: threading.Event,
    results: queue.Queue,
    transport: str,
    pause: float,
    refresh_rate: float,
    namespaces: list[str | None],
    context: str,
    rng: random.Random,
) -> None:
    while not stop.is_set():
        worker = rng.choice(workers)
        run = drive(
            worker,
            transport,
            refresh=rng.random() < refresh_rate,
            namespace=rng.choice(namespaces),
            context=context,
        )
        results.put(run)
        if stop.wait(pause):
            return


# --- what is watched ---------------------------------------------------------


@dataclass
class Sample:
    at: float
    rss: dict[str, float] = field(default_factory=dict)
    threads: dict[str, int] = field(default_factory=dict)
    files: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    db_bytes: int = 0
    redis_bytes: int = 0
    rows: int = 0


def redis_memory() -> int:
    try:
        out = subprocess.run(
            ["docker", "exec", "ai-kubernetes-agent-redis", "redis-cli", "INFO", "memory"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in out.stdout.splitlines():
            if line.startswith("used_memory:"):
                return int(line.split(":", 1)[1])
    except Exception:
        pass
    return 0


def sample_thread(
    workers: list[Worker], stop: threading.Event, samples: list[Sample], interval: float
) -> None:
    slow = 0
    while not stop.is_set():
        sample = Sample(at=time.time())
        for worker in workers:
            rss, threads = process_memory(worker.process.pid)
            sample.rss[worker.name] = rss
            sample.threads[worker.name] = threads
            # lsof is expensive; every sixth sample is enough to see a trend.
            if slow % 6 == 0:
                sample.files[worker.name] = open_files(worker.process.pid)
            sample.metrics.update(
                {
                    f"{worker.name}::{key}": value
                    for key, value in scrape(f"{worker.base}/metrics").items()
                }
            )
        try:
            sample.db_bytes = int(sql("SELECT pg_database_size(current_database())")[0][0])
            sample.rows = int(sql("SELECT count(*) FROM investigations")[0][0])
        except Exception:
            pass
        sample.redis_bytes = redis_memory()
        samples.append(sample)
        slow += 1
        stop.wait(interval)


def trend_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope in units per hour. Empty or degenerate input is 0."""
    if len(points) < 3:
        return 0.0
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return slope * 3600.0


# --- the retention sweep, given something to sweep ---------------------------


def age_reports(count: int, days: int = 3) -> int:
    """Backdate copies of real finished investigations so the sweep has work.

    The sweep only fires on a timer of at least thirty minutes and only deletes
    rows past `REPORT_RETENTION_DAYS`, so on a fresh database it runs, finds
    nothing and costs nothing — which is a measurement of the timer, not of the
    sweep. Copying *real* rows rather than fabricating them is what makes the
    cost figure mean anything: the payload being nulled and the blobs being
    deleted are the sizes this platform actually produces.
    """
    rows = sql(
        """
        SELECT id FROM investigations
         WHERE result IS NOT NULL AND status = 'succeeded'
         ORDER BY created_at DESC LIMIT %s
        """,
        (count,),
    )
    made = 0
    for (source,) in rows:
        aged = f"{source}-aged"
        sql(
            """
            INSERT INTO investigations
                (id, tenant_id, owner, principal, status, request, result, error,
                 created_at, started_at, finished_at, history_item)
            SELECT %s, tenant_id, owner, principal, status, request, result, error,
                   now() - %s::interval, started_at, finished_at, history_item
              FROM investigations WHERE id = %s
            ON CONFLICT (id) DO NOTHING
            """,
            (aged, f"{days} days", source),
        )
        sql(
            """
            INSERT INTO investigation_reports
                (investigation_id, tenant_id, format, content, created_at)
            SELECT %s, tenant_id, format, content, now() - %s::interval
              FROM investigation_reports WHERE investigation_id = %s
            ON CONFLICT DO NOTHING
            """,
            (aged, f"{days} days", source),
        )
        made += 1
    return made


def aged_state() -> tuple[int, int, int]:
    """(aged rows, of those still carrying a payload, surviving report blobs)."""
    total = int(sql("SELECT count(*) FROM investigations WHERE id LIKE '%%-aged'")[0][0])
    with_result = int(
        sql("SELECT count(*) FROM investigations WHERE id LIKE '%%-aged' AND result IS NOT NULL")[
            0
        ][0]
    )
    blobs = int(
        sql("SELECT count(*) FROM investigation_reports WHERE investigation_id LIKE '%%-aged'")[0][
            0
        ]
    )
    return total, with_result, blobs


# --- the caller's own RBAC, so impersonation is on the path -------------------

CLUSTER_ROLE_NAME = "k8s-agent-soak-reader"


def _cluster_role_from(manifest: str) -> str | None:
    """Lift the read-only ClusterRole out of the platform's own manifest.

    Copied rather than written here on purpose. F7 was eight reads naming a
    kind the agent did not have, and it lasted because two lists of resources
    existed with nothing comparing them. A third list, in a benchmark, would be
    the same mistake — so the grant under test is the shipped one, and a read
    the grant forgot shows up as evidence this run did not collect.
    """
    for document in manifest.split("\n---\n"):
        if "kind: ClusterRole\n" not in document and not document.strip().startswith(
            "kind: ClusterRole"
        ):
            continue
        if "rules:" not in document or "pods/log" not in document:
            continue
        return re.sub(r"^(\s*name:).*$", rf"\1 {CLUSTER_ROLE_NAME}", document, count=1, flags=re.M)
    return None


def grant_caller_rbac(manifest: str, context: str) -> bool:
    """Bind the soak's subject to that role, so `--as` reads succeed.

    Without this the whole run is vacuous in the most convincing way possible:
    impersonation is on by default, `soak@example.com` exists in no binding,
    every read comes back FORBIDDEN, and the platform correctly reports a
    locked door — flat memory, no errors, no findings, an hour wasted.
    """
    role = _cluster_role_from(manifest)
    if role is None:
        return False
    binding = f"""
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {CLUSTER_ROLE_NAME}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {CLUSTER_ROLE_NAME}
subjects:
  - apiGroup: rbac.authorization.k8s.io
    kind: User
    name: {SUBJECT}
"""
    out = subprocess.run(
        ["kubectl", f"--context={context}", "apply", "-f", "-"],
        input=role + "\n---\n" + binding,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print(f"  ! could not grant the caller RBAC: {out.stderr.strip()[:200]}")
        return False
    return True


def revoke_caller_rbac(context: str) -> None:
    for kind in ("clusterrolebinding", "clusterrole"):
        subprocess.run(
            [
                "kubectl",
                f"--context={context}",
                "delete",
                kind,
                CLUSTER_ROLE_NAME,
                "--ignore-not-found",
            ],
            capture_output=True,
            text=True,
        )


# --- the agent leg -----------------------------------------------------------


def start_agent(
    binary: Path, cluster: str, worker: Worker, enrolment: dict, workdir: Path, kubeconfig: Path
) -> subprocess.Popen:
    ca_file = workdir / "ca.crt"
    ca_file.write_text(enrolment["ca_bundle"])
    log = (workdir / "agent.log").open("w")
    enrolment_port = worker.gateway_port + 1
    return subprocess.Popen(
        [
            str(binary),
            "--kubeconfig",
            str(kubeconfig),
            "--cluster",
            cluster,
            "--gateway",
            f"127.0.0.1:{worker.gateway_port}",
            "--enrol",
            f"127.0.0.1:{enrolment_port}",
            "--bootstrap-token",
            enrolment["token"],
            "--ca-file",
            str(ca_file),
            "--identity-dir",
            str(workdir / "agent-identity"),
            "--renewal-check",
            "5s",
        ],
        env={**os.environ, "KUBECONFIG": str(kubeconfig)},
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def certificate_serials(cluster: str) -> list[str]:
    try:
        rows = sql(
            "SELECT serial FROM agent_certificates WHERE cluster_id = %s ORDER BY issued_at",
            (cluster,),
        )
        return [str(row[0]) for row in rows]
    except Exception:
        return []


# --- the report --------------------------------------------------------------

LOG_NOISE = re.compile(
    r"\b(ERROR|CRITICAL|Traceback|WARNING)\b",
)


def collapse(text: str) -> str:
    """Reduce a line to its shape, so identical events count as one finding.

    An hour of investigations produces thousands of lines differing only in a
    timestamp, an id and a port. "The same failure 1,091 times" is one finding;
    1,091 lines is a haystack.
    """
    # UUIDs first, and specifically: every log line here carries a correlation
    # id, whose 4-character groups the 8-or-more rule below does not touch. A
    # five-minute smoke run reported six findings at "1x" each — six different
    # investigations rather than six findings — because of exactly this.
    shape = re.sub(r"\b[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<uuid>", text)
    shape = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", shape)
    shape = re.sub(r"\d{4}-\d{2}-\d{2}[ T][\d:.,]+", "", shape)
    shape = re.sub(r"\b\d+(\.\d+)?\b", "<n>", shape)
    return shape.strip()


def log_findings(workers: list[Worker]) -> dict[str, Counter]:
    """Group the run's own log noise, so a soak reports what it saw."""
    findings: dict[str, Counter] = {}
    for worker in workers:
        counter: Counter = Counter()
        try:
            text = worker.log.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not LOG_NOISE.search(line):
                continue
            counter[collapse(line)[:200]] += 1
        findings[worker.name] = counter
    return findings


def failure_reasons(runs: list[Run]) -> Counter:
    """Why the investigations that did not succeed did not succeed.

    Without this a refused run reports "failed 1091" and nothing else, which is
    exactly what let a soak of a cluster that had been dead for 56 minutes look
    publishable. The reason was in every one of those runs the whole time.
    """
    counter: Counter = Counter()
    for run in runs:
        if run.status == "succeeded" and run.usable > 0:
            continue
        if run.error:
            reason = collapse(run.error)[:200]
        elif run.status == "succeeded":
            reason = "succeeded, but collected no usable evidence"
        else:
            reason = f"{run.status or 'no terminal status'}, no error reported"
        counter[reason] += 1
    return counter


def usable_timeline(runs: list[Run], started: float, elapsed: float) -> dict:
    """When the platform was actually working, not just how often.

    A soak is a claim about *sustained* operation, so the share of runs that
    worked is not enough on its own: 81 usable investigations out of 1,172 is
    the same 7% whether they were spread over the hour or all landed in the
    first four minutes before the cluster died. Only the second is a lie, and
    only a timeline can tell them apart.
    """
    good = sorted(run.finished_at for run in runs if run.status == "succeeded" and run.usable > 0)
    if not good:
        return {
            "count": 0,
            "first": None,
            "last": None,
            "longest_gap": elapsed,
        }
    # Both edges count, and the trailing one is the whole point: run 3's last
    # usable investigation was at minute 4 of 60, and a gap list built only
    # *between* good runs reports a longest gap of six seconds for it. The
    # first version of this function did exactly that, and the check sat inert
    # behind a share check that happened to fire.
    marks = [started, *good, started + elapsed]
    gaps = [b - a for a, b in itertools.pairwise(marks)]
    return {
        "count": len(good),
        "first": good[0] - started,
        "last": good[-1] - started,
        "longest_gap": max(gaps),
    }


def summarise(state: dict) -> int:
    runs: list[Run] = state["runs"]
    samples: list[Sample] = state["samples"]
    minutes = state["elapsed"] / 60.0

    print("\n" + "=" * 78)
    print(f"SOAK — {minutes:.1f} minutes, {len(runs)} investigations")
    if state.get("interruption"):
        print(f"TRUNCATED: {state['interruption']}")
    print("=" * 78)

    by_status = Counter(run.status for run in runs)
    succeeded = [run for run in runs if run.status == "succeeded"]
    with_evidence = [run for run in succeeded if run.usable > 0]

    print("\nOutcomes")
    for status, count in by_status.most_common():
        print(f"  {status:<18} {count}")

    # --- why the rest did not work -------------------------------------------
    #
    # Printed above the guard, and unconditionally, because a refused run is
    # exactly the run whose failures someone needs to read.
    reasons = failure_reasons(runs)
    if reasons:
        print(f"\nWhy {sum(reasons.values())} investigations did not produce usable evidence")
        for reason, count in reasons.most_common(8):
            print(f"  {count:>5}x  {reason[:150]}")

    # --- the vacuity guard ---------------------------------------------------
    #
    # Everything below is a measurement of a platform under load. If it was not
    # under load, the measurements are of an idle process and every one of them
    # looks healthy. Refuse rather than publish.
    #
    # Three questions, because each admits a run the other two accept:
    #
    #   volume       did enough happen to compute a trend from?
    #   share        was the platform *working*, or failing most of the time?
    #   continuity   was it working *throughout*, which is the only claim a
    #                soak is entitled to make?
    #
    # The share and the continuity checks are here because an absolute floor
    # alone published a run in which 81 of 1,172 investigations succeeded, all
    # of them before the cluster died four minutes in. It cleared a floor of 60
    # and every memory trend below it was computed from an hour of a platform
    # returning "Unable to connect".
    floor = state["floor"]
    share = len(with_evidence) / len(runs) if runs else 0.0
    timeline = usable_timeline(runs, state["started"], state["elapsed"])
    # Forgiving at the scale of a few investigations, strict at the scale of a
    # soak: whichever is larger, five minutes or a tenth of the run.
    allowed_gap = max(300.0, state["elapsed"] * 0.10)

    print("\nSustained?")
    print(
        f"  {len(with_evidence)}/{len(runs)} runs collected usable evidence "
        f"({100 * share:.0f}%), floor {floor}, minimum share {100 * state['min_share']:.0f}%"
    )
    if timeline["count"]:
        print(
            f"  first at {timeline['first'] / 60:.1f}m, last at {timeline['last'] / 60:.1f}m "
            f"of {minutes:.1f}m; longest quiet gap {timeline['longest_gap'] / 60:.1f}m "
            f"(allowed {allowed_gap / 60:.1f}m)"
        )

    refusals = []
    if len(with_evidence) < floor:
        refusals.append(
            f"only {len(with_evidence)} investigations collected usable evidence, "
            f"below the floor of {floor}"
        )
    if share < state["min_share"]:
        refusals.append(
            f"{100 * share:.0f}% of investigations collected usable evidence, "
            f"below the required {100 * state['min_share']:.0f}%"
        )
    if timeline["longest_gap"] > allowed_gap:
        refusals.append(
            f"the platform went {timeline['longest_gap'] / 60:.1f} minutes without a usable "
            f"investigation, which is longer than the {allowed_gap / 60:.1f} allowed — "
            "this is not a soak, it is a run that stopped working part way"
        )
    if refusals:
        print("\nREFUSED")
        for refusal in refusals:
            print(f"  - {refusal}")
        print("A soak of a platform that was not working measures nothing.")
        if succeeded:
            print(f"  ({len(succeeded)} succeeded, {len(with_evidence)} of those saw the cluster.)")
        return 1

    durations = sorted(run.seconds for run in succeeded)
    print(
        f"\nLatency (succeeded, n={len(durations)}): "
        f"p50 {durations[len(durations) // 2]:.2f}s  "
        f"p95 {durations[int(len(durations) * 0.95)]:.2f}s  "
        f"max {durations[-1]:.2f}s"
    )
    print(f"Throughput: {len(runs) / minutes:.1f} investigations/min offered")

    # --- memory --------------------------------------------------------------
    print("\nResident memory")
    half = samples[len(samples) // 2 :]
    for worker in state["worker_names"]:
        series = [(s.at, s.rss[worker]) for s in samples if worker in s.rss and s.rss[worker] > 0]
        late = [(s.at, s.rss[worker]) for s in half if worker in s.rss and s.rss[worker] > 0]
        if not series:
            continue
        slope = trend_per_hour(late)
        print(
            f"  {worker:<10} start {series[0][1]:6.1f} MB  peak {max(v for _, v in series):6.1f} MB"
            f"  end {series[-1][1]:6.1f} MB  trend(2nd half) {slope:+.1f} MB/h"
        )
    for worker in state["worker_names"]:
        threads = [s.threads.get(worker, 0) for s in samples if s.threads.get(worker)]
        files = [s.files.get(worker, 0) for s in samples if s.files.get(worker)]
        if threads:
            print(f"  {worker:<10} threads {threads[0]} → {threads[-1]}", end="")
        if files:
            print(f"   open files {files[0]} → {files[-1]}", end="")
        print()

    # --- the cache -----------------------------------------------------------
    hits = sum(run.cache_hits for run in succeeded)
    misses = sum(run.cache_misses for run in succeeded)
    total = hits + misses
    print("\nCollection cache")
    print(
        f"  {hits} hits / {misses} misses"
        + (f"  ({100.0 * hits / total:.0f}% of reads reused)" if total else "")
    )
    refreshed = [run for run in succeeded if run.refresh]
    if refreshed:
        forced = sum(run.cache_hits for run in refreshed)
        print(f"  {len(refreshed)} run with refresh=true; those served {forced} reads from cache")

    # --- storage -------------------------------------------------------------
    measured = [sample for sample in samples if sample.db_bytes > 0]
    if measured:
        first, last = measured[0], measured[-1]
        print("\nStorage")
        print(
            f"  postgres {first.db_bytes / 1e6:.1f} MB → {last.db_bytes / 1e6:.1f} MB "
            f"over {last.rows - first.rows} new rows"
        )
        print(f"  redis    {first.redis_bytes / 1e6:.1f} MB → {last.redis_bytes / 1e6:.1f} MB")
        db_series = [(s.at, float(s.db_bytes)) for s in half if s.db_bytes > 0]
        print(f"  postgres trend(2nd half) {trend_per_hour(db_series) / 1e6:+.1f} MB/h")

    # --- retention -----------------------------------------------------------
    retention = state.get("retention") or {}
    print("\nRetention sweep")
    if not retention.get("seeded"):
        print("  not exercised: no aged rows were seeded")
    else:
        print(
            f"  seeded {retention['seeded']} rows dated 3 days back, "
            f"{retention['blobs_before']} report blobs"
        )
        print(
            f"  after the sweep: {retention['with_result_after']} still carry a payload, "
            f"{retention['blobs_after']} blobs remain"
        )
        if retention.get("swept_at"):
            print(f"  the platform's own sweep fired {retention['swept_after'] / 60:.0f}m in")
        else:
            print("  NOT OBSERVED — the platform's sweep did not fire within the run")
        if retention.get("cost_rows"):
            print(
                f"  cost: {retention['cost_seconds'] * 1000:.0f} ms to prune "
                f"{retention['cost_rows']} aged investigations "
                f"({retention['cost_blobs']} report blobs)"
            )

    # --- transports ----------------------------------------------------------
    print("\nTransports")
    for transport in ("sse", "poll"):
        subset = [run for run in runs if run.transport == transport]
        if not subset:
            continue
        good = [r for r in subset if r.status == "succeeded"]
        evidence = sorted({r.usable for r in good})
        print(
            f"  {transport:<5} {len(subset):>4} runs, {len(good)} succeeded, "
            f"usable evidence {evidence[0] if evidence else 0}-{evidence[-1] if evidence else 0}"
        )
    sse_runs = [r for r in runs if r.transport == "sse"]
    backwards = sum(r.out_of_order for r in sse_runs)
    dupes = sum(r.duplicate_events for r in sse_runs)
    frames = sum(r.events for r in sse_runs)
    print(f"  SSE frames {frames}, out of order {backwards}, duplicates {dupes}")
    stream_errors = [r for r in sse_runs if r.error.startswith("sse:")]
    if stream_errors:
        print(f"  SSE stream errors: {len(stream_errors)}")
        for shape, count in Counter(r.error[:120] for r in stream_errors).most_common(3):
            print(f"    {count:>4}x {shape}")

    # --- providers -----------------------------------------------------------
    providers = Counter(run.provider for run in succeeded if run.provider)
    if providers:
        print("\nCluster access")
        for provider, count in providers.most_common():
            print(f"  {provider:<12} {count}")

    # --- certificates --------------------------------------------------------
    certs = state.get("certificates") or {}
    print("\nAgent certificate renewal")
    if not certs.get("enabled"):
        print("  not exercised: the run had no agent")
    else:
        print(
            f"  {certs['serials_end']} certificates issued for {certs['cluster']} "
            f"(started with {certs['serials_start']})"
        )
        print(
            f"  agent-served investigations after the last renewal: "
            f"{certs.get('after_last_renewal', 0)}"
        )
        if certs.get("stream_restarts"):
            print(f"  agent process restarts: {certs['stream_restarts']}")

    # --- log noise -----------------------------------------------------------
    print("\nWhat the logs said")
    findings = state["findings"]
    any_noise = False
    for worker, counter in findings.items():
        for shape, count in counter.most_common(6):
            any_noise = True
            print(f"  {worker} {count:>5}x  {shape[:150]}")
    if not any_noise:
        print("  nothing at WARNING or above")

    print()
    return 0


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--context", default="kind-aiops-test")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=2, help="Clients per transport.")
    parser.add_argument(
        "--pause",
        type=float,
        default=7.0,
        help=(
            "Seconds a client waits between investigations. The default is a sustained "
            "rate, not a peak one: an hour is the point, and 180/min against a single "
            "kind cluster took the Docker daemon down twice before it was reached."
        ),
    )
    parser.add_argument("--refresh-rate", type=float, default=0.2)
    parser.add_argument("--sample-seconds", type=float, default=10.0)
    parser.add_argument("--agent", action="store_true", help="Run a real Go agent as well.")
    parser.add_argument("--agent-binary", default=os.environ.get("AGENT_BINARY", "/tmp/k8s-agent"))
    parser.add_argument(
        "--cert-ttl-hours",
        type=float,
        default=0.5,
        help=(
            "Certificate lifetime for the soak's agent. Thirty minutes renews about three "
            "times in an hour: enough to say rotation survives repetition under load, which "
            "is the claim, without the CA becoming the subject. The shipped default is 90 "
            "days and would renew never. 0.025 (90s) is the pathological setting kept for "
            "stressing renewal itself — it mints a certificate every few seconds, because "
            "the CA backdates NotBefore by five minutes and the renewal point of anything "
            "under 150s is already in the past when it is issued."
        ),
    )
    parser.add_argument("--workdir", default="")
    parser.add_argument("--json", default="", help="Write the raw series here.")
    parser.add_argument("--floor", type=int, default=0, help="Minimum useful investigations.")
    parser.add_argument(
        "--min-share",
        type=float,
        default=0.80,
        help=(
            "Share of investigations that must have collected usable evidence. An absolute "
            "floor alone cannot see a platform that was failing most of the time."
        ),
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--keep-database",
        action="store_true",
        help="Do not drop the soak database first. Growth figures then span runs.",
    )
    args = parser.parse_args()

    if not PYTHON.exists():
        print(f"No backend virtualenv at {PYTHON}")
        return 2

    workdir = Path(args.workdir) if args.workdir else Path(f"/tmp/k8s-soak-{int(time.time())}")
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"Working directory: {workdir}")

    prepare_database(reset=not args.keep_database)
    kubeconfig = pinned_kubeconfig(args.context, workdir)

    workers: list[Worker] = []
    agent: subprocess.Popen | None = None
    granted = False
    state: dict = {
        "floor": args.floor or max(20, int(args.minutes)),
        "min_share": args.min_share,
        "retention": {},
    }

    try:
        for index in range(args.workers):
            # Every worker runs a gateway, because that is the shipped
            # topology: one Deployment, one config, N replicas. It also matters
            # for what is measured. `select_provider` consults the fleet
            # presence index — and refuses to answer an agent-held cluster from
            # a same-named local context — only on a worker that has a gateway
            # of its own, since that is the branch presence is installed under.
            # Giving the first worker a gateway and not the second made a third
            # of this run's investigations fall back to the local kubeconfig
            # silently, which measured the harness rather than the platform.
            gateway = 18443 + index * 2
            extra: dict = {
                "AGENT_GATEWAY_PORT": str(gateway),
                "AGENT_ENROLMENT_PORT": str(gateway + 1),
                "AGENT_GATEWAY_ADVERTISE": f"127.0.0.1:{gateway}",
                "AGENT_GATEWAY_DNS_NAMES": "localhost",
                "AGENT_GATEWAY_IP_ADDRESSES": "127.0.0.1",
                "AGENT_CERT_TTL_HOURS": str(args.cert_ttl_hours),
            }
            worker = start_worker(
                f"worker-{index + 1}", 18000 + index, kubeconfig, workdir, **extra
            )
            workers.append(worker)

        for worker in workers:
            if not await_ready(worker):
                print(f"{worker.name} never became ready; see {worker.log}")
                print(worker.log.read_text()[-3000:])
                return 2
        print(f"{len(workers)} workers ready")

        # The caller's own RBAC, from the platform's own manifest.
        code, enrolment = request(
            f"{workers[0].base}/agents/enrolment",
            "POST",
            {"cluster_id": args.context, "ttl_minutes": 60},
            timeout=60,
        )
        if code != 201:
            print(f"Could not mint an enrolment ({code}): {enrolment}")
            return 2
        granted = grant_caller_rbac(enrolment["manifest"], args.context)
        print(
            f"Impersonated reads as {SUBJECT}: "
            + ("granted the shipped read-only ClusterRole" if granted else "NOT GRANTED")
        )
        if not granted:
            print("  Refusing to soak: every read would be forbidden and the run would be vacuous.")
            return 2

        if args.agent:
            binary = Path(args.agent_binary)
            if not binary.exists():
                print(f"No agent binary at {binary}; build it with `go build ./cmd/agent`.")
                return 2
            agent = start_agent(binary, args.context, workers[0], enrolment, workdir, kubeconfig)
            for _ in range(60):
                code, body = request(f"{workers[0].base}/agents", timeout=10)
                if code == 200 and body.get("items"):
                    break
                time.sleep(1)
            else:
                print("The agent never checked in; see", workdir / "agent.log")
                return 2
            serials_at_start = len(certificate_serials(args.context))
            print(
                f"Agent attached with {serials_at_start} certificate(s), "
                f"life {args.cert_ttl_hours * 3600:.0f}s"
            )

        # --- run -------------------------------------------------------------
        stop = threading.Event()
        results: queue.Queue = queue.Queue()
        samples: list[Sample] = []
        threads = [
            threading.Thread(
                target=sample_thread,
                args=(workers, stop, samples, args.sample_seconds),
                daemon=True,
            )
        ]
        namespaces: list[str | None] = [None, "default", "kube-system"]
        for transport in ("sse", "poll"):
            for index in range(args.concurrency):
                threads.append(
                    threading.Thread(
                        target=load_thread,
                        args=(
                            workers,
                            stop,
                            results,
                            transport,
                            args.pause,
                            args.refresh_rate,
                            namespaces,
                            args.context,
                            random.Random(args.seed + index + (0 if transport == "sse" else 100)),
                        ),
                        daemon=True,
                    )
                )
        started = time.time()
        for thread in threads:
            thread.start()
        print(f"Running for {args.minutes:g} minutes; Ctrl-C stops early.\n")

        deadline = started + args.minutes * 60
        seeded_at = 0.0
        retention = state["retention"]
        last_report = 0.0
        interruption = ""
        probe_failures = 0
        try:
            while time.time() < deadline:
                time.sleep(5)
                now = time.time()

                # Every probe below reads Postgres, which on this machine lives
                # in the same Docker daemon as the cluster under test — and that
                # daemon died three times during this work, twice mid-run. A
                # harness that loses fifty minutes of measurement because its
                # *instrumentation* could not connect has reported nothing,
                # which is worse than reporting a truncated hour. Same rule as
                # `_safe()` in `app/observability`: instrumentation must not
                # fail the thing it measures.
                if not _database_reachable():
                    probe_failures += 1
                    if probe_failures >= 3:
                        interruption = (
                            f"the database became unreachable {(now - started) / 60:.0f}m in; "
                            f"reporting what was measured up to that point"
                        )
                        print(f"  [{(now - started) / 60:.0f}m] {interruption}")
                        break
                    continue
                probe_failures = 0

                # Seed the retention sweep's work once there are real rows to copy.
                if not seeded_at and now - started > 120:
                    made = _try(age_reports, 10, default=0)
                    if made:
                        seeded_at = now
                        _, with_result, blobs = aged_state()
                        retention.update(
                            {
                                "seeded": made,
                                "blobs_before": blobs,
                                "with_result_before": with_result,
                            }
                        )
                        print(f"  [{(now - started) / 60:.0f}m] seeded {made} aged investigations")

                # Watch for the sweep to take them.
                if seeded_at and not retention.get("swept_at"):
                    probe = time.time()
                    _, with_result, blobs = _try(aged_state, default=(0, -1, 0))
                    if 0 <= with_result < retention.get("with_result_before", 0):
                        retention["swept_at"] = probe
                        retention["swept_after"] = probe - started
                        print(f"  [{(probe - started) / 60:.0f}m] the retention sweep fired")

                if now - last_report > 300:
                    last_report = now
                    done = results.qsize()
                    rss = (
                        ", ".join(
                            f"{name} {samples[-1].rss.get(name, 0):.0f}MB"
                            for name in [w.name for w in workers]
                        )
                        if samples
                        else ""
                    )
                    print(f"  [{(now - started) / 60:.0f}m] {done} investigations, {rss}")
        except KeyboardInterrupt:
            print("\nInterrupted; winding down.")

        stop.set()
        for thread in threads:
            thread.join(timeout=120)
        elapsed = time.time() - started

        runs: list[Run] = []
        while not results.empty():
            runs.append(results.get())

        _, with_result_after, blobs_after = (
            _try(aged_state, default=(0, 0, 0)) if seeded_at else (0, 0, 0)
        )
        retention["with_result_after"] = with_result_after
        retention["blobs_after"] = blobs_after

        # Whether the sweep fired is watched for above, on the platform's own
        # timer. Its *cost* needs rows to delete, and by now the platform has
        # probably taken them — so seed a second, larger batch and time the
        # store's own prune against it.
        second = _try(age_reports, 25, default=0)
        if second:
            removed, seconds = _try(measure_sweep_cost, default=(0, 0.0))
            retention.update({"cost_rows": second, "cost_blobs": removed, "cost_seconds": seconds})

        certificates: dict = {"enabled": bool(agent)}
        if agent:
            serials = _try(certificate_serials, args.context, default=[])
            certificates.update(
                {
                    "cluster": args.context,
                    "serials_start": serials_at_start,
                    "serials_end": len(serials),
                    "stream_restarts": _agent_restarts(workdir / "agent.log"),
                    "after_last_renewal": _served_after_last_renewal(runs, workdir, args.context),
                }
            )

        state.update(
            {
                "runs": runs,
                "samples": samples,
                "started": started,
                "elapsed": elapsed,
                "worker_names": [w.name for w in workers],
                "findings": log_findings(workers),
                "certificates": certificates,
                "interruption": interruption,
            }
        )

        code = summarise(state)

        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    {
                        "elapsed": elapsed,
                        "runs": [run.__dict__ for run in runs],
                        "samples": [
                            {
                                "at": s.at,
                                "rss": s.rss,
                                "threads": s.threads,
                                "files": s.files,
                                "db_bytes": s.db_bytes,
                                "redis_bytes": s.redis_bytes,
                                "rows": s.rows,
                            }
                            for s in samples
                        ],
                        "retention": retention,
                        "certificates": certificates,
                    },
                    indent=2,
                    default=str,
                )
            )
            print(f"Raw series written to {args.json}")
        return code

    finally:
        if agent is not None:
            agent.send_signal(signal.SIGTERM)
            try:
                agent.wait(timeout=20)
            except subprocess.TimeoutExpired:
                agent.kill()
        for worker in workers:
            worker.process.send_signal(signal.SIGTERM)
        for worker in workers:
            try:
                worker.process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                worker.process.kill()
        if granted:
            revoke_caller_rbac(args.context)


def measure_sweep_cost() -> tuple[int, float]:
    """Time the real `prune()` against whatever aged rows are present.

    Deliberately the store's own method in a subprocess rather than the SQL
    re-typed here: the cost being measured is the statement the platform
    actually runs, and a second copy of it in a benchmark would be free to
    drift into measuring something the platform never does.

    This is a separate observation from *whether the in-process sweep fired* —
    that one is watched for during the run, on the platform's own timer.
    """
    # Built the way `app/state.py` builds it. `get_report_store()` alone returns
    # the *filesystem* store — installing the Postgres one is a startup step,
    # not something DATABASE_URL implies — so asking it to prune reported
    # "0 blobs in 0 ms", a plausible-looking number for a store that was never
    # pointed at the rows.
    code = (
        "import time, json;"
        "from app.persistence.postgres import Database;"
        "from app.services.report_store import PostgresReportStore;"
        "from app.core.config import settings;"
        "s = PostgresReportStore(Database(settings.database_url));"
        "t = time.perf_counter();"
        "n = s.prune(1);"
        "print(json.dumps({'removed': n, 'seconds': time.perf_counter() - t}))"
    )
    out = subprocess.run(
        [str(PYTHON), "-c", code],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "DATABASE_URL": DATABASE_URL, "REDIS_URL": REDIS_URL},
    )
    for line in reversed(out.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return int(payload["removed"]), float(payload["seconds"])
    return 0, 0.0


def _agent_restarts(log: Path) -> int:
    try:
        text = log.read_text(errors="replace")
        # One "connected" per dial. More than one means the stream dropped and
        # was re-established, which is exactly what renewal must not cause.
        return max(0, text.count('"connected"') + text.count("msg=connected") - 1)
    except OSError:
        return 0


def _served_after_last_renewal(runs: list[Run], workdir: Path, cluster: str) -> int:
    """Investigations that reached the cluster through the agent after the last
    certificate was issued.

    This is the check that makes renewal *mean* something. A count of issued
    certificates only proves the agent asked; what the overlap window promises
    is that collection keeps working across the swap, so the number that
    matters is how many reads the agent served afterwards.
    """
    serials = sql("SELECT max(issued_at) FROM agent_certificates WHERE cluster_id = %s", (cluster,))
    if not serials or serials[0][0] is None:
        return 0
    last = serials[0][0]
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    cutoff = last.timestamp()
    return sum(1 for run in runs if run.provider == "agent" and run.finished_at > cutoff)


if __name__ == "__main__":
    sys.exit(main())
