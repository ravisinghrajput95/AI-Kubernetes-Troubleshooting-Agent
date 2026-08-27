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

## The assertions, and what each one is for

| Group | Asserts | Catches |
|---|---|---|
| Probes | the **live Deployment**'s probes resolve to `/health/live` and `/health/ready`, and a `preStop` hook exists | §21 defect 3 |
| Ingress | `/health`, `/health/live`, `/health/ready` (naming postgres and redis), 401 unauthenticated, `/me` resolving `operator` | an Ingress serving nobody; auth bypassed at the edge |
| `/metrics` | the body is what the `Content-Type` promises | `2f60f76`, named precisely |
| Scrape target | Prometheus has ≥1 target for our ServiceMonitor, all `up`, no `lastError` | `2f60f76`; a ServiceMonitor nobody selects |
| Alert series | all 15 `k8sagent_*` series the shipped rules reference are **in Prometheus**, by instant query | rules that evaluate forever and fire never |
| Investigation | 202, terminal `succeeded`, usable evidence > 0, a real PDF — all through the ingress | §21 defect 1 (`impersonate`); a green pipeline collecting nothing |
| SSE | ≥3 frames, arrivals tracking the platform's own emission times | a live timeline that arrives in one delivery at the end. **Not** `X-Accel-Buffering` — see below |
| Counters | `sum(k8sagent_investigations_total)` **increased in Prometheus** | series that exist and carry nothing |
| HPA | `currentMetrics` resolves to a real utilisation, not `<unknown>` | §21's unexercised autoscaling |
| Agent link | a real in-cluster agent, enrolled through `POST /agents/enrolment`, connects over mTLS with `identity_source: certificate`; investigations **submitted on the stream holder** all reach it | §21 defects 5 and 6 — a gateway certificate naming only localhost, and affinity not pinning work to the holder |

Measured on a full local run — build both images, create, install, enrol,
assert, destroy: **45 passed, 0 failed**, and the same in CI in about 4½
minutes. The exact count moves by a check or two depending on which branches
run, which is why the groups above are the contract and the number is not.

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
- **The SSE check compares against a control taken from the same run.** Every
  frame carries the server-side moment it was emitted, so the assertion is that
  *arrivals track emissions*, and the threshold scales with the window the
  platform actually used. The only absolute left is on what the *platform* did
  (it must have emitted over ≥0.20s), not on how fast the machine ran — a
  distinction that cost a red build, below. **And the check claims less than it
  was built to claim**: it does not pin `X-Accel-Buffering`, which mutation
  testing established rather than assumed. Also below.
- **A `succeeded` investigation that collected nothing is refused.**
  Evidence coverage must show usable records.
- **Series presence is proven by instant query, not the label-values API**,
  which still lists a name ingested once and never again.
- **The harness runs as `operator`, not `owner`.** A caller holding every
  permission cannot tell a working permission table from an absent one.

## The first CI run failed, and the reason is worth keeping

31 of 32 passed. The one failure was the SSE check's own honesty guard, on a
completely healthy platform: the stream lasted **0.73s against a 0.75s floor**,
so the check refused to conclude — and refusing to conclude was wired as a
failure.

The floor had been chosen from a local measurement of 1.22s, on the assumption
that a GitHub runner would be *slower* than the laptop. It was faster. That
assumption was never checked, and it is the whole defect: **an absolute
threshold on a machine-dependent quantity is a coin flip wearing a rigorous
face.** A 1.6× margin over a number that varies with the host is not a margin.

The fix is not a lower floor, which only moves the cliff. Each SSE frame
carries the server-side moment the platform emitted it, so the check now
measures whether arrivals *track* emissions and scales its threshold to the
window the platform actually used. A fast machine shrinks both sides together;
buffering collapses the ratio regardless. The one remaining absolute is on the
platform's own emission window, where a degenerate value is worth failing on in
its own right.

Two things this says about the harness rather than the platform. The guard
worked — it caught an inconclusive measurement instead of reporting a pass it
had not earned, which is exactly what it is for. And the job earned its keep on
its first run by failing on its own weakest assumption rather than on the
product.

## The SSE check does not pin the header it was written to pin

Recorded because it is the same defect class as everything else here, found in
the harness built to catch it.

