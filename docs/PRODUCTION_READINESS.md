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

**Remaining (P1):** `disabled` mode still ships as the default, so a careless
deployment that sets `ALLOW_INSECURE_NO_AUTH` is unprotected.

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
| F17 | No audit logging (actor/action/target/outcome). Disqualifying for SOC2. | 2d |
| F5 | ~~Unbounded all-namespace reads~~ **partially fixed**: API-server paging via `--chunk-size`, retained items capped, truncation recorded as an evidence gap. Peak *parse* memory is still proportional to cluster size — kubectl assembles the whole list before writing it, so removing that ceiling needs a streaming client. | 3d done, 5d remaining |
| — | ~~In-process job store: no HA, single worker mandated~~ **fixed** (M3): setting `DATABASE_URL` and `REDIS_URL` moves jobs, events and reports to Postgres and Redis, so any worker can serve any investigation and a crashed worker's job is reaped to a terminal state instead of hanging. The in-process store remains the default for single-process deployments. Not delivered: mid-run resumption — a lost worker's investigation is failed, not resumed. | done |
| — | ~~No platform self-observability~~ **partially fixed**: `/metrics` in Prometheus format, with the metric set chosen from `docs/PERFORMANCE_ENVELOPE.md` so every number that document tells an operator to act on is observable. No cluster, tenant, namespace or user is ever a label — cardinality and disclosure both forbid it — which is what makes the endpoint safe to leave unauthenticated. Traces are still absent. | 2d done, 1d remaining |
| F11 | ~~No LLM eval harness~~ **partially fixed**: a golden corpus of 21 cases gates reasoning accuracy and grounding behaviour in CI (`docs/EVALUATION.md`). No provider abstraction yet, and no live-model evaluation. | 3d done, 2d remaining |

## P2 — blocks scale and adoption

| # | Finding | Effort |
|---|---|---|
| F1 | `fix`/`prevention`/`next_steps` are still model-authored prose (commands are not). Mark as untrusted in the UI. | 1d |
| F6 | No RBAC preflight — work is done before permission failures surface. | 1.5d |
| F18 | No caching; every investigation re-reads the whole cluster. | 1.5d |
| F19 | Reports embed full investigations; report files are never pruned. | 2d |
| F7 | API version assumptions (EndpointSlice, Ingress) with no discovery. | 1d |
| — | README documents a roadmap as if implemented; `docs/` is orphaned. | 1d |

## P3 — quality

`App.tsx` still ~1,500 lines · `FixRecommendationEngine` vestigial ·
`history_service.py` ~900 lines doing three jobs · kubectl subprocess-per-call ·
no frontend component tests · no runtime plugin discovery (entry points).

---

## Testing gaps

Present: 560 backend + 47 frontend tests covering unit, integration, API,
fault-injection, safety-property, contract, and opt-in live-transport — plus
39 backend tests that run only against real Postgres and Redis
(`K8S_AGENT_INTEGRATION=1`, and a dedicated CI job).

Missing: real
cluster fixtures (kind/envtest, multi-version) · snapshot tests for the PDF and
Markdown renderers · load tests (no test exceeds one pod) · frontend component
tests · automated mutation testing.

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
