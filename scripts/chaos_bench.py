#!/usr/bin/env python3
"""Chaos verification for the distributed deployment.

Tier-5 items 37, 38 and 39. Each asserts a claim `CLAUDE.md` already makes and
that no test had exercised against real infrastructure:

    worker-death   A worker killed mid-investigation leaves a job that another
                   worker's reaper finishes. `kill -9`, not a clean shutdown —
                   a graceful stop takes the drain path and proves nothing
                   about lease expiry.

    redis-loss     "If Redis drops everything the system is slower, never
                   wrong." Every message has a committed row behind it, so
                   losing Redis must not lose or corrupt an investigation.

    postgres-loss  Postgres is the truth. Losing it must fail loudly and
                   visibly — including on the readiness probe — rather than
                   quietly succeeding with nothing written.

Real uvicorn workers, real Postgres, real Redis, real `docker compose stop`.
Nothing here is mocked; that is the entire point.

    docker compose up -d postgres redis
    python scripts/chaos_bench.py all

Investigations are made slow enough to interrupt by pointing the workers at an
**unroutable** cluster address, so collection blocks until `KUBECTL_TIMEOUT_SECONDS`.
That is a realistic condition (a cluster that has gone away) and needs no
test-only branch in the product.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
PYTHON = BACKEND / ".venv" / "bin" / "python"

DATABASE_URL = os.environ.get(
    "CHAOS_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/k8sagent_chaos"
)
REDIS_URL = os.environ.get("CHAOS_REDIS_URL", "redis://127.0.0.1:6379/7")
TOKEN = "chaos-token"

# Investigations have to be slow enough to interrupt, and the obvious ways are
# not. An unroutable address (10.255.255.1) looked right and was measured
# wrong: kubectl returned `error: EOF` in milliseconds, so every investigation
# completed before a signal could reach it and the drain scenario passed
# without ever draining anything.
#
# A listener that *accepts* the connection and then never speaks is what
# actually blocks: the TLS handshake waits, kubectl waits with it, and
# `KUBECTL_TIMEOUT_SECONDS` becomes the real bound. Measured hanging past two
# minutes unbounded.
STALL_PORT = 18443
STALL_KUBECONFIG = f"""\
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: "https://127.0.0.1:{STALL_PORT}"
    insecure-skip-tls-verify: true
  name: stall
contexts:
- context: {{cluster: stall, user: nobody}}
  name: stall
current-context: stall
users:
- name: nobody
  # A token, and it is load-bearing. With no credential kubectl prompts
  # ("Please enter Username:"), gets EOF from a closed stdin and fails in
  # milliseconds — which is how the drain scenario came to pass without ever
  # draining. With one, kubectl proceeds to the TLS handshake and blocks.
  user: {{token: chaos-placeholder}}
"""


def start_stalling_cluster() -> socket.socket:
    """Accept connections and never answer, so kubectl blocks."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", STALL_PORT))
    listener.listen(128)

    held: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            # Held rather than closed: closing would give kubectl an EOF, which
            # is the fast failure this exists to avoid.
            held.append(connection)

    threading.Thread(target=accept_forever, daemon=True).start()
    return listener


KUBECTL_TIMEOUT = 25
LEASE_SECONDS = 10
REAP_PATIENCE = 90


@dataclass
class Worker:
    name: str
    port: int
    process: subprocess.Popen
    log: Path

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def kill(self) -> None:
        """SIGKILL. A worker that dies does not get to tidy up."""
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=30)

    def terminate(self) -> None:
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    observations: list[str] = field(default_factory=list)


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
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"detail": payload}
    except Exception as exc:
        return 0, {"detail": str(exc)}


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=REPO, check=True, capture_output=True)


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


def start_worker(name: str, port: int, kubeconfig: Path, workdir: Path, **extra) -> Worker:
    environment = {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "REDIS_URL": REDIS_URL,
        "REDIS_KEY_PREFIX": "chaos",
        "AUTH_MODE": "token",
        "API_TOKENS": f"{TOKEN}:chaos@example.com",
        "KUBECONFIG": str(kubeconfig),
        "KUBECTL_TIMEOUT_SECONDS": str(KUBECTL_TIMEOUT),
        "JOB_LEASE_SECONDS": str(LEASE_SECONDS),
        "JOB_MAX_CONCURRENT": "4",
        "WORKER_ID": name,
        "REPORT_RETENTION_DAYS": "0",
        "METRICS_ENABLED": "true",
        **{key: str(value) for key, value in extra.items()},
    }
    log = workdir / f"{name}.log"
    handle = log.open("w")
    process = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=BACKEND,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return Worker(name=name, port=port, process=process, log=log)


