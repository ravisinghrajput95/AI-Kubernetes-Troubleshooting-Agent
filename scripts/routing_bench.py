"""Measure agent routing at fleet scale.

M8a's claim is that an investigation of an agent-connected cluster reaches the
worker holding that agent's stream. That claim has a number attached, and this
prints it rather than asserting it:

    routing hit rate — the fraction of submissions queued on the one worker
                       that can actually collect them.

Before M8a the answer was 1/N by construction, because every job went to the
shared queue and any worker claimed it. On three replicas that is 33%: two out
of three agent-cluster investigations were answered by the platform's own
kubeconfig instead of by the cluster. The rest of this script exists to show
what it is now, and what routing costs on the submit path to get there.

    docker compose up -d postgres redis
    python scripts/routing_bench.py --clusters 1000 --workers 3 --submissions 2000

It needs Postgres and Redis, and nothing else — no gRPC, no agents, no
kubeconfig. Routing reads the presence index and writes to a queue, so a
faithful measurement needs exactly those two and real ones. Standing up a
thousand real agent streams is M8c's problem and measures a different thing.

The database is only touched to create job rows; pass `--no-db` to measure the
routing decision and the queue alone, which is the part M8a changed.
"""

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/k8sagent_test"
DEFAULT_REDIS_URL = "redis://localhost:6379/1"


