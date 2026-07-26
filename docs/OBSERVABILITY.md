# Prometheus and Loki

Optional evidence sources. Both are off by default and the platform is fully
functional without them.

## Configuration

```bash
PROMETHEUS_URL=http://prometheus.monitoring:9090
LOKI_URL=http://loki.monitoring:3100
OBSERVABILITY_TIMEOUT_SECONDS=15
METRICS_LOOKBACK_MINUTES=60
```

Unset means "not deployed". The collectors record `not_applicable` evidence
naming the variable to set, and the investigation proceeds unchanged.

## What they add

kubectl answers *what state a resource is in now*. Metrics and retained logs
answer *how it got there*, which is often the difference between a plausible
diagnosis and a confirmed one.

| Signal | What it settles |
|---|---|
| `metrics.memory_peaked_at_limit` | Memory reached the limit — the limit caused the kill, not pressure elsewhere |
| `metrics.memory_near_limit` | Headroom is nearly gone, before anything has failed |
| `metrics.cpu_throttled` | Throttling is slowing the container, which fails timing-sensitive probes |
| `metrics.restart_rate` | Restarts are repeating, not a one-off |
| `metrics.node_overcommitted` | Requests exceed node headroom, so scheduling has nothing to place against |
| `logs.historical_errors` | Errors from container instances kubectl can no longer serve |

The last one matters: `kubectl logs` serves only the current and previous
container. A pod that has restarted twenty times has lost the interesting one.

## Which metrics are queried, and which are not

Queries use only metric names exported by **cAdvisor** and
**kube-state-metrics** — `container_memory_working_set_bytes`,
`container_cpu_cfs_throttled_periods_total`,
`kube_pod_container_status_restarts_total`, `kube_node_status_allocatable`, and
similar. These are standard across clusters.

Application-level metrics — request latency, error rates, throughput — are
**deliberately not queried**. Their names are per-application, and a guessed
metric name returns an empty result that is indistinguishable from a healthy
one. Inventing `http_requests_total` would produce a silently wrong signal.

Adding them properly means letting an operator declare their own PromQL
alongside the signal it should raise, which is a configuration feature rather
than a guess.

## Degradation is data, never silence

Both clients return a result carrying a status instead of raising:

| Condition | Status |
|---|---|
| Not configured | `not_applicable` |
| Unreachable | `unavailable` |
| Slow | `timeout` |
| Query rejected | `failed` |
| No series matched | `empty` (usable — "we looked, there was nothing") |

The distinction between `empty` and `unavailable` carries real diagnostic
weight. "The query ran and found no throttling" is evidence. "Prometheus was
down" is not, and must never be presented as if it were.

**Absent backends produce no signals at all — including no negative ones.** A
missing Prometheus must never read as "metrics look fine".

## Completeness accounting

`EvidenceStore.coverage()` excludes `not_applicable` evidence from the
completeness ratio, reporting it separately as `not_applicable`.

This matters more than it first appears. Completeness feeds the confidence
score. If an undeployed Prometheus counted as a gap, every cluster without it
would permanently cap completeness — and so would lower confidence in diagnoses
that never needed metrics. Not having a backend is not the same as failing to
collect from one.

It still appears in `degraded`, and therefore in the report's evidence gaps,
because "the platform could have seen more here" is worth telling an operator.

## Where they run

Both are emitted by playbooks, not baseline collection — targeted at the
resource under investigation:

- **CrashLoop** → pod metrics and historical logs for each affected pod.
- **Pending** → node metrics for nodes named by node-level signals.

Baseline cluster usage still comes from `kubectl top`, which needs no extra
deployment.

## Testing

Clients are exercised through `httpx.MockTransport`, so real request building
and response parsing run — only the network is faked. Covered: parsing, empty
results, unreachable backends, timeouts, rejected queries, unconfigured
backends, and PromQL label escaping for hostile resource names.
