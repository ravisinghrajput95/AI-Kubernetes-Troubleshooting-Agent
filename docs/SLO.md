# Service level objectives

Backlog item 30. `docs/PERFORMANCE_ENVELOPE.md` says what the platform *does*;
this says what it *promises*, expressed only in metrics `/metrics` actually
exports so every objective is measurable the day it is adopted.

**These are proposed defaults, not measured attainment.** The envelope's
throughput figure was published wrong twice, and the correction each time came
from measuring rather than reasoning. Nothing here has been observed over a
28-day window against production traffic, because there is no production
traffic yet. Adopt them as targets, then revise them against what you observe —
and treat the first month as calibration rather than as a breach.

## Measured once: 19 minutes on one host

**The only observation of these objectives, and it is small.** It says the
published expressions evaluate against a real Prometheus and that a working
platform clears the targets under a sustained synthetic load. It does not say
what a production deployment attains.

| # | Objective | Target | Measured | Sample |
|---|---|---|---|---|
| 1 | Investigation success rate | ≥ 99.0% | **100%** | 360 investigations |
| 2 | Investigation latency p95 | < 30 s | **≤ 0.5 s** (0.56 s at the client) | 360 |
| 3 | Submission availability | ≥ 99.9% | **100%** | 362 submissions |
| 4 | Evidence completeness | ≥ 95.0% | **100%** | 9,889 evidence records |
| 5 | Grounding rejection rate | < 5.0% | **not measured** | — |
| 6 | Fleet visibility | ≥ 99.0% | **100%** | 74 samples |
| 7 | Queue depth below capacity | ≥ 99.0% of samples | **100%** | 74 samples |

**Conditions**, 2026-09-18, 13:22:00–13:40:30 UTC: one Apple Silicon Mac under
Docker Desktop; two workers on Postgres and Redis; one kind cluster carrying the
nine induced faults of `docs/qa/audit-faults.yaml`; one Go agent reading as the
caller through impersonation, and every investigation collected through it; the
F18 collection cache at its defaults; no model; `scripts/soak_bench.py
--concurrency 2 --pause 12`. Evaluated with

```bash
python scripts/slo_attainment.py --start 2026-09-18T13:22:00Z --end 2026-09-18T13:40:30Z \
    --runs soak.json --enrolled 1
```

which reads each objective's PromQL out of this document rather than a copy of
it, so the expressions above are the ones measured.

**What it cannot say:**