def percentiles(samples: list[float]) -> dict[str, float]:
    """p50/p95/p99 in milliseconds.

    Percentiles rather than a mean: the submit path's cost is a Redis round
    trip, and a mean hides the tail that an operator actually notices.
    """
    if not samples:
        return {}
    ordered = sorted(samples)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return round(ordered[index] * 1000, 3)

    return {
        "p50_ms": at(0.50),
        "p95_ms": at(0.95),
        "p99_ms": at(0.99),
        "max_ms": round(max(ordered) * 1000, 3),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clusters", type=int, default=1000, help="Agents in the fleet.")
    parser.add_argument("--workers", type=int, default=3, help="Platform replicas.")
    parser.add_argument("--submissions", type=int, default=2000, help="Investigations submitted.")
    parser.add_argument("--tenant", default="default")
    parser.add_argument(
        "--kubeconfig-share",
        type=float,
        default=0.0,
        help="Fraction of submissions naming a cluster with no agent (0.0-1.0).",
    )
    parser.add_argument("--no-db", action="store_true", help="Skip creating job rows.")
    parser.add_argument("--seed", type=int, default=1729, help="Submissions are seeded.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    arguments = parser.parse_args(argv)

    redis_url = os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL
    database_url = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL

    from app.gateway.presence import AgentPresence
    from app.persistence.redis_bus import RedisBus

    # A prefix of its own, so a bench run cannot be mistaken for real work by a
    # consumer that happens to be pointed at the same Redis.
    bus = RedisBus(redis_url, prefix=f"bench-{os.getpid()}")
    workers = [f"worker-{index}" for index in range(arguments.workers)]

    # --- populate the fleet -------------------------------------------------
    #
    # Each cluster's agent is attached to exactly one worker, which is what a
    # real fleet looks like: agents dial out and land wherever the load
    # balancer sent them.
    class Session:
        def __init__(self, cluster_id: str, tenant: str) -> None:
            self.cluster_id, self.tenant = cluster_id, tenant

        def describe(self) -> dict:
            return {"cluster_id": self.cluster_id, "tenant": self.tenant, "online": True}

    holder_of: dict[str, str] = {}
    announce_started = time.perf_counter()
    for index in range(arguments.clusters):
        cluster = f"cluster-{index:05d}"
        worker = workers[index % len(workers)]
        holder_of[cluster] = worker
        AgentPresence(bus, worker).announce(Session(cluster, arguments.tenant))
    announce_seconds = time.perf_counter() - announce_started

    # --- submit -------------------------------------------------------------
    from app.gateway import presence as presence_module
    from app.tenancy import tenant_scope

    # Submissions arrive on whichever replica the load balancer picked, which
    # is the situation routing exists to correct: the receiving worker is
    # usually not the one holding the stream.
    presence_module.set_agent_presence(AgentPresence(bus, workers[0]))

    store = None
    if not arguments.no_db:
        from app.jobs.distributed import PostgresRedisJobStore
        from app.persistence.postgres import Database

        database = Database(database_url, min_size=1, max_size=4)
        database.migrate()
        store = PostgresRedisJobStore(database, bus)

    from app.core.config import settings

    settings.agent_gateway_port = settings.agent_gateway_port or 9443

    from app.jobs.runner import agent_affinity
    from app.models.investigation import InvestigationRequest

    rng = random.Random(arguments.seed)
    lookups: list[float] = []
    routed = 0
    unrouted = 0
    misrouted = 0
    kubeconfig = 0

    submit_started = time.perf_counter()
    with tenant_scope(arguments.tenant):
        for _ in range(arguments.submissions):
            if rng.random() < arguments.kubeconfig_share:
                context = f"kubeconfig-{rng.randrange(1000)}"
            else:
                context = f"cluster-{rng.randrange(arguments.clusters):05d}"

            started = time.perf_counter()
            affinity = agent_affinity(InvestigationRequest(context=context))
            lookups.append(time.perf_counter() - started)

            expected = holder_of.get(context, "")
            if not expected:
                kubeconfig += 1
                # Correct: nothing holds it, so the shared queue is the answer.
                if affinity:
                    misrouted += 1
            elif affinity == expected:
                routed += 1
            elif affinity:
                misrouted += 1
            else:
                unrouted += 1

            if store is not None:
                job = store.create({"context": context})
                store.enqueue(job.id, affinity)
            else:
                bus.enqueue("bench", affinity)
    submit_seconds = time.perf_counter() - submit_started

    agent_submissions = arguments.submissions - kubeconfig
    hit_rate = routed / agent_submissions if agent_submissions else 1.0
    baseline = 1 / len(workers)

    depths = {worker: bus.queue_depth(worker) for worker in workers}
    depths["(shared)"] = bus.queue_depth()

    report = {
        "fleet": {
            "clusters": arguments.clusters,
            "workers": arguments.workers,
            "submissions": arguments.submissions,
            "agent_submissions": agent_submissions,
            "kubeconfig_submissions": kubeconfig,
        },
        "routing": {
            "hit_rate": round(hit_rate, 4),
            "baseline_before_m8a": round(baseline, 4),
            "routed": routed,
            "unrouted": unrouted,
            "misrouted": misrouted,
        },
        "submit_path": {
            "affinity_lookup": percentiles(lookups),
            "submissions_per_second": round(arguments.submissions / submit_seconds, 1),
            "wrote_job_rows": store is not None,
        },
        "presence": {
            "announce_seconds": round(announce_seconds, 2),
            "announces_per_second": round(arguments.clusters / announce_seconds, 1),
        },
        "queue_depth": depths,
    }

    # Leave nothing behind. A bench run that pollutes the keyspace makes the
    # next one meaningless.
    for worker in workers:
        bus.delete(bus.worker_queue_key(worker))
    bus.delete(bus.queue_key)
    for cluster in holder_of:
        bus.delete(f"{bus.prefix}:agents:{arguments.tenant}:{cluster}")

    if arguments.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"\nFleet: {arguments.clusters} clusters across {arguments.workers} workers")
    print(f"Submitted: {arguments.submissions} ({agent_submissions} to agent clusters)\n")
    print(f"  routing hit rate      {hit_rate:>8.1%}   (before M8a: {baseline:.1%}, i.e. 1/N)")
    print(f"  routed                {routed:>8}")
    print(f"  unrouted              {unrouted:>8}   went to the shared queue")
    print(f"  misrouted             {misrouted:>8}   sent to a worker that cannot collect")
    lookup = report["submit_path"]["affinity_lookup"]
    print(
        f"\n  affinity lookup       p50 {lookup['p50_ms']}ms  "
        f"p95 {lookup['p95_ms']}ms  p99 {lookup['p99_ms']}ms"
    )
    print(f"  submissions/sec       {report['submit_path']['submissions_per_second']}")
    print(f"  presence announces/s  {report['presence']['announces_per_second']}\n")

    if misrouted:
        print("MISROUTED SUBMISSIONS PRESENT — routing sent work to a worker that cannot do it.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
