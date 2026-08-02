# Performance envelope

M8's exit criterion. Every number here was measured by a harness in this
repository, on the hardware named below, and can be re-taken by running the
command printed beside it. Nothing here is extrapolated unless it says so, and
the things that were **not** measured are listed as prominently as the things
that were — an envelope whose limits are implicit is a marketing document.

*Taken 2026-08-02. Apple M5 Pro, 15 cores, 48 GB, macOS. Postgres 17 and Redis
in Docker on the same machine. One platform worker.*

---

## What was measured

### Fleet: 1,000 clusters on one gateway

```bash
python scripts/fleet_bench.py --clusters 1000 --batch 50 --settle 5
```

| | |
|---|---|
| streams attached | **1,000 / 1,000** |
| visible to `GET /agents` | **1,000** |
| time to attach the fleet | **1.04 s** (≈960 attaches/s) |
| platform RSS holding the fleet | **159 MB** |

Each synthetic agent holds its own gRPC channel and its own `Connect` stream,
because that is what a fleet is — a thousand separate processes in a thousand
clusters. Multiplexing them onto one connection would have measured something
nobody deploys.

§3.2 estimated "roughly 10k concurrent streams per replica; 1,000 clusters
needs one replica". 1,000 is comfortably held; the 10k figure remains an
estimate.

### Investigation throughput — measured twice, wrong twice, settled here

This section has been wrong in two different ways. Both are kept, because the
sequence is more useful than the answer.

**Attempt 1 — "~10/s, platform-bound."** Wrong reasoning. It argued saturation
from *throughput flat under 4× offered load while latency grew linearly*. A
saturated **client** produces that signature identically, and both load levels
came from one Python event loop.

**Attempt 2 — "the platform does ~143/s; the ~10/s was harness polling."** Also
wrong. Single-investigation latency (0.223 s end to end) was extrapolated to 32
slots. **Single-request latency does not extrapolate to throughput**, and the
retraction should not have assumed it did.

**Attempt 3 — the concurrency sweep.** Load offered from 6 processes, with
completions counted from the platform's own `k8sagent_investigations_total`.
Every candidate was given more capacity in turn, and each of these is a real
measurement:

| what was scaled | from → to | throughput |
|---|---|---|
| platform slots (`JOB_MAX_CONCURRENT`) | 4 → 32 | 11.6 → 12.2/s |
| synthetic agent processes | 1 → 6 | 12.3 → 12.0/s |
| Postgres pool (`max_size`) | 10 → 64 | 12.2 → 12.1/s |
| **platform workers** | **1 → 2** | **12.1 → 23.0/s** |

**Only the last one moves it, and it moves it linearly.** That is the answer:
the ceiling is **per worker process**, and the platform scales by adding
workers — which is exactly what M3's stateless-workers-behind-a-shared-queue
design claims, now measured rather than assumed.

*The two-worker figure required each worker to have its own gateway and its own
attached agents.* An earlier attempt pointed both agent fleets at one gateway,
so the second worker could reach no cluster and made things slower — a result
that looked like evidence against scale-out and was a broken setup.

**Why one worker stops at ~12/s.** In-process stack sampling (`py-spy` needs
root on macOS; `sys._current_thread_frames` does not) shows the worker ~92%
idle under full load, with every non-idle sample in Postgres or Redis socket
waits — `connection.py:wait` at 4.3%, Redis publish at 0.8%. There is no CPU
hotspot, and the machine sits at ~400% of an available 1500%. One worker
serialises everything through a single Python process: HTTP, the gRPC streams
for every attached agent, the queue consumer, orchestration and analysis.
`asyncio.to_thread` moves blocking calls off the loop but not off the GIL.

**What this means operationally.** `JOB_MAX_CONCURRENT` above a small number
buys nothing on one worker — slots fill, per-phase time inflates in proportion
(`collect` 0.24 s → 2.06 s from 4 to 32 slots) and throughput does not move.
Add workers, not slots.

```bash
python scripts/fleet_bench.py --clusters 48 --agent-processes 6 \
    --investigations 400 --load-processes 6 --slots 32
```

**Still not measured:** where the per-worker limit actually binds. It is a
serialisation inside one process, not CPU and not the Postgres pool, but which
of the loop, the GIL or the gRPC stream handling dominates was not isolated.
Scale-out makes it a sizing question rather than a blocker.

### Memory per investigation

```bash
python scripts/payload_bench.py --pods 2000 --memory
```

Peak heap is about **5× the stored result** and flat across cluster sizes:

