"""Hold a synthetic fleet against a running gateway, and measure what it costs.

M8c's exit criterion is a *documented performance envelope*, and §12 scores
scalability "8 not 9 until load-tested at 1,000". This is the harness that
makes that measurable: N synthetic agents, each holding a real `Connect` stream
to a real gateway, each answering real `CollectionRequest`s with real evidence
records over the real wire contract.

    # one terminal
    cd backend && AGENT_GATEWAY_PORT=19700 AGENT_GATEWAY_TLS=disabled \
        AUTH_MODE=disabled ALLOW_INSECURE_NO_AUTH=true \
        .venv/bin/python -m uvicorn app.main:app --port 8700

    # another
    python scripts/fleet_bench.py --clusters 1000 --gateway localhost:19700

**What this measures and what it does not.** It measures the *platform* side:
how many streams a gateway holds, what they cost it, how long a fleet takes to
attach, and how quickly the platform can drive collection across it. It does
not measure the Go agent, a real API server, or real network conditions — every
agent here is a coroutine in one process answering from a canned payload. That
is the honest scope: the platform's envelope, not the fleet's.

Plaintext (`AGENT_GATEWAY_TLS=disabled`) because a load harness has no business
minting a thousand certificates, and because the gateway's own docs already
name that mode as the local-development path. mTLS changes the handshake cost,
not the steady-state cost, and the envelope should say so rather than pretend
the two were measured together.
"""

import argparse
import asyncio
import contextlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# One usable evidence record per spec, sized like a small real one. The point
# of the harness is stream and dispatch cost, not payload size — that is what
# `payload_bench.py` measures, and conflating them would make both unreadable.
PAYLOAD = json.dumps({"items": [{"metadata": {"name": f"pod-{index}"}} for index in range(20)]})


