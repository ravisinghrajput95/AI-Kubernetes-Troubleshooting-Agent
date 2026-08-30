# Production Readiness

Findings from the 2026-07-26 engineering review, and their current state.

**The P0 section is closed.** That makes a controlled pilot defensible; it does
not make this a finished product — see P1, which still contains the items that
decide whether it survives a real incident at scale.

Scores at review time: architecture 7/10 · Kubernetes 5/10 · AI 6/10 ·
security 3/10 · performance 5/10 · maintainability 6/10 · OSS 2/10 ·
enterprise 3/10.

---

## P0 — blocks any deployment

### F13 · No authentication or authorization · **FIXED**

Every endpoint is unauthenticated. The service holds a kubeconfig, so anyone who
can reach the port has read access to everything that kubeconfig can reach, plus
the full archive of previous investigations. CORS is not a security control — it
constrains browsers, not `curl`.

**Fixed by:** pluggable authentication (OIDC against the provider's JWKS, static
tokens, or explicitly-acknowledged disabled), applied as a router-level
dependency so a new endpoint is protected by default. **Per-request Kubernetes
impersonation** means every cluster read runs as the calling user, so the
cluster applies their RBAC rather than the service account's — authentication
alone would decide *whether* you get in, not *what you can see*. History and
jobs are owner-scoped, and denial returns 404 rather than 403 so it does not
confirm an id exists.

**Authorisation followed** (M6.5): four roles per tenant (`viewer` /
`operator` / `admin` / `owner`) under the same row-level security, checked by a
single router-level dependency against a route → permission table in which **a
route with no entry is denied**. OIDC groups map to roles so the customer's IdP
drives it. Single-tenant deployments keep working unchanged via
`RBAC_DEFAULT_ROLE=admin`, which is refused above `viewer` when
`TENANCY_MODE=shared`. Thirteen mutation tests, all caught.

**Two defects fixed on the way:** `GET /investigations/{id}/events` had no
ownership check and streamed any investigation to any authenticated caller who
knew the id; and `require_principal` was a synchronous dependency, so M6's
ambient tenant never survived into the request — every tenant's rows were
written into `default` and readable by every other tenant, with row-level
security enabled, forced and correct.

**Rate limiting followed**: the operations that cost a customer's cluster and a
model call — exactly those needing `investigation.run` — are capped per caller
and, optionally, per tenant. The counter is shared across workers when
`REDIS_URL` is set, because a per-process limit on three replicas is three
times the configured rate. It fails *open* by design, unlike authorisation:
availability protection against a noisy caller, not a control against a hostile
one.

**Startup validation followed**: authentication was the only configuration
checked lazily, so an invalid one started the service and then failed every
request while `/health` stayed green. It is now validated in `build_state()`
through the same builder the dependency uses. And `docker-compose.yml` no
longer sets `ALLOW_INSECURE_NO_AUTH` for the operator — pre-setting it was the
careless deployment this finding describes, shipped in the repository.

**Remaining (P1):** none. `disabled` remains the *default mode*, but it now
costs a deliberate acknowledgement that nothing in this repository supplies and
that refuses the boot until given.

Phase timing closed the traces item, though not in the shape the finding
assumed: `k8sagent_investigation_phase_seconds` answers "where did the time go"
from a scrape, and OTLP export is blocked by `opentelemetry-proto` requiring
`protobuf<7.0` against this project's load-bearing 7.x pin. Cross-worker trace
correlation is the stated casualty. Taking the phase measurement immediately
corrected a published throughput figure, which is the return on it.

### F14 · Prompt injection reached operator-facing commands · **FIXED**

Verified exploitable: a hostile pod log line caused the model to emit
`kubectl delete ns kube-system` as a recommended command, and grounding accepted
the response because it cited a real signal. The operator is the execution path,
so the read-only executor did not mitigate it.

**Fixed by:** commands are never taken from the model (`_normalize` uses the
deterministic set); every surfaced command is classified by
`classify_command()`, unrecognised strings dropped and mutating ones labelled;
the prompt labels cluster text as untrusted data and no longer requests commands.
Regression tests in `tests/test_prompt_injection.py`.

### F20 · History index corruption and lost entries · **FIXED**

Read-modify-write with a non-atomic write. Concurrent saves lost entries; a crash
or full disk truncated the file; a parse failure **silently discarded all
history**.