The check was written to catch removal of `X-Accel-Buffering: no`
(`app/api/investigate.py`), the header that defeats proxy buffering. Mutation
testing it — deleting the header, rebuilding, rolling out — produced **33/33,
arrivals tracking emissions at 99%**. Turning `proxy-buffering: "on"` for the
vhost and repeating gave 33/33 and 99% again.

Two reasons, both about nginx:

- ingress-nginx ships `proxy_buffering off` globally, so the header is
  redundant on a default install.
- `proxy_buffering on` does not hold a response until it completes. nginx
  forwards as buffers fill, so at this traffic shape — 29 frames, ~8 KB, over
  1.3s — delivery looks identical either way.

The shape where the header earns its keep is a long investigation emitting
sparse events. This harness cannot produce that without an artificially slow
investigation, and engineering the scenario to make the assertion true would be
testing nginx's tuning rather than our code.

So: the header stays, because other proxies honour it and it costs nothing; the
buffering annotation stays, because holding under the stricter configuration is
worth something; and the check now claims only what it verifies — that SSE
reaches a client incrementally, end to end, through a real proxy. That is a
genuine property, and one `TestClient` cannot check at all, since it buffers
streamed responses.

**A mutation-surviving assertion that reads as a guarantee is worse than an
honest description of a narrower one.** The first version was the former for
about an hour.

### And one plain own goal

Between those two fixes the job was also broken by a change that improved
nothing: swapping `helm/kind-action` for a hand-rolled `curl` install, to be
rid of a harmless `No such container: kind-registry` line its post-step logs.
`/usr/local/bin` needs root on a runner, so `chmod` failed with "Operation not
permitted" and the job never reached a single assertion.

Worth recording next to the other two because it is a different mistake and a
more ordinary one: not a wrong assumption about measurement, just churn against
a working, maintained step for a cosmetic gain, pushed without any way to check
it — in the session whose entire subject is not doing that.

## The agent leg, and the three harness defects it found

The agent link was the largest surface this job did not reach, and the one
where §21 found two defects of exactly the class it exists to catch. Closing it
found one more product defect and three defects in the harness itself.

**The chart never set `AGENT_GATEWAY_ADVERTISE`.** Unset, the platform renders
the enrolment endpoint as the literal `<platform-host>:9443` — so every
manifest a chart-deployed `POST /agents/enrolment` generates, including the one
the console's `/connect` page hands an operator, carries a placeholder instead
of an address. Third instance of this exact shape in this chart, after the
probe paths and the gateway's own DNS names: the knob existed and nothing
turned it. Now derived from the release's gateway Service, and the verifier
refuses an enrolment whose endpoint contains a `<`.

The manifest is taken from the endpoint rather than kept in this repository, for
the same reason the observability fixtures are captured from a live backend: a
checked-in copy would drift from what the platform emits, and the harness would
verify itself.

### The routing check that passed against the bug

The first version submitted its investigation through the ingress. Against a
rebuilt image with §21's affinity fix reverted, it passed **6 times out of 6**.

On four replicas that is not luck. Three quarters of submissions land on a
worker that does *not* hold the stream, where `holder()` answers correctly and
routing works. The defect only bites when the submission lands on the holder —
`holder()` deliberately returns nothing for a record naming *this* worker, and
`agent_affinity` had no local-registry check, so the job fell through to the
shared queue.

Submitted on the holder, the same mutant failed **3 of 4**, each with *"attached
to worker &lt;the worker that just accepted it&gt;"*. The check now submits from
inside the holder pod via `kubectl exec`, three times: under the mutation each
round has a one-in-four chance of accidentally succeeding, so three rounds put
survival below 2%; under the fix a submission on the holder always routes to
itself.

Worth stating precisely, because it is easy to get wrong: §21's "1 of 3 before"
was **no routing at all**, not this mutation. Reverting only the local-registry
check is the subtler half and mis-routes about 19% of ingress-submitted
investigations. That gap is exactly why the naive check looked healthy.

### The harness crashed instead of reporting

While correctly detecting that defect, the run died on `None > 0`: it read
`usable` from an investigation that had been *refused*, where the key is absent.
No summary, no failure list, and an exit code whose meaning depended on where it
happened to die. The finding survived only because it had already been printed.

That is `fleet_bench.py`'s "5 collections, 0 records" in a new costume. Every
check group now runs under `guarded()`, which turns an exception into a
recorded failure — a check that could not run has not passed, and a verdict
beats a stack trace.

