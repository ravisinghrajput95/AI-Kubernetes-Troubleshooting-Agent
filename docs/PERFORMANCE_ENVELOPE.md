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

### Investigation throughput: ~10/s per worker

```bash
python scripts/fleet_bench.py --clusters 1000 --investigations 500 \
    --concurrency 64 --timeout 240
```

Distributed deployment (Postgres + Redis), `JOB_MAX_CONCURRENT=32`, evidence
collected through real agent streams:

| offered load | completed | throughput | p50 | p95 | max |
|---|---|---|---|---|---|
| 64 in flight | 500 | **10.9/s** | 5.7 s | 6.7 s | 8.7 s |
| 250 in flight | 500 | **9.5/s** | 25.1 s | 29.7 s | 32.7 s |

**Throughput is flat under ~4× more offered load while latency grows roughly
linearly.** That is saturation, and it is what makes ~10/s a platform ceiling
rather than a harness artefact — the first run alone could not distinguish the
two, which is why the second exists.

The consequence for reading the p50: on a saturated platform, latency is
backlog. A 25-second p50 at 250 in flight is a healthy queue absorbing four
times the work the worker can do, not a slow investigation. Size with
throughput; alarm on queue depth.

11,500 collection requests were answered across the fleet during that run, with
**zero stream failures**.

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
  *measured*, and at ~10/s per worker, 5,000 in flight implies a fleet of
  workers this bench never stood up. **§12's scalability score should not move
  to 9 on the strength of this document.**
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
- **Where the ~10/s ceiling actually is.** Collection, report rendering,
  Postgres writes and analysis all sit inside it and were not isolated. Naming
  a bottleneck without measuring it is how the wrong thing gets optimised.

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