**Fixed by:** temp-file + `os.replace()`, `fsync`, an in-process lock, and
quarantining a corrupt index instead of discarding it. Tests include a 12-thread
concurrency check.
**Remaining:** with the filesystem store, cross-process writers can still
race. Setting `DATABASE_URL` makes history a Postgres row and removes the race
entirely; the index-file path stays for single-process deployments.

### F15 · Redaction missed real credential shapes · **FIXED**

Four of eight shapes leaked: bare JWTs, connection-string passwords, AWS keys,
PEM private key blocks. These reached reports on disk, the API, and the model.

**Fixed by:** shape-based detectors alongside the keyword rules, and a corpus
test (`tests/test_redaction_corpus.py`) that also asserts benign log lines are
untouched. Verified end to end: hostile log content containing a JWT and a
connection-string password is scrubbed before it reaches the investigation
payload or the diagnosis.

**Residual (P2):** redaction happens *only* at the collection boundary. An
investigation dict assembled by any other route — a future import path, or
`regenerate` reading a report written before these detectors existed — is not
re-scrubbed. `PromptBuilder` re-runs the redactor so the model is covered, but
the API response and reports are not. Add a redaction pass at the persistence
boundary as defence in depth.

### F9 · Grounding validated provenance, not semantics · **FIXED**

A response could cite a genuine CrashLoopBackOff signal and still conclude
"Resolved - no action needed"; every id resolved, so it was accepted.

**Fixed by** three deterministic checks on top of citation integrity:
contradiction (reassurance language over severe signals), citation relevance (a
citation must support the selected hypothesis), and invented resources
(`namespace/name` references appearing in no evidence).

Deliberately lenient, because an over-strict check does not fail loudly — it
silently sends every investigation to the fallback. False-positive tests guard
the fallback rate alongside the rejection tests.

**Remaining:** `fix` and `prevention` are still model-authored prose. Commands
never are.

### F16 · Path traversal in report id handling · **FIXED**

Ids were interpolated into filesystem paths unchecked. **Not reachable over
HTTP** — Starlette rejected all four probes — so this was defence-in-depth, not a
live vulnerability.

**Fixed by:** id format validation plus a containment check on the resolved path.

### LICENSE missing · **FIXED**

Apache-2.0 added. Without it the work was legally unusable by any enterprise.

---

## P1 — blocks production

| # | Finding | Effort |
|---|---|---|
| — | ~~No audit logging~~ **fixed** (F17): append-only JSON lines recording actor, action, target, outcome, source IP and auth method, on a separate sink from application logging so a log-level change cannot silence it. Exercised in the live audit (§2, row 14). | done |
| F5 | ~~Unbounded all-namespace reads~~ **partially fixed**: API-server paging via `--chunk-size`, retained items capped, truncation recorded as an evidence gap. Peak *parse* memory is still proportional to cluster size — kubectl assembles the whole list before writing it, so removing that ceiling needs a streaming client. | 3d done, 5d remaining |
| — | ~~In-process job store: no HA, single worker mandated~~ **fixed** (M3): setting `DATABASE_URL` and `REDIS_URL` moves jobs, events and reports to Postgres and Redis, so any worker can serve any investigation and a crashed worker's job is reaped to a terminal state instead of hanging. The in-process store remains the default for single-process deployments. Not delivered: mid-run resumption — a lost worker's investigation is failed, not resumed. | done |
| — | ~~No platform self-observability~~ **partially fixed**: `/metrics` in Prometheus format, with the metric set chosen from `docs/PERFORMANCE_ENVELOPE.md` so every number that document tells an operator to act on is observable. No cluster, tenant, namespace or user is ever a label — cardinality and disclosure both forbid it — which is what makes the endpoint safe to leave unauthenticated. **Phase timing and 17 burn-rate alert rules followed** (`deploy/alerts/`), with every series and filtered label asserted against the real exposition. OTLP export is deliberately not built: `opentelemetry-proto` requires `protobuf<7.0` against this project's load-bearing 7.x pin, and cross-worker trace correlation is the stated casualty. | done, less OTLP |
| F11 | ~~No LLM eval harness~~ **largely fixed**: a golden corpus of 20 investigation cases plus 11 grounding cases gates reasoning accuracy, **detection recall** and grounding behaviour in CI (`docs/EVALUATION.md`). The model path has since been exercised live against a real cluster (10/10 grounded, 0 fabricated citations). Still missing: a provider abstraction, and live-model evaluation in CI rather than by hand. | 4d done, 1d remaining |