- **Nineteen minutes is not 28 days**, and the run was meant to be two hours.
  The Mac idle-slept at minute 19 (`pmset -g log`: "Entering Sleep state due to
  'Idle'"), waking only for maintenance for the rest of the window; the soak's
  own guard refused the full run for a 15-minute gap with nothing completing,
  and the evaluation is limited to the window before the sleep. **Run a soak
  under `caffeinate -i`, on power, lid open.**
- **One friendly cluster is not customers' clusters.** Success rate and
  completeness read 100% because nothing here was unreachable, refusing reads,
  or running an older agent — the failures SLO 1 is set at 99% to absorb.
- **Latency is bounded, not resolved.** Every investigation fell in
  `k8sagent_investigation_duration_seconds`' lowest bucket (0.5 s), so the p95
  Prometheus reports, 0.47 s, is interpolation inside that bucket. The client's
  own end-to-end timings give p95 0.56 s — including its polling interval; the
  median over the event stream was 0.08 s. For a 30 s objective the resolution
  is enough; for tracking regressions below half a second it is not.
- **SLO 3 is measured at the load generator**, which is the vantage point this
  document prescribes (an ingress); the platform has no per-route status series.
- **SLO 5 has no measurement.** Its phase — the same load with a local gemma4
  answering — started while the Mac was asleep; four investigations timed out
  and the soak refused the phase. `docs/EVALUATION.md` has the model-answer
  measurements that exist (20/20 grounded, gemma4 and `gpt-4o-mini`), which are
  the same ratio over a golden corpus rather than live load.

**What it found** is the part that made it worth running. Evaluating SLO 5 as
published reported **100% grounding rejections on a deployment with no model**:
an unconfigured model was recorded as a failed call, the gate matched on a label
value the platform never emitted, and `GroundingRejectionRateHigh` would have
fired permanently on every such deployment. Fixed before this measurement was
recorded — see objective 5 below.

## Why the objectives are shaped the way they are

Two constraints from the architecture drive everything below.

**Availability is not the same as correctness.** This platform is a diagnostic
tool for incidents. Its worst failure is not being slow — it is confidently
reporting a wrong root cause, or silently reporting a degraded investigation as
a complete one. So there is a **quality** SLO alongside the usual latency and
availability ones, and it is the one to defend first.

**Metrics carry no cluster, tenant, namespace or user label**, deliberately —
per-cluster series would fall over at 1,000 clusters and would publish the
customer list to any scraper. So **every SLO here is fleet-wide.** Per-tenant
questions are answered from the audit log or from Postgres
(`docs/TENANT_USAGE_REPORTING.md`), never from a label here.

---

## The objectives

| # | SLO | Target | Window |
|---|---|---|---|
| 1 | **Investigation success rate** | 99.0% | 28 days |
| 2 | **Investigation latency** — p95 to terminal state | < 30 s | 28 days |
| 3 | **Submission availability** — `POST /investigations` non-5xx | 99.9% | 28 days |
| 4 | **Evidence completeness** — usable evidence records | 95.0% | 28 days |
| 5 | **Diagnosis soundness** — grounding rejection rate | < 5.0% | 28 days |
| 6 | **Fleet visibility** — agents online vs enrolled | 99.0% | 28 days |
| 7 | **Queue latency** — depth below one worker's capacity | 99.0% of samples | 7 days |

### 1. Investigation success rate — 99.0%

```promql
sum(rate(k8sagent_investigations_total{outcome="succeeded"}[28d]))
/
sum(rate(k8sagent_investigations_total[28d]))
```

**Not 99.9%, on purpose.** A `failed` investigation is frequently the customer's
cluster being unreachable, their RBAC refusing every read, or their agent being
down — none of which the platform can fix and all of which count here. Chasing
three nines would push toward reporting a total collection failure as a success,
which is precisely the line `collection_failure()` exists to hold: partial
degradation succeeds with reduced completeness, total failure fails the job,
because there is nothing to reason over.

Track `outcome="cancelled"` separately; a user cancelling is not an error and
should be excluded from the denominator once you have enough volume to see it.

### 2. Investigation latency — p95 < 30 s

```promql
histogram_quantile(0.95,
  sum by (le) (rate(k8sagent_investigation_duration_seconds_bucket[28d]))
) < 30
```

A single investigation completed end to end in **0.223 s** at 500 pods on the
distributed deployment, of which `collect` is 65%. Thirty seconds is therefore
not ambitious — it is two orders of magnitude of headroom, deliberately, because
the histogram's tail is dominated by things the platform does not control: a
slow API server, a cluster on the other side of an ocean, a `logs` read on a
chatty container.

The bucket boundaries are `0.5, 1, 2.5, 5, 10, 30, 60, 120, 300`, so 30 s is a
real boundary and this quantile does not interpolate across a wide bucket. If
you tighten the target, tighten it to 10 s rather than to 20 s, for the same
reason.

Break the SLO down by phase when it burns:

```promql
histogram_quantile(0.95,
  sum by (le, phase) (rate(k8sagent_investigation_phase_seconds_bucket[1h])))
```

Expect roughly collect 65%, report 13%, analyse 11%, persist 10%. A shift in
that shape says more than the total does.

### 3. Submission availability — 99.9%

`POST /investigations` returning non-5xx. This is the one conventional
availability SLO and it gets the extra nine, because submission is cheap,
entirely within the platform's control, and its failure blocks the user from
even starting.

429s are **excluded** — a rate limit is the platform working correctly. 409
(`ClusterUnreachable`, naming the worker that holds the agent stream) is also
excluded: it is a correct refusal, and the alternative was reading a same-named
local kubeconfig context, which is the cross-tenant failure M8a exists to
prevent.

Serve this from your ingress or service mesh; the platform's own metrics do not
carry a per-route status series.

### 4. Evidence completeness — 95.0%

```promql
sum(rate(k8sagent_evidence_records_total{status=~"ok|empty"}[28d]))
/
sum(rate(k8sagent_evidence_records_total{status!="not_applicable"}[28d]))
```

`not_applicable` is excluded from the denominator, matching
`EvidenceStore.coverage()`. An undeployed Prometheus is not a gap — counting it
as one would permanently depress confidence in diagnoses that never needed
metrics.

This is the **leading indicator**: degradation shows up here before it shows up
in the success rate, because a partial collection still succeeds. A sustained
drop in completeness with a flat success rate means investigations are getting
thinner, not failing — which is the harder problem to notice and the one worth
an alert.

Watch `status="forbidden"` in particular. A rise means impersonated callers are
being refused by Kubernetes RBAC, which `app/kubernetes/access.py` will surface
to the user as a permissions problem rather than a broken cluster — but only
once refusals dominate. A low, steady forbidden rate is invisible in the product
and visible here.

### 5. Diagnosis soundness — grounding rejection rate < 5.0%

```promql
sum(rate(k8sagent_grounding_rejections_total[28d]))
/
sum(rate(k8sagent_llm_calls_total{outcome="succeeded"}[28d]))
```

**Of the answers the model actually gave, the share grounding rejected.** A
rejection is recorded once per answer refused, and an answer is a call that
succeeded, so neither side counts a diagnosis the model never wrote.

**It used to be `fallback / all diagnoses`, gated on
`k8sagent_llm_calls_total{outcome!="skipped"}` — and neither half held**, which
evaluating it against a real Prometheus showed the first time anyone did. The
platform never emitted `skipped`: an unconfigured model was recorded as a
*failed* call, so the gate matched everything, and every deployment running
without a model — a supported configuration — read **100% rejections** and
would have been paged by `GroundingRejectionRateHigh` permanently. And a model
*outage* also sends every diagnosis to the fallback, so the old ratio reported a
provider being down as the model fabricating citations — the same confusion
`evals.live` had to be taught out of. An unconfigured model now records
`skipped`; an outage is `llm_calls_total{outcome="failed"}`, an availability
signal rather than a soundness one; and with no answers in the window there is
no denominator and nothing to alert on.

This is a **two-sided** objective, which is unusual and is the point.

- *Rising* rejections mean the model is producing ungrounded output — fabricated
  citations, or prose contradicting the signals it cites. Falling back is the
  correct response, and the user still gets a complete deterministic diagnosis.
- *Zero* rejections over a long window is not obviously good. It may mean the
  grounding checks have been weakened into inertness. That failure is silent by
  construction: an over-strict check routes everything to the fallback and is
  loud, while an under-strict one simply accepts everything.

Measured live over 10 runs against a real cluster with `gpt-4o-mini`: **0%
false-rejection rate, 10/10 sound diagnoses accepted.** So 5% is a ceiling with
real headroom under it, not a guess.

Break down by reason when it burns — `k8sagent_grounding_rejections_total` is
labelled by a **closed category set**, never the raw reason, because the raw
reason quotes the model, which quotes cluster text, which is attacker-influenced.

### 6. Fleet visibility — 99.0%

```promql
sum(k8sagent_agents_connected) / <enrolled cluster count>
```

The denominator is not a metric — deliberately, since a per-cluster series is
exactly what the cardinality rule forbids. Take it from `agentctl list` or from
`agent_certificates`, and record it as a recording rule or a static target.

"Online" is **heartbeat-derived, not socket-derived**: the gateway pings every
15 s and `AGENT_STALE_SECONDS` (30, two missed heartbeats) decides staleness. An idle stream and a
half-open one look identical from the platform's side, so do not replace this
with "the stream is open".

Sum across workers. `agents_connected` is per-worker by necessity, and on more
than one replica the console reads the shared presence index rather than any one
registry.

### 7. Queue latency — depth below one worker's capacity, 99.0% of samples

```promql
sum(k8sagent_queue_depth) < sum(k8sagent_worker_capacity)
```

A proxy for wait time, which is not directly instrumented. Depth exceeding total
capacity means work is waiting rather than running.

Seven days rather than 28, because this is the objective that tells you to scale
and a monthly window is too slow to act on. **The action is to add workers, not
slots**: the ceiling is per worker process — scaling `JOB_MAX_CONCURRENT`,
agent processes and the Postgres pool each left throughput at ~12/s, while
workers 1→2 gave 12.1 → 23.0/s, linear. A saturated worker samples ~92% idle
with every non-idle sample in a socket wait, because one Python process
serialises HTTP, every agent's gRPC stream, the queue consumer and analysis.

Watch per-queue as well as in total. `queue_depth{queue="…"}` distinguishes the
shared queue from the per-worker affinity queues M8a introduced; depth on one
worker queue with an empty shared queue means routing is concentrating work on
the replica holding a busy cluster's agent stream.

---

## Error budgets

| SLO | Budget over 28 days |
|---|---|
| Success rate 99.0% | 1% of investigations |
| Latency p95 < 30 s | 5% of investigations above 30 s |
| Submission 99.9% | ~40 minutes |
| Completeness 95.0% | 5% of evidence records |
| Soundness < 5% rejections | 5% of diagnoses |

Suggested policy: at 50% burn, stop taking on collector or rule changes and
find the cause. At 100%, the next change to `app/collectors/` or `app/analysis/`
carries a live-cluster verification, not just a green suite — which is the
lesson this repository has now learned eight times.

## Alerting

Ship alerts on **burn rate**, not on instantaneous violation. A single failed
investigation is not a page; consuming a quarter of the monthly budget in an
hour is.

```yaml
- alert: InvestigationSuccessBudgetBurningFast
  expr: |
    (
      sum(rate(k8sagent_investigations_total{outcome!="succeeded"}[1h]))
      / sum(rate(k8sagent_investigations_total[1h]))
    ) > (14.4 * 0.01)
  for: 5m
  labels: {severity: page}

- alert: EvidenceCompletenessDegraded
  expr: |
    (
      sum(rate(k8sagent_evidence_records_total{status=~"ok|empty"}[6h]))
      / sum(rate(k8sagent_evidence_records_total{status!="not_applicable"}[6h]))
    ) < 0.95
  for: 30m
  labels: {severity: ticket}

- alert: QueueDepthExceedsCapacity
  expr: sum(k8sagent_queue_depth) > sum(k8sagent_worker_capacity)
  for: 10m
  labels: {severity: ticket}
  annotations:
    summary: "Add workers, not slots — the ceiling is per worker process."
```

These are correct from a cold start because **every label set is closed and
every series is seeded to zero at import**. Prometheus does not create a
labelled series until first use, so without seeding an alert on
`investigations_total{outcome="failed"}` reads "no data" while the platform is
healthy and fires on the *second* failure.

A fuller alert set is backlog item 40 and is not built.

## What has no SLO, and why

- **Report rendering and download.** Cheap, synchronous, and covered by
  submission availability. `/investigations/{id}/status` is 10.7 KB against the
  full endpoint's 784 KB at 500 pods, so polling cost is not a risk worth an
  objective.
- **Notification delivery.** Deliberately best-effort: delivery is detached,
  exceptions are dropped, and `announce()` returns nothing to await, precisely
  so no caller comes to depend on it. Guaranteed delivery would need a queue, a
  dead-letter and an ordering story — a different feature. An SLO here would
  promise something the design refuses to provide.
- **Rate limiter accuracy.** It fails *open* by design; a fixed window permits a
  short-term 2× ceiling, which is stated rather than hidden.
- **Detection recall against real faults.** The thing a buyer most wants
  promised, and the thing hardest to express as an SLI: it needs ground truth
  the platform does not have at runtime. It is measured instead in `evals/`
  (currently 20/20 golden, 100% recall over 47 expected signals) and gated in
  CI. Treat a recall regression there as an SLO breach with a different alarm.
