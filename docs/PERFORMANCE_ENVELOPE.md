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

**These numbers stop at 2,000 pods for a reason, and it is not only that the
cap is there.** `payload_bench` drives a fake whose `run()` overrides
`KubectlExecutor.run`, so neither `json.loads` nor `_cap_items` executes in it
and `MAX_LIST_ITEMS` is never applied — the harness measures the *derived*
payload, which is what M8b wanted, and is blind to the read that produced it.
Run it above the cap and it reports a stored result that keeps growing, because
in that harness nothing caps anything.

### One list read, through the real executor (F5)

```bash
python scripts/payload_bench.py --parse-scan
```

| pods | kubectl stdout | peak parse | retained after cap |
|---|---|---|---|
| 500 | 0.27 MB | 1.5 MB | 0.27 MB |
| 2,000 | 1.09 MB | 5.9 MB | 1.09 MB |
| 5,000 | 2.73 MB | 14.9 MB | 1.09 MB |
| 10,000 | 5.46 MB | 29.7 MB | 1.09 MB |
| 25,000 | 13.67 MB | **74.3 MB** | 1.09 MB |

**The cap does exactly what it claims and nothing more.** What is *retained* is
flat at 1.09 MB from 2,000 pods upward. What is *parsed* is linear and
unbounded — about 2.95 KB per pod, 5.5× kubectl's own output — because
`_cap_items` truncates a document `json.loads` has already built in full. That
is F5's remaining half, and this is the first measurement of it; the 13.4 MB
above is a whole investigation at the cap, not a transient spike on a cluster
past it.

**Deferred rather than built, and the reason is the shape of the number.** At
10,000 pods the spike is 29.7 MB, so a worker at the default
`JOB_MAX_CONCURRENT=4` transiently touches ~119 MB against a 159 MB resident
platform — real, and not the constraint. The measured ceiling is per-worker
throughput at ~12/s with the worker 92% idle in socket waits, which is CPU and
the GIL, not memory; five days spent on memory would not move it. Removing the
spike needs a streaming client, because kubectl assembles the whole list before
writing a byte, and it would replace the only path in the platform that shells
out.

**What an operator has today is scope, not a setting.** Raising or lowering
`MAX_LIST_ITEMS` does not change the spike — it is applied after the parse — so
on a cluster of this size the lever is investigating a namespace rather than
the cluster. That is worth knowing before the 5 days are spent, and it is why
this is written down rather than left as an effort estimate.

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

### Repeating an investigation (F18)

```bash
python scripts/cache_bench.py --context kind-my-cluster
```

The one number in this document taken against a **real cluster and a real
`kubectl`**, because that is the only place the saving exists: the cost being
removed is a subprocess and a round trip, and a fake executor has neither. A
harness measuring `json.loads` against `json.loads` would report an excellent
figure that means nothing — the mistake the throughput number made twice.

Measured on a 53-pod kind cluster carrying `docs/qa/audit-faults.yaml`, three
runs, one investigation immediately after another:

| | cold | warm | `refresh=true` |
|---|---|---|---|
| kubectl processes spawned | 70 | **13** | 70 |
| collect wall time | 0.57 s | **0.16 s** | 0.65 s |
| reads served from memory | 0 | 57 of 70 | 0 |
| usable evidence records | 61 | 61 | 61 |

**81% fewer processes, ~72% less collect time**, and the same evidence both
times. Process count is the headline rather than the seconds: a laptop timing
is a coin flip, and the process count is what scales with cluster size and with
a WAN.

**All 13 of the warm run's misses were failures**, which is the design working
rather than a shortfall. Nothing that failed is ever stored — a cached
`FORBIDDEN` would go on refusing after the RBAC that caused it was fixed. That
cluster is *deliberately broken*, so it is close to the worst case for hit
rate; the 13 are `kubectl top` with no metrics-server and logs from pods that
have no container to read logs from.

**What the warm run does not do is claim to be fresh.** Each evidence record is
stamped with the age of the read behind it, not the age of the investigation,
so a citation resolved six weeks later still means what it says. The harness
asserts this rather than trusting it: the warm run's oldest evidence must be at
least as old as the cold run took.

Cache footprint, for sizing `COLLECTION_CACHE_MAX_BYTES` (default 64 MB):

| cluster | entries | bytes |
|---|---|---|
| 53-pod kind cluster, real reads | 56 | 2.4 MB |
| 2,000-pod synthetic (`payload_bench` fake) | 40 | 1.6 MB |

One whole cluster is tens of entries and single-digit megabytes, so the default
holds many clusters at once; beyond it the eviction is LRU by bytes, and
`tests/test_collection_cache.py` asserts the bound rather than assuming it.

### Payload reads

```bash
python scripts/payload_bench.py --pods 2000
```

| read | before M8b | after |
|---|---|---|
| 25-row job listing | 67.5 MB read from Postgres, 0 returned | payload never selected |
| status read of a finished job | 784 KB | **10.7 KB** (73×, measured at 500 pods) |