def peak_rss_mb() -> float:
    """Peak resident memory of this process.

    macOS reports bytes, Linux kilobytes. Getting this wrong by 1024x in a
    published envelope would be worse than not publishing one.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class SyntheticAgent:
    """One cluster's agent: a stream, a hello, and answers.

    Deliberately not a subclass or a mock of anything in `agent/` — it speaks
    the published protobuf contract and nothing else, so a change that breaks
    real agents breaks this too rather than being papered over by a shared
    helper.
    """

    def __init__(self, cluster_id: str, channel, kinds: list[str]) -> None:
        self.cluster_id = cluster_id
        self._channel = channel
        self._kinds = kinds
        self._outbound: asyncio.Queue = asyncio.Queue()
        self.collections = 0
        self.records = 0
        self.attached = asyncio.Event()
        self.failure: BaseException | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._guarded())

    async def _guarded(self) -> None:
        """Record why a stream died instead of losing it.

        Nothing awaits these tasks, so an exception here is discarded by the
        event loop. The first run of this harness reported "5 collections, 0
        records" — which looks like a platform finding and was an
        `AttributeError` on an enum name. A benchmark that fails silently
        publishes confident nonsense.
        """
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
            self.attached.set()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _messages(self):
        from app.wire.gen.agent.v1 import agent_pb2

        yield agent_pb2.AgentMessage(
            hello=agent_pb2.AgentHello(
                cluster_id=self.cluster_id,
                agent_version="fleet-bench",
                kubernetes_version="v1.31.0",
                supported_kinds=self._kinds,
            )
        )
        while True:
            yield await self._outbound.get()

    async def _run(self) -> None:
        from app.wire.gen.agent.v1 import agent_pb2, agent_pb2_grpc

        stub = agent_pb2_grpc.AgentGatewayStub(self._channel)
        stream = stub.Connect(self._messages())
        self.attached.set()

        async for message in stream:
            kind = message.WhichOneof("payload")
            if kind == "heartbeat":
                # Presence is heartbeat-derived, not socket-derived: an agent
                # that stops answering must go stale even with its stream open.
                await self._outbound.put(
                    agent_pb2.AgentMessage(health=agent_pb2.AgentHealth(active_collections=0))
                )
            elif kind == "collect":
                await self._answer(message.collect)

    async def _answer(self, request) -> None:
        from app.wire.gen.agent.v1 import agent_pb2, collection_pb2, evidence_pb2

        self.collections += 1
        for spec in request.specs:
            record = evidence_pb2.EvidenceRecord(
                id=f"{spec.kind}:{self.cluster_id}",
                kind=spec.kind,
                source=evidence_pb2.EVIDENCE_SOURCE_KUBECTL,
                status=evidence_pb2.EVIDENCE_STATUS_OK,
                payload=PAYLOAD.encode(),
            )
            record.target.CopyFrom(spec.target)
            await self._outbound.put(
                agent_pb2.AgentMessage(
                    evidence=agent_pb2.EvidenceEnvelope(
                        investigation_id=request.investigation_id,
                        request_id=request.request_id,
                        record=record,
                    )
                )
            )
            self.records += 1

        await self._outbound.put(
            agent_pb2.AgentMessage(
                done=collection_pb2.CollectionDone(
                    investigation_id=request.investigation_id,
                    request_id=request.request_id,
                    records_emitted=len(request.specs),
                    specs_requested=len(request.specs),
                )
            )
        )


def _hold_agents(gateway: str, first: int, count: int, kinds: list[str], batch: int) -> None:
    """Attach `count` agents in this process and hold them until terminated.

    One process per slice of the fleet. A load generator that is multi-process
    on the submitting side and single-process on the answering side still
    measures itself, and this repository has published a table row that assumed
    this existed while the flag was silently ignored.
    """
    import grpc

    async def run() -> None:
        agents = []
        for index in range(first, first + count):
            agent = SyntheticAgent(f"bench-{index:05d}", grpc.aio.insecure_channel(gateway), kinds)
            agents.append(agent)
            await agent.start()
            if batch and (index - first + 1) % batch == 0:
                await asyncio.sleep(0.05)
        await asyncio.gather(*(agent.attached.wait() for agent in agents))
        while True:  # held open: the agents are the counterparty being measured
            await asyncio.sleep(3600)

    with contextlib.suppress(BaseException):
        asyncio.run(run())


async def attach_distributed_fleet(arguments, kinds: list[str], api: str):
    """Spread the fleet across processes; wait on the *platform's* view of it.

    A child cannot report on the thing being measured, and the parent has no
    other way to know the fleet is genuinely up.
    """
    import multiprocessing

    import httpx

    context = multiprocessing.get_context("spawn")
    per_process = max(1, arguments.clusters // arguments.agent_processes)
    started = time.perf_counter()
    workers = [
        context.Process(
            target=_hold_agents,
            args=(arguments.gateway, index * per_process, per_process, kinds, arguments.batch),
            daemon=True,
        )
        for index in range(arguments.agent_processes)
    ]
    for worker in workers:
        worker.start()

    expected = per_process * arguments.agent_processes
    deadline = time.perf_counter() + 120
    seen = 0
    async with httpx.AsyncClient(base_url=api, timeout=30.0) as probe:
        while time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            try:
                seen = len((await probe.get("/agents")).json().get("items", []))
            except Exception:
                continue
            if seen >= expected:
                break

    names = [f"bench-{index:05d}" for index in range(expected)]
    return workers, names, seen, time.perf_counter() - started


async def attach_fleet(gateway: str, clusters: int, kinds: list[str], batch: int):
    """Bring `clusters` agents up, in batches, and report how long it took."""
    import grpc

    # One channel per agent, because that is what a real fleet is: a thousand
    # separate processes in a thousand clusters. Multiplexing them onto one
    # HTTP/2 connection would measure something nobody deploys.
    channels = []
    agents: list[SyntheticAgent] = []

    started = time.perf_counter()
    for index in range(clusters):
        channel = grpc.aio.insecure_channel(gateway)
        channels.append(channel)
        agent = SyntheticAgent(f"bench-{index:05d}", channel, kinds)
        agents.append(agent)
        await agent.start()
        if batch and (index + 1) % batch == 0:
            # Let the gateway breathe: a thousand simultaneous handshakes
            # measures the accept backlog rather than the steady state.
            await asyncio.sleep(0.05)

    await asyncio.gather(*(agent.attached.wait() for agent in agents))
    return agents, channels, time.perf_counter() - started


async def platform_counters(client) -> dict[str, float]:
    """The platform's own view, so a client-side number can be checked.

    Added after the first published envelope reported a throughput ceiling that
    was this harness saturating rather than the platform. Two offered-load
    levels could not tell the difference — both were bounded by the same single
    asyncio process — and only the server's own counters can.
    """
    import re

    try:
        payload = (await client.get("/metrics")).text
    except Exception:
        return {}

    counters: dict[str, float] = {}
    for phase in ("collect", "analyse", "report", "persist"):
        found = re.search(
            rf'k8sagent_investigation_phase_seconds_sum\{{phase="{phase}"\}} ([0-9.e+-]+)', payload
        )
        counters[phase] = float(found.group(1)) if found else 0.0
    finished = re.findall(
        r"k8sagent_investigations_total\{outcome=\"[a-z_]+\"\} ([0-9.e+-]+)", payload
    )
    counters["finished"] = sum(float(value) for value in finished)
    return counters


async def main_async(arguments) -> int:
    import httpx

    if arguments.kinds:
        kinds = [kind.strip() for kind in arguments.kinds.split(",") if kind.strip()]
    else:
        # The platform's own translation table, so a harness agent supports
        # exactly what a real one is ever asked for. Hardcoding a list here
        # would drift the moment a collector is added, and would show up as a
        # throughput result rather than as a missing kind.
        from app.providers.remote_agent import _KINDS

        kinds = sorted(set(_KINDS.values()))

    workers: list = []
    if arguments.agent_processes > 1:
        workers, names, _seen, attach_seconds = await attach_distributed_fleet(
            arguments, kinds, arguments.api
        )
        # Names only: the agents live in the child processes. The load phase
        # needs the cluster ids, not the objects.
        agents = [SyntheticAgent(name, None, kinds) for name in names]
        channels = []
    else:
        agents, channels, attach_seconds = await attach_fleet(
            arguments.gateway, arguments.clusters, kinds, arguments.batch
        )
    # The gateway registers on the hello, which it has already received; give
    # the last few a moment rather than racing the assertion below.
    await asyncio.sleep(arguments.settle)

    async with httpx.AsyncClient(base_url=arguments.api, timeout=60.0) as client:
        try:
            seen = (await client.get("/agents")).json()
        except Exception as exc:
            print(f"Could not reach the API at {arguments.api}: {exc}", file=sys.stderr)
            seen = {"items": []}

        attached = len(seen.get("items", []))

        latencies: list[float] = []
        drive_seconds = 0.0
        before = after = {}
        throughput: dict[str, Any] = {}
        if arguments.investigations and arguments.load_processes:
            throughput = await measure_throughput(client, agents, arguments)
        elif arguments.investigations:
            before = await platform_counters(client)
            drive_started = time.perf_counter()
            latencies = await drive_investigations(client, agents, arguments)
            drive_seconds = time.perf_counter() - drive_started
            after = await platform_counters(client)

    failures = [agent for agent in agents if agent.failure is not None]
    report = {
        "fleet": {
            "requested": arguments.clusters,
            "visible_to_the_api": attached,
            "attach_seconds": round(attach_seconds, 2),
            "attaches_per_second": round(arguments.clusters / attach_seconds, 1),
        },
        "harness": {"peak_rss_mb": round(peak_rss_mb(), 1)},
        "collections": {
            "requests_answered": sum(agent.collections for agent in agents),
            "records_emitted": sum(agent.records for agent in agents),
        },
        "stream_failures": {
            "count": len(failures),
            "first": f"{type(failures[0].failure).__name__}: {failures[0].failure}"
            if failures
            else "",
        },
    }
    if throughput:
        report["throughput"] = throughput

    if latencies:
        ordered = sorted(latencies)
        report["investigations"] = {
            "completed": len(ordered),
            # Latency here includes queue wait, so on a saturated platform it
            # measures backlog rather than work. Throughput is the number an
            # envelope should be read from; publishing only percentiles would
            # invite reading a healthy queue as a slow platform.
            "p50_s": round(ordered[len(ordered) // 2], 2),
            "p95_s": round(ordered[int(len(ordered) * 0.95)], 2),
            "max_s": round(ordered[-1], 2),
            "wall_seconds": round(drive_seconds, 2),
            "per_second": round(len(ordered) / drive_seconds, 1) if drive_seconds else 0,
        }
        # The check that would have caught the first envelope's mistake. If the
        # platform was busy for far less time than the run took, the ceiling
        # being measured is the harness's, not the platform's.
        busy = sum(
            after.get(phase, 0) - before.get(phase, 0)
            for phase in ("collect", "analyse", "report", "persist")
        )
        if drive_seconds:
            report["platform_side"] = {
                "busy_seconds": round(busy, 2),
                "utilisation": round(busy / drive_seconds, 3),
                "verdict": (
                    "platform-bound"
                    if busy / drive_seconds > 0.5
                    else "HARNESS-BOUND — the platform was mostly idle; this is not its ceiling"
                ),
            }

    for agent in agents:
        await agent.stop()
    for channel in channels:
        await channel.close()
    for worker in workers:
        worker.terminate()

    if throughput:
        print(f"\nThroughput, load offered from {throughput['load_processes']} processes")
        print(f"  offered      {throughput['offered']:>7}  at {throughput['offered_per_second']}/s")
        print(f"  completed    {throughput['completed']:>7}  in {throughput['drain_seconds']}s")
        print(f"  platform     {throughput['completed_per_second']:>7}/s")
        print(
            f"  server busy  {throughput['platform_busy_seconds']:>7}s  "
            f"({throughput['slot_occupancy']:.0%} of {throughput['slots']} slots)"
        )
        print(f"\n  {throughput['verdict']}\n")

    print(json.dumps(report, indent=2))
    if failures:
        print(
            f"\n{len(failures)} of {len(agents)} streams failed; the numbers above "
            f"describe a broken harness, not the platform.",
            file=sys.stderr,
        )
        return 1
    return 0 if attached >= arguments.clusters * 0.99 else 1


def _submit_batch(api: str, clusters: list[str], count: int, seed: int) -> int:
    """Fire `count` submissions and return how many were accepted.

    Runs in a **separate process**, and does exactly one thing: POST. It never
    polls, because polling was the bottleneck that produced this repository's
    first, wrong throughput number — a client that waits for each result cannot
    offer load faster than the platform answers, so it measures itself.
    """
    import random

    import httpx

    rng = random.Random(seed)
    accepted = 0
    with httpx.Client(base_url=api, timeout=30.0) as client:
        for _ in range(count):
            try:
                response = client.post("/investigations", json={"context": rng.choice(clusters)})
                accepted += response.status_code == 202
            except Exception:
                pass
    return accepted


def _verdict(occupancy: float, offered: int, completed: float) -> str:
    """Say what this run can and cannot conclude.

    Deliberately refuses to say "platform-bound" from one run, because that is
    the mistake this repository has now made twice. Utilisation alone cannot
    establish it: a platform whose *counterparty* is the bottleneck looks fully
    occupied while adding capacity changes nothing.

    **The control is a concurrency sweep.** Run the same offered load at two
    values of `JOB_MAX_CONCURRENT`. If throughput rises with slots, the
    platform was the constraint. If it does not — and per-phase time inflates
    in proportion to slots instead — the constraint is downstream of the
    platform, which for this harness means the synthetic agents, all of which
    live in one process.
    """
    if completed < offered * 0.95:
        return "INCOMPLETE — the platform did not finish what it was offered within the timeout"
    if occupancy < 0.3:
        return (
            f"HARNESS-BOUND — slots only {occupancy:.0%} occupied; the platform spent "
            f"the run waiting for this script"
        )
    return (
        f"INCONCLUSIVE from one run — slots {occupancy:.0%} occupied. Re-run at a "
        f"different JOB_MAX_CONCURRENT: throughput that does not rise with slots "
        f"means the bottleneck is downstream of the platform, not in it."
    )


async def measure_throughput(client, agents, arguments) -> dict[str, Any]:
    """Saturate from several processes and read the *platform's* counters.

    Two corrections to how this was measured the first time, both of which the
    published envelope had to retract:

    - **Completions come from the server.** `k8sagent_investigations_total` is
      the platform's own count; a rate derived from it cannot be limited by how
      fast this script can poll.
    - **Load comes from several processes.** One Python event loop saturates
      well before this platform does, and a saturated client looks exactly like
      a saturated server if you only look at the client.

    The verdict is the point: a throughput number is a statement about the
    platform only if the platform was busy for most of the run.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    clusters = [agent.cluster_id for agent in agents] or ["bench"]
    before = await platform_counters(client)
    started = time.perf_counter()

    per_process = max(1, arguments.investigations // arguments.load_processes)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=arguments.load_processes, mp_context=context) as pool:
        accepted = sum(
            pool.map(
                _submit_batch,
                [arguments.api] * arguments.load_processes,
                [clusters] * arguments.load_processes,
                [per_process] * arguments.load_processes,
                range(arguments.load_processes),
            )
        )
    offered_seconds = time.perf_counter() - started

    target = before.get("finished", 0) + accepted
    deadline = time.perf_counter() + arguments.timeout
    settled = before
    while time.perf_counter() < deadline:
        await asyncio.sleep(1.0)
        settled = await platform_counters(client)
        if settled.get("finished", 0) >= target:
            break
    drain_seconds = time.perf_counter() - started

    completed = settled.get("finished", 0) - before.get("finished", 0)
    busy = sum(
        settled.get(phase, 0) - before.get(phase, 0)
        for phase in ("collect", "analyse", "report", "persist")
    )
    # Divided by the concurrency limit, so this is slot *occupancy* in 0..1
    # rather than a number that exceeds 100% whenever the platform runs more
    # than one investigation at a time — which it always does.
    slots = max(1, arguments.slots)
    occupancy = (busy / drain_seconds / slots) if drain_seconds else 0

    return {
        "offered": accepted,
        "offered_per_second": round(accepted / offered_seconds, 1) if offered_seconds else 0,
        "completed": int(completed),
        "completed_per_second": round(completed / drain_seconds, 1) if drain_seconds else 0,
        "drain_seconds": round(drain_seconds, 2),
        "platform_busy_seconds": round(busy, 2),
        "slot_occupancy": round(occupancy, 3),
        "slots": slots,
        "load_processes": arguments.load_processes,
        "verdict": _verdict(occupancy, accepted, completed),
    }


async def drive_investigations(client, agents, arguments) -> list[float]:
    """Submit investigations across the fleet and time them end to end."""
    import random

    rng = random.Random(arguments.seed)
    targets = [rng.choice(agents).cluster_id for _ in range(arguments.investigations)]

    async def one(cluster: str) -> float | None:
        started = time.perf_counter()
        try:
            response = await client.post("/investigations", json={"context": cluster})
            job = response.json()["id"]
        except Exception:
            return None
        while time.perf_counter() - started < arguments.timeout:
            await asyncio.sleep(0.5)
            try:
                state = (await client.get(f"/investigations/{job}/status")).json()
            except Exception:
                continue
            if state.get("status") in {"succeeded", "failed", "cancelled"}:
                return time.perf_counter() - started
        return None

    semaphore = asyncio.Semaphore(arguments.concurrency)

    async def bounded(cluster: str):
        async with semaphore:
            return await one(cluster)

    results = await asyncio.gather(*(bounded(cluster) for cluster in targets))
    return [value for value in results if value is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clusters", type=int, default=1000)
    parser.add_argument("--gateway", default="localhost:19700")
    parser.add_argument("--api", default="http://127.0.0.1:8700")
    parser.add_argument("--batch", type=int, default=50, help="Agents per settle pause.")
    parser.add_argument("--settle", type=float, default=2.0, help="Seconds before counting.")
    parser.add_argument(
        "--kinds",
        default="",
        help="Evidence kinds the agents claim; empty means every kind the platform can ask for.",
    )
    parser.add_argument("--investigations", type=int, default=0)
    parser.add_argument(
        "--load-processes",
        type=int,
        default=0,
        help=(
            "Submit from N separate processes and count completions from the "
            "platform's own counters. 0 uses the legacy client-side loop, which "
            "measures this script as much as the platform."
        ),
    )
    parser.add_argument(
        "--agent-processes",
        type=int,
        default=1,
        help=(
            "Spread the synthetic fleet across N processes. 1 keeps them all in "
            "this one, which the concurrency sweep showed becomes the bottleneck."
        ),
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=32,
        help="The platform's JOB_MAX_CONCURRENT, so slot occupancy can be computed.",
    )
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=1729)
    arguments = parser.parse_args(argv)

    os.environ.setdefault("OPENAI_API_KEY", "")
    return asyncio.run(main_async(arguments))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