def await_ready(worker: Worker, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = request(f"{worker.base}/health/ready", timeout=5)
        if status == 200:
            return True
        if worker.process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def submit(worker: Worker) -> str | None:
    status, body = request(f"{worker.base}/investigations", "POST", {"namespace": "default"})
    return body.get("id") if status in (200, 202) else None


def status_of(worker: Worker, job_id: str) -> str:
    code, body = request(f"{worker.base}/investigations/{job_id}/status", timeout=15)
    if code != 200:
        return f"http-{code}"
    return body.get("status", "unknown")


def await_status(worker: Worker, job_id: str, wanted: set[str], timeout: float) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = status_of(worker, job_id)
        if last in wanted:
            return last
        time.sleep(1.0)
    return last


# --- scenarios ---------------------------------------------------------------


def scenario_worker_death(workdir: Path, kubeconfig: Path) -> Result:
    """Item 37. Kill the worker holding a claimed job; another must finish it."""
    observations = []
    alpha = start_worker("chaos-a", 8801, kubeconfig, workdir)
    beta = start_worker("chaos-b", 8802, kubeconfig, workdir)
    try:
        if not (await_ready(alpha) and await_ready(beta)):
            return Result("worker-death", False, "workers did not become ready")

        # Submitted to alpha and claimed by whichever consumer wins. Kill both
        # candidates' worst case by killing alpha and letting beta reap.
        job_id = submit(alpha)
        if not job_id:
            return Result("worker-death", False, "submission was refused")
        observations.append(f"submitted {job_id}")

        running = await_status(alpha, job_id, {"running"}, timeout=30)
        if running != "running":
            return Result(
                "worker-death",
                False,
                f"job never reached running (last: {running})",
                observations,
            )
        observations.append("job is running")

        holder = _lease_holder(job_id)
        observations.append(f"lease held by {holder}")
        victim = alpha if holder == "chaos-a" else beta
        survivor = beta if victim is alpha else alpha

        killed_at = time.time()
        victim.kill()
        observations.append(f"SIGKILLed {victim.name} while it held the lease")

        final = await_status(survivor, job_id, {"failed", "succeeded"}, timeout=REAP_PATIENCE)
        elapsed = time.time() - killed_at
        observations.append(f"terminal state {final!r} after {elapsed:.1f}s")

        if final != "failed":
            return Result(
                "worker-death",
                False,
                f"expected the reaper to fail the job, got {final!r}",
                observations,
            )

        _, body = request(f"{survivor.base}/investigations/{job_id}", timeout=20)
        error = (body or {}).get("error", "")
        observations.append(f"error: {error!r}")
        if "worker" not in error.lower():
            return Result(
                "worker-death",
                False,
                "job failed but the error does not name a lost worker",
                observations,
            )

        return Result(
            "worker-death",
            True,
            f"reaped by {survivor.name} in {elapsed:.1f}s (lease {LEASE_SECONDS}s)",
            observations,
        )
    finally:
        for worker in (alpha, beta):
            if worker.process.poll() is None:
                worker.terminate()


def _lease_holder(job_id: str) -> str:
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT lease_worker FROM investigations WHERE id = %s", (job_id,))
        row = cursor.fetchone()
        return (row[0] if row else "") or ""


def scenario_redis_loss(workdir: Path, kubeconfig: Path) -> Result:
    """Item 38. Redis is the latency layer; losing it must not lose an investigation."""
    observations = []
    worker = start_worker("chaos-r", 8803, kubeconfig, workdir)
    try:
        if not await_ready(worker):
            return Result("redis-loss", False, "worker did not become ready")

        # A control, because the workers point at an unroutable cluster and
        # every investigation therefore fails on collection. Without this the
        # scenario cannot tell "Redis loss broke it" from "it was always going
        # to fail", and would have reported a pass either way.
        control_id = submit(worker)
        control = await_status(worker, control_id, {"succeeded", "failed"}, timeout=120)
        _, control_body = request(f"{worker.base}/investigations/{control_id}", timeout=20)
        control_error = (control_body or {}).get("error", "")
        observations.append(f"control (redis up) reached {control!r}: {control_error[:60]!r}")

        job_id = submit(worker)
        if not job_id:
            return Result("redis-loss", False, "submission was refused")
        if await_status(worker, job_id, {"running"}, timeout=30) != "running":
            return Result("redis-loss", False, "job never started", observations)
        observations.append(f"{job_id} running")

        compose("stop", "redis")
        observations.append("redis stopped mid-investigation")

        # The committed row is the truth, and reading it does not need Redis.
        during = status_of(worker, job_id)
        observations.append(f"status readable with redis down: {during!r}")

        ready_code, ready_body = request(f"{worker.base}/health/ready", timeout=10)
        observations.append(f"readiness during outage: {ready_code} {ready_body.get('checks')}")

        final = await_status(worker, job_id, {"succeeded", "failed"}, timeout=120)
        _, final_body = request(f"{worker.base}/investigations/{job_id}", timeout=20)
        final_error = (final_body or {}).get("error", "")
        observations.append(f"with redis down reached {final!r}: {final_error[:60]!r}")

        compose("start", "redis")
        time.sleep(6)
        observations.append("redis restarted")

        recovered = await_ready(worker, timeout=60)
        observations.append(f"readiness recovered: {recovered}")

        after = submit(worker)
        observations.append(f"post-recovery submission: {after is not None}")

        failures = []
        if final != control:
            failures.append(
                f"Redis loss changed the outcome: control {control!r}, during outage {final!r}"
            )
        if final_error != control_error:
            failures.append("Redis loss changed the recorded error")
        if during.startswith("http-"):
            failures.append("committed state was unreadable without redis")
        if ready_code != 200:
            failures.append(
                f"readiness was {ready_code} during a Redis outage. Every worker shares "
                f"one Redis, so this takes the whole fleet out while every read still "
                f"works — a degradation presenting as an outage"
            )
        if not recovered or after is None:
            failures.append("worker did not recover once redis returned")

        if failures:
            return Result("redis-loss", False, "; ".join(failures), observations)

        return Result(
            "redis-loss",
            True,
            f"outcome identical to the control ({final!r}); worker stayed in rotation",
            observations,
        )
    finally:
        compose("start", "redis")
        if worker.process.poll() is None:
            worker.terminate()


def scenario_postgres_loss(workdir: Path, kubeconfig: Path) -> Result:
    """Item 39. Postgres is the truth. Losing it must fail visibly, not silently."""
    observations = []
    worker = start_worker("chaos-p", 8804, kubeconfig, workdir)
    try:
        if not await_ready(worker):
            return Result("postgres-loss", False, "worker did not become ready")

        job_id = submit(worker)
        if not job_id:
            return Result("postgres-loss", False, "submission was refused")
        if await_status(worker, job_id, {"running"}, timeout=30) != "running":
            return Result("postgres-loss", False, "job never started", observations)
        observations.append(f"{job_id} running")

        compose("stop", "postgres")
        observations.append("postgres stopped mid-investigation")

        ready_code, ready_body = request(f"{worker.base}/health/ready", timeout=15)
        observations.append(f"readiness during outage: {ready_code} {ready_body.get('checks')}")

        live_code, _ = request(f"{worker.base}/health/live", timeout=10)
        observations.append(f"liveness during outage: {live_code}")

        submitted, _ = request(
            f"{worker.base}/investigations",
            "POST",
            {"namespace": "default"},
            timeout=20,
        )
        observations.append(f"submission during outage: {submitted}")

        compose("start", "postgres")
        time.sleep(8)
        observations.append("postgres restarted")

        recovered = await_ready(worker, timeout=90)
        observations.append(f"readiness recovered: {recovered}")

        after = submit(worker)
        observations.append(f"post-recovery submission: {after is not None}")

        failures = []
        if ready_code != 503:
            failures.append(f"readiness should be 503 during a Postgres outage, got {ready_code}")
        if live_code != 200:
            failures.append(
                f"liveness must stay 200 — restarting every worker on a database "
                f"blip turns a recoverable failure into an outage (got {live_code})"
            )
        if submitted in (200, 202):
            failures.append(
                "a submission during the outage reported success; the durable row "
                "is what makes an accepted id meaningful"
            )
        if not recovered or after is None:
            failures.append("worker did not recover once Postgres returned")

        if failures:
            return Result("postgres-loss", False, "; ".join(failures), observations)

        return Result(
            "postgres-loss",
            True,
            "readiness 503, liveness 200, submissions refused, full recovery",
            observations,
        )
    finally:
        compose("start", "postgres")
        if worker.process.poll() is None:
            worker.terminate()


def scenario_graceful_drain(workdir: Path, kubeconfig: Path) -> Result:
    """Item 43, live. SIGTERM must finish in-flight work rather than abandon it."""
    observations = []
    # The drain window must exceed the collection time, or the deadline expires
    # and the investigation is cancelled — which is the behaviour this scenario
    # exists to distinguish from.
    worker = start_worker(
        "chaos-d",
        8805,
        kubeconfig,
        workdir,
        SHUTDOWN_DRAIN_SECONDS=120,
        KUBECTL_TIMEOUT_SECONDS=20,
    )
    try:
        if not await_ready(worker):
            return Result("graceful-drain", False, "worker did not become ready")

        job_id = submit(worker)
        if not job_id:
            return Result("graceful-drain", False, "submission was refused")
        if await_status(worker, job_id, {"running"}, timeout=30) != "running":
            return Result("graceful-drain", False, "job never started", observations)
        observations.append(f"{job_id} running")

        started = time.time()
        worker.process.send_signal(signal.SIGTERM)
        observations.append("SIGTERM sent while the investigation was running")
        worker.process.wait(timeout=240)
        shutdown_seconds = time.time() - started
        observations.append(f"process exited after {shutdown_seconds:.1f}s")

        log = worker.log.read_text()
        drained_cleanly = "Drained" in log and "cleanly" in log
        readiness_first = "readiness is now false" in log
        observations.append(f"drain logged: {drained_cleanly}")
        observations.append(f"readiness dropped before teardown: {readiness_first}")

        # The row is the evidence, and it outlives the process.
        final, error = _outcome_from_database(job_id)
        observations.append(f"committed status after shutdown: {final!r} / {error[:60]!r}")

        # The workers point at an unroutable cluster, so a *drained*
        # investigation still ends `failed` — on its own collection error.
        # What distinguishes drained from abandoned is therefore the error,
        # not the status: WORKER_LOST means the drain gave up on it.
        abandoned = "worker stopped" in error.lower()

        failures = []
        if not readiness_first:
            failures.append("readiness did not go false during shutdown")
        if abandoned:
            failures.append(f"in-flight investigation was abandoned rather than drained: {error!r}")
        if final not in {"succeeded", "failed"}:
            failures.append(f"investigation left in a non-terminal state ({final!r})")

        # Without these two the scenario passes vacuously: an investigation that
        # had already finished before SIGTERM arrived satisfies every assertion
        # above while proving nothing about draining. The first run did exactly
        # that — 0.2s to exit and no drain in the log.
        if not drained_cleanly:
            failures.append(
                "no drain was logged, so nothing was in flight when SIGTERM arrived "
                "and this run proves nothing"
            )
        if shutdown_seconds < 1.0:
            failures.append(
                f"shutdown took {shutdown_seconds:.1f}s — too fast to have waited for anything"
            )

        if failures:
            return Result("graceful-drain", False, "; ".join(failures), observations)

        return Result(
            "graceful-drain",
            True,
            f"in-flight investigation ran to its own conclusion ({final!r}) during SIGTERM",
            observations,
        )
    finally:
        if worker.process.poll() is None:
            worker.terminate()


def _outcome_from_database(job_id: str) -> tuple[str, str]:
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status, error FROM investigations WHERE id = %s", (job_id,))
        row = cursor.fetchone()
        if not row:
            return "missing", ""
        return (row[0] or "missing"), (row[1] or "")


SCENARIOS = {
    "worker-death": scenario_worker_death,
    "redis-loss": scenario_redis_loss,
    "postgres-loss": scenario_postgres_loss,
    "graceful-drain": scenario_graceful_drain,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    args = parser.parse_args()

    prepare_database()
    chosen = list(SCENARIOS) if args.scenario == "all" else [args.scenario]

    with tempfile.TemporaryDirectory(prefix="chaos-") as raw:
        workdir = Path(raw)
        kubeconfig = workdir / "kubeconfig"
        kubeconfig.write_text(STALL_KUBECONFIG)
        listener = start_stalling_cluster()

        results = []
        for name in chosen:
            print(f"\n{'=' * 70}\n{name}\n{'=' * 70}", flush=True)
            try:
                result = SCENARIOS[name](workdir, kubeconfig)
            except Exception as exc:
                result = Result(name, False, f"harness error: {exc!r}")
            results.append(result)
            for line in result.observations:
                print(f"  · {line}", flush=True)
            print(f"  {'PASS' if result.passed else 'FAIL'}: {result.detail}", flush=True)

        print(f"\n{'=' * 70}")
        for result in results:
            print(f"  {'PASS' if result.passed else 'FAIL'}  {result.name}: {result.detail}")
        failed = [result for result in results if not result.passed]
        print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
        listener.close()
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