## P2 — blocks scale and adoption

| # | Finding | Effort |
|---|---|---|
| F1 | ~~Mark model-authored prose as untrusted in the UI~~ **done**: `ReportDocument` styles the model-authored sections distinctly and labels them; commands render as commands. |  done |
| F6 | ~~No RBAC preflight~~ **answered, not as asked**: the preflight described needs `kubectl auth can-i`, which is a *command*, and `ResourceRequest` deliberately cannot carry one — that closed verb set is what makes a request safe to send to a customer's cluster. The reads are cheap anyway (a 403 is immediate); what was expensive was the explanation. `app/kubernetes/access.py` recognises "nothing usable, refusals dominate" from evidence status and says whose RBAC is at fault, identically through both providers. | done |
| F18 | ~~No caching; every investigation re-reads the whole cluster~~ **fixed**: a TTL-bounded cache at the `ClusterProvider` seam, keyed on `(tenant, provider, cluster, impersonated identity)` — the identity because impersonation means the same read has different correct answers per caller. Measured against a real cluster: a second investigation spawns **13 kubectl processes instead of 70** and collects in **0.16 s instead of 0.57 s**. Failures are never stored; an alert-triggered investigation always refreshes; and **evidence built from a reused read carries the age of the read, not of the run**, so a citation still means what it says. Five mutations in `scripts/mutation_check.py`, all caught. | done |
| F19 | ~~Report files are never pruned~~ **fixed**: `ReportStore.prune()` deletes rendered artefacts past `REPORT_RETENTION_DAYS` (14) **and nulls `investigations.result` in the same transaction** — the larger copy, at 2.7 MB against a couple of hundred kilobytes, which previously survived retention so an expired investigation 404'd on `/pdf` while `GET /investigations/{id}` still served its whole contents. The history entry survives marked `expired`, because deleting it would make an investigation that happened look like one that never did. Deletes payloads on upgrade that were previously kept — see `docs/UPGRADE.md`. | done |
| F7 | ~~API version assumptions (EndpointSlice, Ingress) with no discovery~~ **fixed, and it was not the version that was wrong.** The kubeconfig path does its own discovery, so the assumption only lives in the agent — and every group version it hardcodes is GA on every supported Kubernetes release, which a discovery client would confirm at the cost of a startup dependency. The assumption lacked *evidence*, not machinery, so `verify_deployment.py` now checks all 24 entries against a live cluster's discovery document in the required CI job. What running it *did* find is worse and in the same tables: **eight of the deep-investigation reads named a resource the agent has no kind for** — including EndpointSlice and Ingress, `configmap` singular against a plural key, and StorageClass, VolumeAttachment, ResourceQuota, LimitRange and ServiceAccount entirely. Every one degraded silently, so an agent-reached cluster produced a shallower investigation than the same cluster read locally and nothing compared the two. Fixed in all three places it lived (`_KINDS`, `kinds.go`, and the ClusterRole the enrolment manifest ships), and pinned by `tests/test_provider_parity.py`, which derives the required set by *running* every collector rather than reading a list. Six mutations, all caught. | done |
| — | README documents a roadmap as if implemented; `docs/` is orphaned. | 1d |

## P3 — quality

`App.tsx` still ~1,050 lines · `FixRecommendationEngine` vestigial ·
`history_service.py` ~900 lines doing three jobs · kubectl subprocess-per-call
(**reduced, not removed**: F18 stops a *repeat* investigation paying for it —
70 processes to 13 — but a cold one still spawns one per read) ·
no frontend component tests · no runtime plugin discovery (entry points).

---

## Testing gaps

Present: 1,350 backend + 225 frontend tests covering unit, integration, API,
fault-injection, safety-property, contract, and opt-in live-transport — plus
backend tests that run only against real Postgres and Redis
(`K8S_AGENT_INTEGRATION=1`, and a dedicated CI job). **Reusable live-cluster
fault fixtures** now ship at `docs/qa/audit-faults.yaml` (nine faults plus a
healthy control) and `docs/qa/observability-faults.yaml`, and several test
fixtures are **captured from real clusters and backends** rather than
hand-written — which is what caught the defects hand-written ones could not.

