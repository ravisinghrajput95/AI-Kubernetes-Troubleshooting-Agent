# Prometheus and Loki — configuration and verification

Both backends are optional. Unset `PROMETHEUS_URL` / `LOKI_URL` and the
collectors record `not_applicable` evidence naming the variable to set; nothing
degrades and no signal is invented. This document covers what to point them at,
and records the verification that closes audit items 31 and 32.

Until 2026-08-03 neither integration had been run against a real backend. Both
were wrong, and in the same direction: queries that parse, return `success`, and
match nothing.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PROMETHEUS_URL` | *(unset)* | Base URL, no path. `http://prometheus-operated.monitoring:9090` |
| `LOKI_URL` | *(unset)* | Base URL, no path. `http://loki.monitoring:3100` |
| `OBSERVABILITY_TIMEOUT_SECONDS` | 15 | Per-query timeout |
| `METRICS_LOOKBACK_MINUTES` | 60 | Window for `max_over_time` and `increase` |

Neither client raises for an operational problem: an unreachable, slow or
unconfigured backend becomes a result carrying the reason, which the collector
turns into non-usable evidence with a status the report can cite.

### What Prometheus must be scraping

The platform queries **cAdvisor** (via the kubelet) and **kube-state-metrics**.
Both are installed by `kube-prometheus-stack`; a Prometheus scraping neither
will answer every query successfully and match nothing.

| Series | Source | Used for |
|---|---|---|
| `container_memory_working_set_bytes` | cAdvisor | current and peak memory, node usage |
| `container_cpu_usage_seconds_total` | cAdvisor | CPU cores |
| `container_cpu_cfs_throttled_periods_total`, `container_cpu_cfs_periods_total` | cAdvisor | throttling ratio |
| `kube_pod_container_resource_limits` | kube-state-metrics | **the memory limit** |
| `kube_pod_container_status_restarts_total` | kube-state-metrics | restart count and rate |
| `kube_node_status_allocatable` | kube-state-metrics | node commitment |
| `kube_pod_container_resource_requests` | kube-state-metrics | node commitment |

`container_spec_memory_limit_bytes` is accepted as a **fallback** for the memory
limit and must not be relied on — see below.

### What Loki must be receiving

Log streams labelled `namespace` and `pod`. Promtail's default Kubernetes
scrape config produces both, as does Grafana Alloy's. Verified against promtail
3.5.1 shipping to Loki 3.6.11 with `auth_enabled: false`; a multi-tenant Loki
requiring `X-Scope-OrgID` is **not** supported and would need a header the
client does not send today.

---

## Three defects, and why the test suite could not see them

All three were found by standing the backends up. The suite was green
throughout, and stayed green through the first two fixes.

### 1. The memory limit did not exist (HIGH)

`container_spec_memory_limit_bytes` is a real metric name and the obvious source
for a container's limit. **kube-prometheus-stack drops it**, along with every
other `container_spec.*` series, in its kubelet ServiceMonitor:

```json
{"action": "drop", "regex": "container_spec.*", "sourceLabels": ["__name__"]}
```

That is a cardinality decision by the most widely deployed Prometheus
configuration for Kubernetes, and it meant `memory_limit_bytes` was `None` on
the deployments this platform is most likely to meet. Both derived percentages
depend on the limit, so both memory signals — `metrics.memory_near_limit` and
`metrics.memory_peaked_at_limit` — were unreachable. The evidence still recorded
`OK`.

Measured: a 96Mi container the kernel OOMKilled eight times, exit 137, produced
**no memory finding at all**. Corroborating an OOM with metrics is the headline
reason this integration exists, and `evals/cases/investigations/oom-corroborated-by-metrics.json`
tests exactly that shape against a hand-written payload.

The limit is now read from kube-state-metrics first, falling back to cAdvisor:

```promql
max(kube_pod_container_resource_limits{namespace=…,pod=…,resource="memory"})
  or max(container_spec_memory_limit_bytes{namespace=…,pod=…})
```

kube-state-metrics is preferred on its own merits: it reports the *declared*
limit, which is what "% of the container limit" means, and it emits no series
at all for a container with no limit — where cAdvisor reports the cgroup
sentinel (`9223372036854771712`), against which any usage divides to "0.0% of
the limit". `_plausible_limit()` discards that sentinel on the fallback path.

### 2. The node query filtered on a label that does not exist (MEDIUM)

```promql
node_memory_MemAvailable_bytes{node="…"}      # always empty
```

node-exporter series carry `instance`, `job` and `pod` — never `node`.
kube-prometheus-stack adds no such label. This read `None` on every deployment
since it was written, and no signal rule consumed it, so nothing failed; it was
a permanently-absent field presented as evidence.

Replaced with a query on series that *do* carry `node`, and renamed to say what
it now measures:

```promql
sum(container_memory_working_set_bytes{node="…"})    # used_memory_bytes
```