### An assertion inherited from a fact this milestone deleted

The agent check first required the agent's logs to show it serving collections.
The agent does not log that. §21 read collection activity out of client-go's
`client-side throttling` warnings — which **this milestone's own `--api-qps`
fix removed**. An assertion inherited from an observation whose cause had since
been fixed, and it could never have passed.

Replaced with two things that are true and independent of the platform's own
account: the agent's own mTLS `connected` line, and a control proving no
kubeconfig context named after the agent's cluster exists — so a succeeded
investigation of it could not have been answered locally.

### Revocation, mutation-tested because it is a security control

`AgentGateway._sweep_revocations()` is a background task on a timer, which is
the shape that goes inert without anything noticing — the same family as the
correlation-id patcher that was correct, called, and produced a constant. A
transport built around a stream that stays open for weeks makes
revocation-at-reconnect close to meaningless, so the sweep is the control, not
the connect-time check.

The check revokes the agent's certificate with `agentctl` and asserts it stops
serving. **Its control is the check immediately before it**: three
investigations already reached this agent, so "it no longer serves" means
revocation did something. Without that pairing an agent that had never worked
would pass identically — a chaos scenario with no control, which §18 recorded
as the way to get a confident number out of nothing.

Mutation tested by making `_sweep_revocations` return immediately, rebuilt into
an image and rolled out:

```
Revocation ends a live stream
  PASS  the certificate is revoked
  FAIL  the revoked agent stops serving investigations
        the agent kept collecting after its certificate was revoked
  FAIL  the gateway logged that it ended the stream
```

**43 passed, 2 failed, exit 1**; restored, 45/0. Note the first line still
passes: revoking *succeeded*, and the certificate was recorded as revoked. Only
the live stream ignored it — which is exactly the gap between a revocation
list and revocation taking effect, and the reason this check asserts behaviour
rather than the store's contents.

A failing run of this check costs ~90s more than a passing one, because it
waits out its deadline rather than returning on the first observation. Worth
knowing for the CI budget: a genuine regression here is the slowest failure the
job has.

### An observation from the revocation run, not a defect

After revocation the investigation came back `status=failed
provider=kubeconfig`. It did not succeed, which is what the check asserts, but
note *how* it failed: the platform fell back to `LocalKubectlProvider` and
failed there only because no local context is named after that cluster.

That is the documented M8a behaviour — `select_provider` refuses outright only
when the presence index names *another* worker, and a revoked agent leaves no
presence record at all. So the platform has no way to know that a cluster was
ever agent-only. On a deployment where a kubeconfig context happens to share
the name, a revoked agent's cluster would be read locally instead: exactly the
"one customer's `prod` answered by another's" risk the refusal message
describes, arriving through the one door the refusal does not cover.

Recorded rather than fixed. Closing it means a durable "this cluster is
agent-served" fact, which is a design decision about what the platform
remembers, not a wiring bug — and inventing one here would be scope this
harness is not entitled to take.

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

Those totals are the suite as it stood for that experiment — 32 checks, one of
which (the Prometheus counter read) is skipped when scraping is broken, hence
27+4. It has 45 now. The numbers are left as they were measured rather than
rescaled, because a mutation record that drifts with later work is no longer a
record of anything.

## What this does not reach

Stated plainly, because a harness's reputation is made by what it claims not to
cover:

- **Real users and real production load.** Every number here comes from a
  single-node kind cluster with a handful of pods.
- **Cross-host scale-out**, which needs workers on separate machines. The
  existing `scaleout_bench.py` finding — flat past two workers on the
  kubeconfig path, because process spawning is a host resource — is unchanged
  and unmeasured across hosts.
- **Agent certificate renewal.** The agent leg enrols, connects and is
  revoked, but nothing runs long enough to reach renewal at 2/3 of certificate
  life — the one part of the identity lifecycle still unexercised here.
- **Upgrades under traffic.** §21 measured this by hand across ten runs; the
  measurement is load-generator-shaped and does not fit a pass/fail assertion
  without a flakiness budget nobody has set.
- **`X-Accel-Buffering: no`.** Nothing in the repository pins it — see above.
- **Multi-node scheduling, PDBs under real disruption, and network policy.**