---

### Sustained operation — one hour, continuous

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 postgres:17-alpine
docker compose up -d redis
(cd agent && go build -o /tmp/k8s-agent ./cmd/agent)
python scripts/soak_bench.py --minutes 60 --concurrency 2 --pause 12 --agent
```

Two workers, a **real Go agent** against a **real kind cluster**, 19.5
investigations a minute offered for sixty minutes. This is the first entry here
that answers "what happens if you leave it on" rather than "how fast".

| | |
|---|---|
| investigations | **1,168, all of which collected usable evidence** |
| working throughout? | first at 0.0m, last at **59.9m**; longest quiet gap **0.2m** |
| latency | p50 **0.26 s**, p95 **0.57 s**, max 3.82 s |
| resident memory, worker-1 | 119.2 → 129.5 MB, trend **+0.8 MB/h** over the second half |
| resident memory, worker-2 | 117.1 → 123.8 MB, trend **+7.9 MB/h** |
| collection cache | 29,180 hits / 10,379 misses — **74% of reads reused** |
| SSE | 23,589 frames, **0 out of order, 0 duplicates** |
| transports | 594 SSE runs and 574 polling runs, all succeeded, same evidence range |
| agent certificates | **3 renewals** on a 30-minute TTL, **0 stream drops**; 98 agent-served investigations after the last one |
| retention sweep | fired **30m in**, on the platform's own timer |
| sweep cost | **7 ms** to prune 25 investigations and 75 report blobs |

**Memory is flat, which is the headline.** The F18 collection cache holds raw
cluster reads in-process and bounds itself by bytes; nothing bounded what a
leak *elsewhere* would do over an hour, and nothing had run long enough to tell
the two apart. Both workers settle within about 10 MB of where they started and
stay there. Worker-2's +7.9 MB/h is a trend fitted over the second half of a
run whose total movement was 6.7 MB — it is noise at this scale, not a slope to
extrapolate.

**Postgres grows at 87.9 MB/h and retention is what bounds it.** 8.5 → 98.0 MB
over 1,174 rows, or roughly **77 KB per investigation** on a 13-pod cluster
(`payload_bench` measures 2.7 MB at the 2,000-pod ceiling — this figure is the
small end of that range, not a contradiction of it). Nothing pruned it during
the run: `REPORT_RETENTION_DAYS` was at its floor of 1 and the run's own rows
were minutes old, so the sweep collected only the aged rows seeded for it. What
the sweep *did* prove is F19 end to end — **after it ran, 0 of the aged
investigations still carried a payload and 0 blobs remained**, which is the
half of retention that used to be missing.

**`refresh=true` bypasses the cache and it is measured, not assumed**: 218 of
the 1,168 runs asked for a refresh and those served **0 reads from cache**,
while the population as a whole reused 74%.

Two things an hour found that a short run cannot:

- **One investigation in 1,168 (0.09%) was answered from the local kubeconfig**
  rather than through the agent, with no refusal. That is M8a's deliberate
  fail-open: `_fleet_holder` returns nothing when the presence index cannot be
  read, because refusing every investigation on a Redis hiccup would turn a
  degraded dependency into an outage. Presence carries a 45-second TTL
  refreshed by a 15-second heartbeat, and under host contention that window can
  lapse. The behaviour is correct and documented; what is new is the **rate**,
  and that `cluster_access` reported it honestly rather than hiding it. On this
  harness the kubeconfig points at the same cluster, so the answer was right —
  on a real fleet it is the same-named-cluster risk the refusal exists to
  prevent, at 1-in-1,000 rather than the 2-in-3 M8a started from.
- **Three kubectl reads failed with their own error message replaced by gRPC's.**
  A worker that runs an agent gateway *and* falls back to kubectl forks a
  subprocess out of a process holding gRPC channels; gRPC's fork handlers write
  to the inherited stderr, so `kubectl logs` exits non-zero and the captured
  stderr reads `ev_poll_posix.cc:593 FD from fork parent still in poll list`
  instead of whatever kubectl was trying to say. The read is correctly recorded
  as failed evidence — the defect is diagnosability, and it is the same shape as
  the agent-path `unknown` that `detailFor` was written to fix. 3 of roughly
  23,000 reads, all inside the single kubeconfig-fallback investigation above.
  Recorded in `docs/PRODUCTION_READINESS.md`, not fixed here.

**What this does not measure**: one cluster of 13 pods, two workers, one host,
loopback. It is a claim about *duration*, not about scale — the scale numbers
are above, and they were taken separately for that reason.

## What was **not** measured

Stated plainly, because each is a real limit on how far the numbers above
travel.

- **5,000 concurrent investigations.** One worker was measured. The design is
  stateless workers behind a shared queue (M3) and routing is per-worker (M8a),
  so scale-out is expected to be close to linear — but *expected* is not
  *measured*, and the harness cannot generate enough load from one process to
  test it. **§12's scalability score should not move to 9 on the strength of
  this document.** Partially addressed below — see *Scale-out past two
  workers*, which found the answer depends on which collection path is in use.
- **Scale-out across hosts.** Everything below is co-located on one machine.
  That turns out to matter more than expected.
- **The Go agent at *fleet* scale.** Every agent in the 1,000-cluster figures
  is a coroutine answering from a canned payload over the published protobuf
  contract, which measures the platform's side of the wire rather than a real
  agent. The hour-long soak *does* use the real binary against a real API
  server and a real cluster — 1,167 of its 1,168 investigations collected
  through it — but that is **one** agent, so what remains unmeasured is a
  thousand real ones, not the agent itself.
- **mTLS at fleet scale.** Measured with `AGENT_GATEWAY_TLS=disabled`. mTLS
  changes the handshake cost, and the 1.04 s attach figure would move; steady
  state should not, but that is reasoning rather than measurement.
- **Real networks.** Everything is loopback. No latency, no loss, no proxy, no
  egress filtering — which is precisely the environment ADR-004 shaped the
  transport around.
- **Sustained operation beyond one hour.** An hour is now measured — see
  *Sustained operation* above — and it is flat. Nothing says what a day or a
  week does, and the Postgres figure (+87.9 MB/h before retention collects
  anything) is the number most likely to behave differently over one.
- **Whether the cache helps a *fleet*.** The F18 figures above are one
  process investigating one cluster twice. The cache is per worker and per
  `(tenant, cluster, identity)`, so on a fleet the hit rate depends on how
  often the same cluster is investigated by the same caller within the TTL —
  which is a usage question, not a platform one, and nothing here measures it.
  What is measured is the saving *when* a read is reused.
- **The cache under a *storm* of `refresh=true`.** The soak ran 218 of 1,168
  investigations with `refresh: true` and confirmed they served **0 reads from
  cache** while the population reused 74% — so the bypass works and costs what
  a cold read costs. What is still unmeasured is the pathological shape: an
  alert storm in which *most* traffic refreshes, rewriting a hot cluster's
  entries faster than anyone reuses them.
- **Where the per-worker limit binds.** ~12/s per worker is measured and
  scale-out is linear, but which serialisation inside the process sets it —
  event loop, GIL, or gRPC stream handling — was not isolated. Ruled out: CPU,
  the Postgres pool, platform slots and the synthetic fleet.
  Per-phase attribution *is* measured and is above.

## Scale-out past two workers (Tier-5 item 44)

```bash
docker compose up -d postgres redis
python scripts/scaleout_bench.py --workers 1,2,3,4 --investigations 150
```

Measured on the **local-kubeconfig** path, against a cluster that refuses
connections so `collect` fails immediately and what is timed is the platform's
own work — analysis, report rendering, persistence, queue round trip.

| workers | throughput/s | per worker | vs 1 worker |
|---|---|---|---|
| 1 | 8.2 | 8.2 | 1.00x |
| 2 | 9.4 | 4.7 | 1.14x |
| 3 | 9.8 | 3.3 | 1.19x |
| 4 | 9.8 | 2.4 | 1.19x |

**Flat past two workers — and that is not the platform's ceiling.** The
per-worker limit is real: at one worker, throughput rises 3.8 → 8.3/s as
`JOB_MAX_CONCURRENT` goes 1 → 4 and then stops (8.6/s at 12), which by this
document's own rule makes ~8.5/s a genuine per-worker figure for this workload.
What does *not* follow is that adding workers cannot help.

Every worker in this run shares one host, and on the local-kubeconfig path each
investigation shells out to kubectl roughly fifteen times. **Process spawning
is a host resource, not a per-worker one**, so four co-located workers compete
for the same thing rather than adding capacity.

That is why this does not contradict the 1 → 2 linear result above: that was
measured on the **agent** path, where collection crosses a gRPC stream and
spawns no subprocess at all. The two numbers describe different bottlenecks.

The operational consequence is worth stating directly, because it changes
deployment advice:

- **Agent-reached clusters:** add workers, and expect them to help. Measured
  linear 1 → 2.
- **Kubeconfig-reached clusters:** adding workers *on the same host* buys
  little. Add hosts, or move the fleet onto agents.

Still unmeasured: workers on separate hosts, which is the configuration that
would separate the platform's ceiling from this machine's. `scaleout_bench.py`
prints that caveat with every run rather than leaving the table to be quoted
alone — the throughput figure in this document has been wrong twice already,
and both times a harness reported a limit it could not distinguish from its own.

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

# The one that needs a real cluster and a real kubectl, not the stack above.
kind create cluster --name bench
kubectl --context kind-bench apply -f docs/qa/audit-faults.yaml
python scripts/cache_bench.py --context kind-bench
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