| cluster | stored result | peak heap |
|---|---|---|
| 100 pods | 0.30 MB | 1.3 MB |
| 1,000 pods | 1.44 MB | 7.0 MB |
| 2,000 pods (the `MAX_LIST_ITEMS` cap) | 2.70 MB | **13.4 MB** |

Roughly **76 concurrent investigations per GB**. This is what sizes
`JOB_MAX_CONCURRENT`, and it is why streaming ingest was measured and not built
— see `docs/ENTERPRISE_ARCHITECTURE.md` §9, M8b.

### Routing

```bash
python scripts/routing_bench.py --clusters 1000 --workers 3 --submissions 2000
```

| | |
|---|---|
| routing hit rate, 1,000 clusters / 3 workers | **100%** (1/N = 33% before M8a) |
| routing hit rate, 1,000 clusters / 10 workers | **100%** (1/N = 10%) |
| affinity lookup on the submit path | **0.33 ms p50, 0.5 ms p99**, flat 200 → 1,000 clusters |

Flat because the lookup is a `GET` on one key rather than a scan of the
tenant's agents; the fleet's size is not the cost of starting an investigation.

### Payload reads

```bash
python scripts/payload_bench.py --pods 2000
```

| read | before M8b | after |
|---|---|---|
| 25-row job listing | 67.5 MB read from Postgres, 0 returned | payload never selected |
| status read of a finished job | 784 KB | **10.7 KB** (73×, measured at 500 pods) |

---

## What was **not** measured

Stated plainly, because each is a real limit on how far the numbers above
travel.

- **5,000 concurrent investigations.** One worker was measured. The design is
  stateless workers behind a shared queue (M3) and routing is per-worker (M8a),
  so scale-out is expected to be close to linear — but *expected* is not
  *measured*, and the harness cannot generate enough load from one process to
  test it. **§12's scalability score should not move to 9 on the strength of
  this document.**
- **The Go agent.** Every agent here is a coroutine answering from a canned
  payload over the published protobuf contract. This measures the platform's
  side of the wire, not a real agent, a real API server, or a real cluster.
- **mTLS at fleet scale.** Measured with `AGENT_GATEWAY_TLS=disabled`. mTLS
  changes the handshake cost, and the 1.04 s attach figure would move; steady
  state should not, but that is reasoning rather than measurement.
- **Real networks.** Everything is loopback. No latency, no loss, no proxy, no
  egress filtering — which is precisely the environment ADR-004 shaped the
  transport around.
- **Sustained operation.** The longest run here is under a minute. Nothing says
  what happens over hours: no leak test, no certificate rotation under load, no
  Postgres growth over a retention period.
- **Where the per-worker limit binds.** ~12/s per worker is measured and
  scale-out is linear, but which serialisation inside the process sets it —
  event loop, GIL, or gRPC stream handling — was not isolated. Ruled out: CPU,
  the Postgres pool, platform slots and the synthetic fleet.
  Per-phase attribution *is* measured and is above.

## Known limits that are design, not measurement

- **`JOB_MAX_CONCURRENT` binds only the distributed deployment.** The
  single-process path runs what it accepts, immediately, with no queue and no
  cap — correct for the getting-started deployment, and not something to put a
  fleet behind.
- **`MAX_LIST_ITEMS` (2,000)** is what keeps the per-investigation numbers
  bounded at all. A cluster larger than that is investigated from a partial
  view, and the truncation is recorded as evidence.

## Re-taking these numbers

```bash
docker compose up -d postgres redis

cd backend && AGENT_GATEWAY_PORT=19700 AGENT_GATEWAY_TLS=disabled \
  AUTH_MODE=disabled ALLOW_INSECURE_NO_AUTH=true JOB_MAX_CONCURRENT=32 \
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/k8sagent_test \
  REDIS_URL=redis://localhost:6379/6 \
  .venv/bin/python -m uvicorn app.main:app --port 8700

python scripts/fleet_bench.py --clusters 1000 --investigations 500 --concurrency 64
python scripts/payload_bench.py --pods 2000 --memory
python scripts/routing_bench.py --clusters 1000 --workers 3
```

`fleet_bench.py` reports `stream_failures` and exits non-zero when any stream
died. That exists because its first run printed "5 collections, 0 records" — a
plausible-looking platform result produced entirely by an `AttributeError` in
the harness. A benchmark that fails quietly publishes confident nonsense.

It also reports `platform_side.utilisation` now, for the same reason one level
up: a benchmark that *succeeds* quietly can publish confident nonsense too. The
first version of this document did exactly that, and the fix is that the
harness now has to show the server was busy before its throughput number means
anything.