**A required CI job now stands the platform up** on kind with ingress-nginx,
metrics-server, a prometheus-operator Prometheus and out-of-band Postgres and
Redis **and a real Go agent enrolled over mTLS**, installs the chart, and makes
45 assertions against the live deployment
(`scripts/integration_verify.sh`, `docs/INTEGRATION_VERIFICATION.md`). That
closes the gap every tier from §16 to §21 was actually found through: nothing in
CI had ever *run* the system against a real dependency, so a defect that needs a
second product to disagree with us — Prometheus's parser, nginx's buffering, the
kubelet's probe path — had no way to fail a build. Mutation-tested by reverting
`2f60f76` into a rebuilt image: 27/4 and exit 1 on the mutant, 32/0 restored.

**`K8S_AGENT_CLUSTER_INTEGRATION` is set by nothing** — not CI, not
`scripts/integration_verify.sh`. The differential agent suite (36 tests,
including M4's stated exit criterion that an agent-collected investigation
produces the same evidence as the same read performed locally, and the two
certificate-renewal tests that run the real Go binary against a real gateway)
therefore runs only when someone remembers. Running it found a shipped defect
within minutes: the agent mapped every 404 to `EMPTY`, a *usable* status, so an
absent metrics-server read as an idle cluster and raised the confidence of a
diagnosis that had seen less. Fixed, mutation-tested three ways, and pinned by
a Go test plus a tripwire in `tests/test_metrics_parity.py`. **The suite now
runs in the `integration-verify` job**, which builds the agent binary on the
host and fails unless most of the suite actually ran — a fully-skipped pytest
run exits 0, so the count is checked rather than the exit status. Wiring it in
also closed a hole in the suite itself: the agent binary has no `--context` and
followed the caller's *current-context*, so the differential comparison rested
on ambient kubectl state. The kubeconfig is now pinned, mutation-tested with a
decoy current-context (23 of 36 fail without it).

Renewal, specifically, is *not* the gap it was thought to be: it is covered by
`TestRenewalHappensAtTwoThirdsOfLife` (Go, arbitrary clock) and
`test_the_agent_renews_itself_without_dropping_the_stream` (real binary, real
gateway, 30-second certificate, asserting the **same session object** still
serves reads afterwards). Both pass. What blocked moving it into
`integration_verify.sh` was that `AGENT_CERT_TTL_HOURS` was an **integer in
hours**, so the shortest deployable certificate was one hour and its renewal
point forty minutes away — the harness sidestepped it by constructing
`AgentIdentityService(leaf_lifetime=timedelta(seconds=30))` directly, which
proves the mechanism but not that a deployment can be configured into it. **The
setting is now a float** (`AGENT_CERT_TTL_HOURS=0.025` is ninety seconds,
renewing at sixty; existing integer values parse unchanged), asserted on the
validity of a certificate `build_identity_service()` actually issues rather than
on the setting. So a CI check is now a matter of standing the deployment up with
that value and watching the serial change while the stream stays open — which
is work, but no longer blocked.

Missing: envtest / multi-version cluster fixtures · snapshot tests for the PDF
and Markdown renderers · load tests inside the suite (throughput and chaos are
opt-in scripts, not tests) · **automated** mutation testing — every invariant
added since the audit was mutation-tested by hand, which does not survive
inattention (**partly closed**: `scripts/mutation_check.py` re-runs six of
them in CI, pairing a defect that shipped with the test that must object to it
— what remains uncovered is mutations needing a live cluster, which the
integration job's own mutation record covers by hand) · **agent certificate
renewal**, the one part of the identity
lifecycle the job does not reach (it enrols, connects and revokes, but nothing
runs long enough for renewal at 2/3 of certificate life) · cross-host
scale-out, which needs workers on separate machines.

---

## What the review got right about the existing design

Calibration matters as much as criticism. These held up under scrutiny and
should not be redesigned:

- Evidence spine with deterministic ids and status-as-data
- Fault-isolated collector DAG with budgets
- Read-only enforcement by construction, including the mixed-verb `rollout` case
- Signal → hypothesis separation with declarative rules
- Playbook-as-planner (inherits isolation, redaction, concurrency for free)
- Deterministic remediation with stated risk, rollback, and required RBAC
- Degradation as citable data rather than silence
