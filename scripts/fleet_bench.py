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
        if arguments.investigations:
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

    print(json.dumps(report, indent=2))
    if failures:
        print(
            f"\n{len(failures)} of {len(agents)} streams failed; the numbers above "
            f"describe a broken harness, not the platform.",
            file=sys.stderr,
        )
        return 1
    return 0 if attached >= arguments.clusters * 0.99 else 1


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
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=1729)
    arguments = parser.parse_args(argv)

    os.environ.setdefault("OPENAI_API_KEY", "")
    return asyncio.run(main_async(arguments))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