### 3. The peak depended on Prometheus's result ordering (MEDIUM, latent)

```promql
max_over_time(container_memory_working_set_bytes{namespace=…,pod=…}[60m])
```

This looks aggregated and is not. Over a pod's selector it returns **one series
per container and per restarted container instance** — ten of them for a pod
that had crashed six times — and `QueryResult.scalar()` takes `samples[0]`.
Prometheus does not guarantee result ordering, so the peak was whichever series
came back first: **5.9 MB or 92 MB from the same query**, the difference between
a critical signal and silence. It read correctly in testing by luck.

`restarts_in_window` had the same shape (one series per container, so any pod
with a sidecar), and the node allocatable queries were unaggregated against
possible duplicate kube-state-metrics replicas. All are now wrapped.

`tests/test_observability.py::test_every_query_reduces_to_a_single_series`
asserts the property directly against captured real responses, so a future
unaggregated query fails rather than working by luck.

### 4. A restart erased the evidence for the restart (HIGH)

Found only *after* fixing 1-3, because it needed real data to be reachable.

With the limit resolving, the OOMKilled container reported `memory_peak_percent
91.6` and `memory_utilisation_percent 0.2` — the sample landed just after a
restart. Still no signal, for two compounding reasons:

- **The 98% peak threshold was unreachable.**
  `container_memory_working_set_bytes` is a *sampled* gauge (15s here, commonly
  30-60s). A container that allocates until the kernel kills it dies *between*
  scrapes, so the last observed sample is always short of the limit. 98% needs
  the scrape to land in the final fraction of a second before the kill. Now 90%.
- **`peak` and `current` were chained with `elif`.** A low current skipped the
  peak check entirely, so the restart that *proves* the fault erased the history
  `max_over_time` was queried to recover. The rule now judges on the worst of
  the two and says which it saw.

---

## Reproducing the verification

```bash
kind create cluster --name obs

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm install kps prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.enabled=false --set alertmanager.enabled=false \
  --set prometheus.prometheusSpec.scrapeInterval=15s --wait

helm install loki grafana/loki --namespace monitoring \
  -f docs/qa/loki-values.yaml --wait
helm install promtail grafana/promtail --namespace monitoring \
  --set "config.clients[0].url=http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push" --wait

kubectl apply -f docs/qa/observability-faults.yaml

kubectl port-forward -n monitoring svc/kps-kube-prometheus-stack-prometheus 9090:9090 &
kubectl port-forward -n monitoring svc/loki 3100:3100 &
```

`docs/qa/observability-faults.yaml` induces three faults chosen to exercise
distinct query shapes, not distinct workloads: a 96Mi container allocating until
it is OOMKilled (limit, peak, restart rate), a 20m-CPU busy loop (CFS
throttling), and a crashlooping logger emitting error lines (Loki retention).
Allow ~5 minutes for restarts to accumulate and for `max_over_time` to have a
window.

Then, with `PROMETHEUS_URL` and `LOKI_URL` set, run an investigation scoped to
`obsfault`. Expected on a correct deployment:

| Signal | Source |
|---|---|
| `metrics.memory_peaked_at_limit` (CRITICAL) | 91.6% of a 96Mi limit |
| `metrics.cpu_throttled` (HIGH) | ~93-100% of periods throttled |
| `metrics.restart_rate` (HIGH) | both crashing pods |
| `logs.historical_errors` (MEDIUM) | 112-200 retained lines |

## The regression harness

`tests/fixtures/real_observability_kps_loki.json` holds the captured responses
of that stack, keyed by the exact query string the collector issued — recorded
by intercepting httpx while the shipped collectors ran, so the keys are what the
code asks rather than what someone believed it asks.

`TestAgainstCapturedRealBackends` replays them. **An unrecognised query returns
an empty vector**, which is precisely what the real Prometheus returned for the
metric names this code used to use. That is what makes the fixture a regression
harness rather than a recording: point a query back at a series the platform
does not export and the value goes missing in the test the same way it went
missing in the cluster.

`known_absent` in that fixture preserves the live responses for both broken
queries, so the claim "these names resolve to nothing" is evidence rather than
assertion.

## Not verified

- **Multi-tenant Loki** (`auth_enabled: true`). The client sends no
  `X-Scope-OrgID`; a tenanted Loki would reject or misroute every query.
- **Grafana Alloy** as the log shipper. Promtail only. Alloy's Kubernetes
  defaults produce the same `namespace`/`pod` labels, but this was not run.
- **Prometheus behind auth** — no credential is sent, and none is configurable.
- **Remote-write / Thanos / Mimir** query frontends, which differ in how they
  answer `query_range` and in retention semantics.
- **Multi-container pods.** The aggregation fix is correct by construction and
  is asserted against captured data, but every fault pod here had one container.
- Single-node `kind` only, so node overcommitment (`METRICS_NODE_OVERCOMMITTED`,
  which needs ≥90% committed) never fired live and remains unit-tested only.
