# Integration verification — standing the platform up, in CI

Every significant defect found in this project's last seven tiers was found by
a person deciding to stand something up. Not by a test, not by CI, and never by
review. This document is about turning that habit into infrastructure, what the
resulting job can and cannot catch, and the decisions taken on the way.

## The defect class

Six shipped defects, one shape:

| Defect | What was correct | What disagreed |
|---|---|---|
| `/metrics` content type vs body (`2f60f76`) | 16 KB of correct exposition, 200, every series labelled | Prometheus's OpenMetrics parser: `data does not end with # EOF` |
| Four Prometheus query defects (M9.1) | queries parsed, returned `success` | kube-prometheus-stack, which drops `container_spec.*` |
| Helm probes on `/health` (§21) | `/health/ready`, `begin_drain()`, shutdown ordering | the kubelet, which polled a path that never goes false |
| the inert correlation-id patcher (§18) | the patcher, the scope, the middleware | loguru, which merges configured `extra` before the patcher |
| the gateway's localhost certificate (§21) | `AGENT_GATEWAY_DNS_NAMES` and everything reading it | Go's x509 verifier, on a name the chart never set |
| `agent_affinity` not pinning to the stream holder (§21) | M8a's routing, refusal and presence index | the queue, which took the job anywhere |

In each case the code is right, an in-process test asserts something true, and
a **second product** disagrees with us at a boundary that process cannot see.
No amount of unit testing reaches them, because the thing that disagrees is not
in the process.

This is not carelessness. It is that nothing in CI ever *ran* the system
against a real dependency, so the class had no way to fail a build.

## What was built

```bash
scripts/integration_verify.sh          # create, install, assert, destroy
scripts/integration_verify.sh --keep   # leave the cluster up
scripts/verify_deployment.py           # the assertions, against a live deployment
deploy/verify/                         # the environment: kind, deps, Prometheus, values
```

A kind cluster with ingress-nginx, metrics-server, the prometheus-operator, and
Postgres and Redis deployed **beside** the chart rather than by it — the chart
bundles neither, deliberately, so verifying it means supplying them the way an
operator does. Then `helm install` with the ingress, autoscaling and the
ServiceMonitor all enabled, which is precisely the set §21 recorded as "created
and correct, and serving nobody".

It is a script rather than a list of workflow steps so that **the thing CI runs
is the thing a laptop runs**. Every defect above was found by hand; a harness
reproducible only by pushing to a branch would not have found any of them
either.

`--kube-context` is pinned on every `kubectl` and `helm` invocation. During the
§21 rolling-upgrade work the current context silently switched to an unrelated
live GKE cluster mid-experiment, and three commands ran against it.

## The 32 assertions, and what each one is for

| Group | Asserts | Catches |
|---|---|---|
| Probes | the **live Deployment**'s probes resolve to `/health/live` and `/health/ready`, and a `preStop` hook exists | §21 defect 3 |
| Ingress | `/health`, `/health/live`, `/health/ready` (naming postgres and redis), 401 unauthenticated, `/me` resolving `operator` | an Ingress serving nobody; auth bypassed at the edge |
| `/metrics` | the body is what the `Content-Type` promises | `2f60f76`, named precisely |
| Scrape target | Prometheus has ≥1 target for our ServiceMonitor, all `up`, no `lastError` | `2f60f76`; a ServiceMonitor nobody selects |
| Alert series | all 15 `k8sagent_*` series the shipped rules reference are **in Prometheus**, by instant query | rules that evaluate forever and fire never |
| Investigation | 202, terminal `succeeded`, usable evidence > 0, a real PDF — all through the ingress | §21 defect 1 (`impersonate`); a green pipeline collecting nothing |
| SSE | ≥3 frames, arrival times spread across the stream | nginx buffering the live timeline into one blob |
| Counters | `sum(k8sagent_investigations_total)` **increased in Prometheus** | series that exist and carry nothing |
| HPA | `currentMetrics` resolves to a real utilisation, not `<unknown>` | §21's unexercised autoscaling |

Measured on the first full run: 32 passed, 0 failed, ~4 minutes of assertions
on top of ~4 minutes of environment.

## Keeping it honest

The recurring failure in this repository's harnesses is not a wrong assertion,
it is a **vacuous** one — `fleet_bench.py`'s "5 collections, 0 records"
produced entirely by an `AttributeError`; a drain scenario that reported PASS
while the process exited 0.2s after SIGTERM with nothing in flight; a chaos
scenario with no control. Each printed a confident number.

So every check here carries a guard against its own subject being absent:

- **Zero scrape targets is not "no unhealthy targets".** A ServiceMonitor that
  no Prometheus selects yields an empty list, and `all(t.health == "up")` over
  an empty list is `True`. The count is asserted first.
- **The alert-series check refuses fewer than 13 referenced series.** If the
  rules file moves or the regex stops matching, "all 0 referenced series are
  present" would pass. 15 are referenced today.
- **An SSE stream under 0.75s cannot answer the buffering question**, so the
  check reports a failure that says exactly that rather than a pass it did not
  earn. Measured at 1.22s with a 1.21s spread; buffered delivery would show
  ~0.
- **A `succeeded` investigation that collected nothing is refused.** Evidence
  coverage must show usable records.
- **Series presence is proven by instant query, not the label-values API**,
  which still lists a name ingested once and never again.
- **The harness runs as `operator`, not `owner`.** A caller holding every
  permission cannot tell a working permission table from an absent one.

## `serviceMonitorSelector` is restrictive on purpose

`deploy/verify/prometheus.yaml` selects ServiceMonitors on `release:
kube-prometheus-stack`, reproducing kube-prometheus-stack's shipped default
(`serviceMonitorSelectorNilUsesHelmValues=true`) rather than the permissive
`{}` an author reaches for when they want their harness to go green. The
chart's ServiceMonitor carries no such label unless
`metrics.serviceMonitor.labels` supplies one, and `deploy/verify/values.yaml`
supplies it — so the path an operator actually walks is the path under test.

The §21 live install used `serviceMonitorSelectorNilUsesHelmValues=false`,
which is why it could not have found this.

## Required, not opt-in

The job runs on every push and pull request, and its failure fails the build.

A job that is allowed to fail is the same as no job. It becomes noise inside a
week, and then nobody reads it — which is worse than absence, because it also
carries the appearance of coverage. This defect class has reached `main` four
times with a green suite; it is not one to detect optionally.

The cost is roughly 12 minutes of wall clock, in parallel with jobs that
already take five, so the critical path grows by about seven minutes.

## What belongs here, and what does not

`backend-integration` (`K8S_AGENT_INTEGRATION=1`) runs **our code** against
real Postgres and Redis, in one process, asserting **our** contracts — the job
store's claim semantics, the enrolment store's single-use `UPDATE`. It is
faster, more precise, and where an assertion belongs whenever it can be made by
importing `app`.

This job is for assertions that need a **second product to agree with us**, and
can only be observed from outside the pod. The dividing question is: *what
would have to be wrong for this to fail?* If the answer is "our code", it is a
pytest. If the answer is "our code, or Prometheus's parser, or nginx's
buffering, or the kubelet's probe path, or the operator's label selector", it
is here.

## Mutation-tested

An invariant that has never been observed failing is a hypothesis. So
`2f60f76`'s one-line fix was reverted, rebuilt into an image, rolled out, and
the job re-run:

```
$ git diff backend/app/observability/metrics.py
-from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
-from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest
+from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
+from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST
```

The diff was read before the run, not assumed: a mutation that fails to apply
reports "survived" identically to a missing test.

Prometheus, against the mutant:

```
http://10.244.0.18:8000/metrics | down | data does not end with # EOF
http://10.244.0.19:8000/metrics | down | data does not end with # EOF
http://10.244.0.20:8000/metrics | down | data does not end with # EOF
http://10.244.0.21:8000/metrics | down | data does not end with # EOF
```

The job, against the mutant — **exit 1, 27 passed, 4 failed**:

```
/metrics is what its Content-Type claims
  FAIL  an OpenMetrics content type is served an OpenMetrics body
The ServiceMonitor produces a healthy scrape target
  PASS  the ServiceMonitor was selected and produced targets  (4 targets)
  FAIL  every scrape target is up
  FAIL  no target reports a scrape error
Every alert-rule series is stored in Prometheus
  PASS  the alert rules reference series  (15 distinct series)
  FAIL  every referenced series is present in Prometheus
        15 of 15 series the alert rules depend on are not in Prometheus
```

Three things in that output are worth reading rather than skimming.

**The two honesty guards passed while the checks they guard failed**, which is
exactly right and is the only way to tell them apart. The ServiceMonitor *was*
selected and *did* produce four targets — the selector worked; the scrape did
not. A harness that collapsed those into one assertion would have reported the
same failure for a label typo and for a malformed body, and sent the reader to
the wrong place.

**"15 of 15 series are not in Prometheus" is the whole cost of the bug**, in one
line. That is 17 alert rules evaluating against nothing, which is what a
`down` target means downstream and what no local check could see.

**Everything else still passed.** The platform served health, authenticated,
ran an investigation to `succeeded` with 20 usable evidence records, rendered a
PDF, and streamed 29 SSE frames incrementally. The bug is invisible from every
direction except the one this job added — which is precisely why it shipped.

Restored, rebuilt and re-run: 32 passed, 0 failed.

## What this does not reach

Stated plainly, because a harness's reputation is made by what it claims not to
cover:

- **Real users and real production load.** Every number here comes from a
  single-node kind cluster with a handful of pods.
- **Cross-host scale-out**, which needs workers on separate machines. The
  existing `scaleout_bench.py` finding — flat past two workers on the
  kubeconfig path, because process spawning is a host resource — is unchanged
  and unmeasured across hosts.
- **The agent path.** This install is kubeconfig-only. The gateway, mTLS
  enrolment and M8a routing were exercised by hand in §21 and are not in this
  job; adding a Go agent build and a second image is the obvious next step.
- **Upgrades under traffic.** §21 measured this by hand across ten runs; the
  measurement is load-generator-shaped and does not fit a pass/fail assertion
  without a flakiness budget nobody has set.
- **Multi-node scheduling, PDBs under real disruption, and network policy.**
