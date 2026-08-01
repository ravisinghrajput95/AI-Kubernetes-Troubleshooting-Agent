# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Backend (run from `backend/`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend (run from `frontend/`):

```bash
npm install
npm run dev      # Vite dev server on :3000
npm run build    # tsc -b && vite build — the only type-check gate in the repo
```

Frontend tests (from `frontend/`):

```bash
npm test                                    # vitest run (hermetic)
npm test -- src/lib/analysis.test.ts        # single file
npm test -- -t "falls back to polling"      # single test

# Opt-in transport tests against a running backend:
VITE_API_INTEGRATION=1 react_PUBLIC_API_BASE_URL=http://127.0.0.1:8778 npm test
```

`http.integration.test.ts` is skipped unless `VITE_API_INTEGRATION` is set, so the default suite stays hermetic. It exists because the unit tests mock `fetch` — which proves the logic but not the wire, including FastAPI's `{"detail": ...}` error bodies.

Backend tests (from `backend/`):

```bash
pip install -r requirements-dev.txt
python -m pytest                       # whole suite
python -m pytest tests/test_collection_scheduler.py -k timeout   # single test
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. Tests use a fake `KubectlExecutor` subclass and never touch a real cluster.

```bash
# Opt-in tests against real Postgres and Redis (same precedent as VITE_API_INTEGRATION):
docker compose up -d postgres redis
K8S_AGENT_INTEGRATION=1 python -m pytest
```

Without that variable the distributed-store tests skip, so `python -m pytest` **never needs a database**. There is deliberately no fake Postgres and no SQLite stand-in: the store depends on `jsonb`, `bigserial` and a conditional UPDATE for claiming, so a substitute would prove the tests pass rather than that the store works. `tests/test_job_store_contract.py` runs the *same* assertions against both stores, which is what stops them diverging.

```bash
python -m evals    # reasoning + grounding regression report
```

`evals/` is a golden corpus enforced by `tests/test_evals.py` and printed in CI. It exists because rules, prompts and grounding checks can all change without breaking a unit test while making the platform worse at reasoning. **The grounding corpus must keep cases that are expected to be *accepted*** — a corpus of only-rejections passes while an over-strict check has silently routed every investigation to the deterministic fallback. See `docs/EVALUATION.md`.

Docker: `docker compose up --build` starts the backend, console, Postgres and Redis. The image installs a pinned `kubectl` and compose mounts `~/.kube/config` read-only (override with `KUBECONFIG_FILE`). `docker compose up --scale backend=3` is the multi-worker demonstration. Local processes remain the getting-started path and need none of it.

Lint and format (from `backend/`, config in `ruff.toml`):

```bash
ruff check .            # CI fails on any finding
ruff format --check .   # CI enforces formatting
```

`.github/workflows/ci.yml` runs lint, format, backend tests on Python 3.12/3.13, frontend build and tests, a dependency audit, a secret scan, and both Docker builds.

`requirements.txt` pins are kept current and audited — `pip-audit --strict` runs in CI and **fails the build**, because the original pins shipped known CVEs in PyJWT (which validates auth tokens) and Starlette.

**Security status:** there is no authentication on any endpoint. See `SECURITY.md` and `docs/PRODUCTION_READINESS.md` — do not deploy against a production cluster.

`PyYAML` is a runtime dependency: generated patches are applied to production clusters, so YAML is serialised properly rather than string-formatted.

## Runtime requirements

- `kubectl` must be on PATH of the backend process **when a cluster is read locally** — which is the default. A cluster reached through its agent needs no kubectl on the platform at all; `LocalKubectlProvider` is the only thing that shells out.
- `OPENAI_API_KEY` (optional). Without it the LLM call fails cleanly and the deterministic fallback diagnosis is used — the app stays fully functional, just with `ai_generated: false`.
- Config is `pydantic-settings` in `app/core/config.py`, read from env or `backend/.env`: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-4o-mini`), `KUBECONFIG_PATH`, `KUBECTL_TIMEOUT_SECONDS`, `LLM_TIMEOUT_SECONDS`.
- `InvestigationHistoryService` renders reports and hands the bytes to a `ReportStore`. The default `FilesystemReportStore` writes to `Path("data")/"investigations"` — a **relative** path, so the backend must be started from `backend/` or history lands in the wrong directory. With `DATABASE_URL` set, reports are Postgres blobs and the working directory stops mattering.
- `DATABASE_URL` + `REDIS_URL` select durable state (see *State backends* below). Both unset is the single-process default; exactly one set is **refused at startup**.

## Architecture

`run_investigation()` (`app/services/investigation_runner.py`) is the single pipeline both APIs use, so the sync and async paths cannot drift. It runs, in order:

1. `InvestigationService.run()` (`app/services/investigation_service.py`) — drives the collector graph, then derives the investigation payload from the resulting evidence.
2. `RootCauseAnalyzer.analyze()` (`app/ai/root_cause_analyzer.py`) — the reasoning layer.
3. `InvestigationHistoryService.save()` (`app/services/history_service.py`) — persists PDF + JSON + Markdown reports.

Steps 2 and 3 block, so both are dispatched via `asyncio.to_thread` to keep the event loop free.

### Two entry points

| Endpoint | Behavior |
|---|---|
| `POST /investigate` | Runs to completion and returns the full result. Retained for backward compatibility. |
| `POST /investigations` | 202 + id, runs in the background. `GET /investigations/{id}` for state, `/events` for SSE, `/cancel` to abort. |

**The job id is the investigation id.** `history_service.save()` takes an optional `investigation_id`, so the finished report is stored under the id returned at submit and `/investigations/{id}/pdf|json|markdown|report` resolve without a second id space. `GET /investigations/{id}` falls back to the persisted report when the job has been evicted or the process restarted.

`collection_failure()` draws the line between the two failure modes: **partial** degradation (one collector down) succeeds with reduced completeness; **total** failure (zero usable evidence) fails the job, because there is nothing to reason over and reporting it as success would misrepresent it. The sync endpoint deliberately keeps its old contract — 200 with `health.status == "error"` — so the existing frontend still renders the message.

### Tenancy (`app/tenancy/`, migration `003`)

| `TENANCY_MODE` | Behaviour |
|---|---|
| `single` (**default**) | one implicit tenant, nothing to isolate, no behaviour change |
| `shared` | tenant on every row, enforced by Postgres row-level security |

`shared` without `DATABASE_URL` or without authentication is **refused at startup** — there is no in-memory equivalent of RLS, and every caller being anonymous means every caller is the same tenant.

**The tenant is ambient, not an argument.** A `ContextVar` set when the caller is authenticated; `Database.cursor()` emits `set_config('app.current_tenant', …, true)` on every transaction (`SET LOCAL`, so a pooled connection cannot carry it to the next request). Tenanted tables default `tenant_id` to that setting and have a policy comparing against it, so **no store method mentions a tenant and none had to change**. A `SELECT` with no `WHERE` returns only the caller's rows.

`ContextVar` specifically because asyncio copies the context at task creation: an investigation submitted by a request keeps that request's tenant after the request returns.

**The one thing that would make all of it inert: connecting as a superuser.** `ENABLE`/`FORCE ROW LEVEL SECURITY` were both set and correct, and every tenant could still read every row, because superusers and `BYPASSRLS` roles skip policies entirely — a deployment with no isolation and no symptom. `Database.assert_row_level_security_applies()` refuses to start `shared` on such a role. `tests/test_tenancy.py` connects as an unprivileged role for exactly this reason; run as `postgres` its isolation assertions all pass while proving nothing.

`system_scope()` is the one deliberate hole — the queue consumer and reaper cannot know a tenant before reading the row that names one. A test asserts `jobs/consumer.py` is its only user.

Agent identities carry their tenant in the SPIFFE path (`spiffe://<domain>/tenant/<t>/cluster/<id>`); the untenanted M4b form still parses as `default`. `AgentRegistry` is keyed by `(tenant, cluster)`, so two customers may both call a cluster `prod` without either evicting or reaching the other.

### State backends (`app/state.py`, `app/persistence/`)

One decision, made once at startup, and the only place either deployment is named:

| `DATABASE_URL` / `REDIS_URL` | Store | Reports |
|---|---|---|
| both unset (**default**) | `InMemoryJobStore` | local disk |
| both set | `PostgresRedisJobStore` | Postgres blobs |
| exactly one set | **startup fails** | — |

The single-process default is *supported*, not a dev-only fallback: `app/persistence/` is imported lazily, so nothing loads `psycopg` or `redis` unless configured. Do not make a database mandatory — `uvicorn app.main:app --reload` against nothing but a kubeconfig is the getting-started path.

**The governing rule: Redis is the latency layer, Postgres is the truth.** Every message has a committed row behind it — a queued id is a `pending` row, a cancel is a committed `cancel_requested`, an event is an inserted row carrying the sequence the message quotes. If Redis drops everything the system is slower, never wrong. Do not add a Redis-only fact.

Migrations are numbered forward-only SQL in `app/persistence/migrations/`, applied under `pg_advisory_lock` so N replicas booting together cannot race. Not Alembic: there is no ORM, so autogenerate — its whole value — does not apply. No downgrades by design; a bad migration is fixed by the next one.

### Jobs (`app/jobs/`)

`JobStore` (`app/jobs/base.py`) is the seam; both implementations satisfy it and no API handler knows which it has. `publish()` never blocks — a subscriber that is not draining loses events rather than stalling the investigation.

Three things are easy to regress:

- **`subscribe()` opens the live subscription *before* reading the backlog**, then de-duplicates by sequence. Subscribe-first is what makes it impossible to drop an event published during the read; the sequence filter (`EventSequencer`) is what makes it impossible to then deliver that event twice. Swapping the order looks harmless and silently loses events. The sequence is also the SSE frame id, so `Last-Event-ID` resumes a broken stream.
- **Cancellation is a message, not a method call.** `Task.cancel()` only works in the owning process. `/cancel` commits `cancel_requested` and publishes on the Redis control channel; the owning worker's runner turns that back into a local cancel. A per-job watchdog polling the committed flag is the backstop — it is what makes cancellation a *guarantee* rather than best effort, and it must not be removed because "the message already handles it". Both paths are pinned by tests that disable the other one.
- **A claim is a conditional UPDATE** (`WHERE status = 'pending'`), which is the mutual exclusion. Two workers may pop the same id; only one UPDATE matches.

`start_investigation_job` and `cancel_investigation` **must stay `async`** — unchanged conclusion, new reason: they touch the loop thread (`asyncio.create_task`, listener dispatch) and must not block it.

Scope boundary: M3 gives durable, correctly-terminated records, **not mid-run resumption**. A dead worker's job is reaped to `failed` via lease expiry. Resuming half-collected work would be a re-run, and needs ADR-007's state machine.

Progress flows through the `ProgressReporter` protocol on `CollectionContext` (default `NullProgressReporter`, so sync runs stay silent). Collectors carry a `label` used as the progress message — the generic scheduler must not gain a mapping of Kubernetes collector ids.

`JobConsumer` (`app/jobs/consumer.py`) runs only in the distributed deployment: a queue loop that claims and runs, a control loop that delivers cancels, and a reaper that fails jobs whose lease expired and re-offers ones the queue lost. An **idle** consumer is the normal state — `RedisBus` gives its socket deadline headroom over the blocking read, because redis-py defaults both to five seconds and the collision crashed the loop every cycle.

Testing note: `TestClient` must be used as a context manager (`with TestClient(app) as c`). Without it the event loop is torn down per request and background jobs are cancelled mid-run. `TestClient` also buffers streamed responses, so it verifies SSE framing but not progressive delivery — that needs a real server.

### Evidence layer (`app/evidence/`)

Every collected fact is an `Evidence` record with a **deterministic id** (`kind:target.key`), a `status`, and its originating command. This is the citation spine: conclusions reference evidence ids rather than copying payloads. `EvidenceStatus.usable` covers `OK` and `EMPTY`; everything else (`UNAVAILABLE`, `FORBIDDEN`, `TIMEOUT`, `NOT_APPLICABLE`, `FAILED`) records *why* a fact is missing, so a degraded backend becomes citable data instead of an exception. `EvidenceStore.coverage()` reports completeness and is the intended input for evidence-completeness confidence scoring.

### Cluster access (`app/providers/`)

`ClusterProvider` is the engine's **only** route to a cluster, reached through `CollectionContext.provider` / `context.fetch(request)`. A collector describes *what* evidence it needs as a `ResourceRequest`; the provider decides *how* to obtain it. `LocalKubectlProvider.to_args()` is the single place `ResourceRequest` → argv exists — a remote-agent provider replaces that translation and nothing above it changes. See `docs/ENTERPRISE_ARCHITECTURE.md` ADR-003.

`ReadVerb` is a **closed enum** (`get`/`describe`/`logs`/`top`/`config`) and `ResourceRequest` has no field that can carry a command, a flag string, or a shell fragment. That is the security property, not a validation step: a hostile value lands as one argv element and can never become the verb. It is what makes it safe to send a request to a remote agent. `tests/test_providers.py` pins both the translation table and this property.

**Every collector is on the seam, and `raw_executor()` no longer exists** (M5). It was the migration hatch for collectors that still built kubectl argv; with the inspectors migrated it is gone from the protocol and from both implementations, which is what makes "the engine cannot tell which provider it has" true rather than intended. `tests/test_providers.py::test_the_migration_escape_hatch_is_gone` pins it.

`select_provider()` (`app/services/investigation_service.py`) makes the choice: an agent connected for this cluster wins, otherwise the local kubeconfig. The registry is per-process, so a cluster whose agent is connected to another worker falls back to local — correct, and *visible*, via `investigation["cluster_access"]`. Routing to the worker holding the stream is M8.

### The cluster agent (`/agent`, `app/gateway/`, `app/providers/remote_agent.py`)

A Go binary, one per cluster, that **dials out** to the platform and answers evidence requests on that connection. No inbound port is opened into a customer cluster — that is the binding constraint the transport is shaped around (ADR-004), not throughput or schema.

Run it: `cd agent && go build ./cmd/agent`. The gateway is off unless `AGENT_GATEWAY_PORT` is set, and `app/gateway/` is imported lazily so a local-kubeconfig deployment never loads grpc.

Four things here are load-bearing:

- **The agent refuses a kind it does not know** (`agent/internal/policy`). The platform names a *kind* of evidence; it cannot describe an operation. This is a security control rather than validation, and it is why the agent is a separate process at all — enforced only on the platform, the read-only guarantee would be a promise the customer cannot verify. Mutation-tested in Go.
- **Raw JSON, not typed objects.** client-go's typed structs drop unknown fields and reorder keys, which would make an agent's evidence differ from the same read performed locally. Raw reads keep the two comparable and still avoid subprocess-per-call.
- **Correlation is on the envelope, not the record.** `EvidenceEnvelope` carries `request_id`; `EvidenceRecord` stays the storage and audit format, which has no such thing.
- **kubectl rewrites list envelopes.** `kubectl get pods -o json` returns `kind: List`; the API server returns `PodList`. Evidence is therefore compared on objects, never bytes — see `tests/test_agent_transport.py`.

`K8S_AGENT_CLUSTER_INTEGRATION=1` runs the differential suite against a real cluster (`kind create cluster --name m4b`); it skips otherwise, so `python -m pytest` still needs nothing.

### Agent identity (`app/security/`, `app/gateway/identity.py`, `agent/internal/identity/`)

**The certificate is the identity.** An agent names itself exactly once — in `Register`, where a single-use token has already bound that name — and never again. Every `Connect` stream is placed by reading the peer certificate, carried as a URI SAN in SPIFFE form (`spiffe://<trust-domain>/cluster/<id>`; the CN is for humans and is never trusted).

Five things here are load-bearing:

- **`AgentHello.cluster_id` cannot override the certificate, and a contradiction aborts the stream** with `PERMISSION_DENIED` naming both values. Silently preferring the certificate is defensible against an attacker but not against a mistake: a wrong `--cluster` flag would file evidence under one name while the agent's own logs said another, forever. An *empty* hello is fine — the certificate supplies it.
- **The CSR contributes a public key and nothing else.** Subject, SANs and extensions are discarded; the leaf is built from the token's cluster binding. Its self-signature is verified, because a CA that skips proof-of-possession is a signing oracle.
- **Single-use is a conditional `UPDATE`** (`WHERE consumed_at IS NULL`) on Postgres — the same mutual exclusion as the job claim — or an in-process lock plus atomic replace on the file store. Tokens are stored SHA-256 hashed, never in the clear. Pinned by a *concurrent* test; a read-then-write passes every other assertion and fails that one.
- **Renewal is authenticated by the current certificate**, at 2/3 of its life, and **never touches the running stream**. The new material is swapped into a `Holder` that Go's `GetClientCertificate` consults per handshake, so the *next* dial picks it up while the open connection keeps the old certificate — still valid for the remaining third. That overlap is why rotation drops no in-flight collection and needs no restart. Do not revoke on renewal; that would kill the stream this design exists to protect.
- **Revocation is swept, not just checked at connect.** A transport built around a stream that stays open for weeks makes revocation-at-reconnect close to meaningless, so `AgentGateway._sweep_revocations()` ends live sessions whose serial was revoked. Both this and the connect-time check are pinned by tests.

Two listeners, because gRPC's Python bindings offer only "never request a client certificate" or "require and verify one" — there is no request-but-don't-require mode. The **gateway** port requires a certificate and serves `Connect` plus renewals; the **enrolment** port (default: gateway + 1) requests none and serves only token bootstrap. A fleet that has finished enrolling can firewall the enrolment port off.

`AGENT_GATEWAY_TLS=disabled` keeps the M4a plaintext path as an explicit, logged opt-in for local development — same discipline as the single-process job store. Sessions established that way report `identity_source: "declared"`, so a deployment that left it on cannot look like one that did not. An agent must pass `--insecure` to match, and an mTLS gateway refuses it outright rather than downgrading.

Enrolment state follows the same decision as the job store: `FileEnrolmentStore` under `AGENT_IDENTITY_DIR` by default, `PostgresEnrolmentStore` when `DATABASE_URL` is set (migration `002_agent_identity.sql`). Both are held to `tests/test_enrolment_store_contract.py`. **The CA private key is a file, not a database row** — a dev CA is generated on first start and says so loudly; supply `AGENT_CA_CERT_FILE`/`AGENT_CA_KEY_FILE` for anything shared.

Operator commands are a CLI, not an HTTP endpoint, because the platform still has no auth (F13) and a token-minting endpoint behind nothing would be worse than the problem it solves:

```bash
python -m app.agentctl issue-token --cluster prod-eu-1
python -m app.agentctl list --cluster prod-eu-1
python -m app.agentctl revoke --cluster prod-eu-1 --reason "node compromised"
python -m app.agentctl ca --out ca.crt
```

**Known limitation:** an agent's *first* dial has nothing to verify the platform with. `--ca-file` (printed by `issue-token`) is the correct path; without it the bootstrap call is trust-on-first-use, logged as such, and the CA bundle returned by `Register` is pinned for every dial afterwards.

### Wire contract (`/proto`, `app/wire/`)

The schema for the cluster-agent protocol, and the source of both the Python bindings and the Go ones under `agent/gen/`.

`app/wire/codec.py` maps `Evidence` ↔ protobuf losslessly in both directions, which is the whole of M2's guarantee: if a value can change on a round trip, a fleet diagnosis stops being reproducible from its own evidence. `tests/test_wire_contract.py` fuzzes it (seeded, so failures reproduce) rather than spot-checking, and every mutation of the codec — dropping a field, treating `""` as absent, losing sub-second precision, non-canonical key order — fails it.

Three things the codec must not regress:

- `None` is not `""`. A cluster-scoped object has no namespace; that is different from a namespace named `""`. proto3 field presence carries it.
- **No `default=` fallback in `json.dumps`.** Coercing an unserialisable value to its repr would round-trip a datetime into a string and lose the type silently. `history_service` already requires strictly JSON-serialisable payloads, so raising `WireEncodeError` is consistent.
- **A decode failure raises, it does not degrade.** Inventing a plausible record to paper over a protocol bug or a hostile peer is exactly what the evidence spine exists to prevent.

`EvidenceSpec` (`collection.proto`) is the schema-level counterpart to `ResourceRequest`'s closed verb set: it names a *kind* of evidence and its target, and has no field that can carry a command. `TestRequestsCannotCarryCommands` asserts the field sets directly, so a future field reintroducing the escape fails the build.

Generated bindings under `app/wire/gen/` are **committed** — `pip install -r requirements.txt` stays sufficient, and a schema change shows up in review as a diff. Regenerate with `python scripts/generate_proto.py`; CI runs `--check` so the two cannot drift. `protobuf` and `grpcio-tools` are pinned to matching versions because protobuf 7 validates gencode against the runtime. Service stubs are deliberately *not* generated yet — they import `grpc`, which is not a dependency until M4.

### Collection (`app/collectors/`)

Collectors declare `provides` / `requires` / `optional_requires`; `CollectorRegistry.resolve()` topologically sorts them into waves. `requires` must have a registered provider (a missing one raises `CollectorGraphError` at resolve time); `optional_requires` only affects ordering when a provider exists, which is how optional backends like Prometheus stay absent without breaking the graph.

`CollectionScheduler` guarantees three things regardless of collector behavior:

- A collector that raises, hangs, or exhausts the budget degrades **only its own** evidence.
- Every declared kind lands in the store, worst case as a non-usable record explaining the gap.
- **Redaction happens here, at the collection boundary** — so reports on disk, the HTTP API, and the LLM all see the same scrubbed payload. Do not reintroduce redaction at the prompt boundary; that leaves the persistence and API paths uncovered.

The inspectors are **adapted, not rewritten** (`app/collectors/kubernetes.py`). `InspectorCollector` runs one inspector — fetch what it declares, then let it analyse — and maps the established `{"error": ...}` contract onto evidence status through `app/kubernetes/errors.py`. Everything except pod logs is independent and runs as one concurrent wave; logs form a second wave because `PodLogsCollector` declares `requires={PODS}`.

An inspector's reads now go out as a **batch** (`fetch_many`). `WorkloadInspector` made four sequential kubectl calls; it issues one round trip, which matters on a stream that may cross a continent.

### Inspector contract (`app/kubernetes/`)

Since M5 an inspector **fetches nothing**. It declares `id` / `kind` / `label`, and two methods (`app/kubernetes/inspector.py`):

- `requests(scope) -> [ResourceRequest]` — what to read. A provider decides how.
- `analyse(results, scope) -> dict` — what it means. Pure: no I/O, no clock, no cluster. Results arrive positionally, in the order `requests()` asked for them.

`analyse()` keeps the `{"error": ...}` contract on a failed read (build it with `inspector.failure()`), because severity, health and overview logic count exactly those keys — so **a new inspector still needs wiring into `_health_summary` and `_severity_summary`** or its findings are ignored. An inspector inventing its own failure shape gets recorded as healthy evidence for a cluster nobody could read, which `WorkloadInspector` shipped once already.

`analyse` takes the scope as well as the results because some conclusions depend on what was *asked for*: "no cluster DNS service exists" is only sayable when the whole cluster was scanned. That check was previously unreachable — it read the same local the service loop assigned to — and M5 fixed it. See `tests/test_inspectors.py`.

All cluster access is **read-only by construction**, now at two layers: `ReadVerb` cannot express a mutation, and `KubectlExecutor.run()` calls `assert_read_only()` from `app/kubernetes/command_policy.py`, which allowlists verbs and sub-verbs. A mutating command raises `UnsafeKubectlCommand`, which the scheduler's fault boundary records as failed evidence. `executed_commands` is guarded by a lock because collectors run in worker threads.

`InvestigationService` derives the rest from the store: `metrics`, `security`, `topology`, `timeline`, `cluster_access`, plus the additive `evidence` (citation index) and `evidence_coverage` keys.

**Resource usage is measured; the percentage is derived** (`app/kubernetes/metrics.py`). `kubectl top` prints a percentage it computed from node allocatable; metrics.k8s.io returns usage and nothing else. Rather than teach the Go agent to reproduce kubectl's column layout, `ResourceMetricsCollector` normalises both into one shape and computes the ratio on the platform from `NODES_RAW` evidence — for *both* providers, so they cannot disagree. kubectl's own percentage column is parsed and discarded. `tests/test_metrics_parity.py` pins it.

Scoping: `resource_kind` + `resource_name` narrow the pod and deployment collectors only; the rest still run namespace- or cluster-wide.

### Playbooks (`app/playbooks/`)

Investigation is **iterative**: baseline collection → analysis → playbook selection → targeted collection → re-analysis. `InvestigationOrchestrator` drives the loop and `InvestigationService._build_view` is passed in as the view builder, so the analysis engine always sees the latest evidence.

**A playbook is a planner, not an executor.** It emits targeted collectors from `app/collectors/targeted.py`; the existing scheduler runs them. That is why playbooks inherit fault isolation, redaction, concurrency, and budgets for free — do not let a playbook call `kubectl` directly.

Each hypothesis's `missing_evidence` is the collection plan: playbooks collect exactly what the deterministic layer already knew it lacked.

Two conventions in targeted collectors:

- **Structured summaries, not raw dumps.** One `get pod -o json` yields probes, exit codes, restart counts, limits, and config references; `PodSpecCollector` extracts them. Never parse `kubectl describe` text — it is not stable across versions.
- **Secret values are never read.** Secrets go through `describe` (prints key names, never values); ConfigMaps use `get -o json` but only key names are emitted. `test_secret_values_are_never_requested` asserts no command issues `get secret`.

Bounded on three axes: `max_targets` (5) per playbook, `max_rounds` (1), and the shared `CollectionBudget`. Collectors already executed are never repeated, so raising `max_rounds` converges.

Deep payloads land in `investigation["deep_evidence"]` keyed by kind (baseline kinds excluded — they are already in named sections); `AnalysisInput.deep()` reads them. `playbook_rounds` records what ran.

`CollectorRegistry.resolve(available)` is what lets round 2 depend on baseline evidence without re-registering the collectors that produced it.

### Analysis layer (`app/analysis/`)

Evidence → **signals** → **hypotheses**, all deterministic, all before any model call.

A `Signal` is an evidence-backed observation extracted by a rule in `signal_rules.py`. `evidence_ids` is mandatory — `Signal.__post_init__` raises if empty, because a signal that cannot name its provenance is a bug. Ids are deterministic (`pod.crash_loop:pod/prod/web-0`), which makes duplicate observations idempotent.

A `Hypothesis` is a candidate root cause. Rules in `hypothesis_rules.py` are **declarative data**, not branching logic: each `SignalPatternRule` states its `triggers`, `supporting`, `refuting`, and `missing_evidence`. Adding a failure mode means appending a rule to `DEFAULT_HYPOTHESIS_RULES` — no dispatcher to edit. Confidence is `base + 10/supporting type − 20/refuting type`, and `missing_evidence` drives what playbooks collect.

`signal_rules.py` runs over baseline evidence; `deep_signal_rules.py` runs over targeted evidence and produces findings baseline cannot reach (exit code 137 confirming an OOM, a referenced ConfigMap key that does not exist). Rules that could fire broadly are gated on evidence of the specific failure — `image.no_pull_secret` only fires when a container is actually failing to pull.

Both rule loops are individually fault-isolated: one broken rule is logged and skipped, not fatal.

### Remediation (`app/remediation/`)

Plans are keyed on the **hypothesis**, not on investigation heuristics, so they can name the actual container, its limits, and the owning workload. `RemediationPlanner` maps `hypothesis.id` → rule; an unmatched or failing rule degrades to a read-only *diagnostic* plan built from the hypothesis's `missing_evidence` rather than inventing a fix.

**The platform can never apply what it generates.** Mutating commands are rejected by `assert_read_only()`, and `tests/test_remediation_safety.py` asserts that for every registered rule — plus the inverse, that preconditions and verification steps *pass* the policy. Those tests are parameterised over `DEFAULT_REMEDIATION_RULES`, so a new rule is automatically held to the same guarantees.

Two correctness details that are easy to regress:

- **Target the controller, not the pod.** `PodSpecCollector` captures `ownerReferences`; the Deployment name is derived by dropping the ReplicaSet's pod-template-hash. The derivation is flagged as a caveat, never presented as fact.
- **Bare pods are not rollable.** `rollout restart/undo/status` fail against a Pod. `_rollable()` gates this; unmanaged workloads get pod-appropriate verification and a manual rollback step.

The platform deliberately refuses to generate secret values, a memory limit when no current limit was observed, and NetworkPolicy selectors — each emits a caveat saying why instead.

`remediation_risk` and `remediation_plan` keep their original shapes via `to_legacy()`/`to_legacy_plan()`; `remediation` and `patches` are additive. All are computed deterministically and overwrite model output.

`command_policy` treats `rollout` as a mixed verb: `status`/`history` are allowed, `undo`/`restart`/`pause` are not.

### AI layer (`app/ai/`)

**The model never diagnoses from raw JSON.** `PromptBuilder` sends only signals, hypotheses, scope, health, and coverage — then asks the model to *select and explain*, citing signal ids. This shrinks the prompt and, more importantly, narrows what the model can assert.

**Commands are never taken from the model.** `_normalize()` uses the deterministic command set regardless of what the model returns, and every surfaced command passes `classify_command()` — unrecognised strings are dropped, mutating ones labelled. This closes a verified injection chain: hostile pod-log text reached the prompt and produced `kubectl delete ns kube-system` as an operator-facing recommendation. The read-only executor does *not* mitigate that, because the human is the execution path. Regression tests: `tests/test_prompt_injection.py`.

`GroundingValidator` (`app/analysis/grounding.py`) enforces **citation integrity** *and* **semantic consistency**. Citation checks:

- A fabricated hypothesis id **rejects** the response.
- Fabricated signal ids are **stripped** and recorded in `grounding.rejected_citations`; if signals existed and none of the model's citations survive, the response is **rejected**.
- An empty root cause **rejects** the response.
- With no signals at all (healthy cluster), citations are not required.

A rejected response falls back to the deterministic ranking. Do not weaken this to "log a warning and use it anyway" — the tests in `tests/test_grounding.py` and the two `*_is_discarded` tests in `tests/test_root_cause_analyzer.py` exist to catch that.

Semantic checks then reject prose that misrepresents what it cites — citation integrity alone let a response cite a genuine CrashLoopBackOff while concluding "resolved, no action needed":

- **Contradiction**: reassurance language ("no action needed", "appears healthy") over CRITICAL/HIGH signals. The same wording passes on a genuinely healthy cluster, where there are no severe signals to contradict.
- **Citation relevance**: at least one cited signal must be one the selected hypothesis actually rests on. Citing real but unrelated signals explains nothing.
- **Invented resources**: `namespace/name` references appearing in no evidence. The regex is case-sensitive and excludes paths, so `512Mi/1Gi` and `/healthz` are not mistaken for resources.

These are deterministic — a second model call would add latency and cost per investigation and would itself need grounding. Matching is deliberately lenient: **an over-strict check does not fail loudly, it silently routes every investigation to the fallback**, so `TestGenuineDiagnosesStillPass` and the false-positive cases in `tests/test_semantic_grounding.py` guard the fallback rate and must not be weakened.

`fix` and `prevention` remain model-authored prose. Commands never are.

Confidence is composed, not asserted (`app/analysis/confidence.py`): evidence strength / AI confidence / evidence completeness at 0.5·0.3·0.2 on the grounded path, 0.7·0.3 (capped 95) on the deterministic one. `confidence_breakdown` exposes each component's weight and contribution.

The dual-path design still holds: `RootCauseAnalyzer._fallback()` builds a complete diagnosis from `FixRecommendationEngine` and `ConfidenceEngine`, and `_normalize()` spreads that fallback as the per-field default for the model response. **Any new diagnosis field must be produced by `_fallback()` first.** `remediation_risk` and `remediation_plan` are always computed deterministically and overwrite whatever the model said.

`LLMClient` posts to the OpenAI chat completions endpoint directly via `httpx` (no SDK), 3 attempts with linear backoff, `response_format: json_object`.

### Observability backends (`app/integrations/`, `app/collectors/observability.py`)

Prometheus and Loki are **optional**: unset `PROMETHEUS_URL`/`LOKI_URL` means the collectors record `not_applicable` evidence naming the variable to set. Clients never raise for an operational problem — they return a result carrying a status, so `empty` ("we looked, nothing there") stays distinguishable from `unavailable` ("we could not look").

**Absent backends produce no signals, including no negative ones.** Missing metrics must never read as healthy metrics.

`EvidenceStore.coverage()` excludes `not_applicable` from the completeness ratio (reporting it separately). Completeness feeds confidence, so counting an undeployed Prometheus as a gap would permanently lower confidence in diagnoses that never needed metrics.

Queries use only cAdvisor/kube-state-metrics names. **Application-level metrics are deliberately not queried** — their names are per-application, and a guessed name returns an empty result indistinguishable from a healthy one.

Both are emitted by playbooks (CrashLoop → pod metrics + historical logs; Pending → node metrics), not baseline collection. Baseline usage still comes from `kubectl top`.

### Reports (`app/reports/`)

`IncidentReportComposer` builds a structured `IncidentReport`; the PDF, Markdown and JSON writers all render **that one composition**, so the formats cannot disagree and a new section is one change rather than three. The JSON report carries the composition under its `report` key.

Sections with nothing behind them are **omitted, not padded** — same rule as the console.

`history_service.py` renders the three formats and hands the **bytes** to a `ReportStore` (`app/services/report_store.py`) — filesystem or Postgres. It returns bytes rather than a path because `/investigations/{id}/pdf` may be served by a worker that never rendered the file; that is also the seam M8 swaps for object storage, changing one method and no endpoint. The PDF is hand-rolled object emission (`_pdf_bytes`, base-14 fonts, no PDF dependency), so section bodies are flattened via `ReportSection.as_lines()` and text must be pre-wrapped and non-ASCII escaped. On the filesystem backend `history.json` is capped at 25 entries; on Postgres the 25 is a query limit and nothing is discarded. `POST /investigations/{id}/regenerate` re-renders all three from stored JSON without re-querying the cluster — so improving the composer improves historical reports too.

### Onboarding and the fleet API (`app/api/agents.py`)

`/clusters` merges kubeconfig contexts with agents connected to this worker; each entry carries `connection` (`agent`/`kubeconfig`) and, when present, an `agent` block. A cluster reached only by an agent has no kubeconfig entry and would otherwise be invisible.

`POST /agents/enrolment` mints a single-use token and returns an apply-able manifest (namespace, ServiceAccount, a ClusterRole granting `get`/`list`/`watch` only, Deployment). **It refuses outright when `AUTH_MODE=disabled`** — an unauthenticated endpoint that enrols clusters is worse than the problem it solves — and points at `agentctl` instead. `agent/Dockerfile` builds the image the manifest references (distroless, non-root, no shell).

**"Online" is heartbeat-derived, not socket-derived.** An idle stream and a half-open one look identical from the platform's side, so the gateway pings every 15s and the agent's `AgentHealth` reply refreshes `last_seen`; `AGENT_STALE_SECONDS` (45) decides staleness. Do not replace this with "the stream is open".

### Report retention

`ReportStore.prune()` deletes rendered artefacts older than `REPORT_RETENTION_DAYS` (14), swept every `REPORT_RETENTION_SWEEP_HOURS` (6) by a task started in `app/state.py`. **The history entry survives and is marked `expired`** — deleting it too would make an investigation that happened look like one that never did. 0 disables pruning.

### Frontend

`src/App.tsx` still holds the original panels plus the `Dashboard` composition. **New work goes in `src/routes/`, `src/components/`, `src/hooks/`, and `src/lib/`** — do not grow `App.tsx` further. `InvestigatePage` was the last pre-redesign page living there and has moved to `src/routes/`; what remains is `InvestigationPage`, `ReportsPage`'s `HistoryTable`, and the old `Dashboard`.

`/connect` (`ConnectClusterPage`) is the onboarding flow: name a cluster, mint an enrolment, copy the manifest, watch for the agent to check in. `AgentDot` renders agent reachability in three states — online, degraded, silent — and never in colour alone.

Investigations run through `useInvestigationJob` (`src/hooks/`), not a React Query mutation. It posts to `/investigations`, streams progress over SSE, and **falls back to polling** when `EventSource` fails — corporate proxies commonly block it, and a stalled screen during an incident is worse than a slower one. Both paths converge on one terminal `GET /investigations/{id}` for the full result. `transport` is surfaced in the UI so a degraded path is visible.

Pure logic lives in `src/lib/analysis.ts` (grouping, filtering, severity ordering, formatting) so it is testable without rendering. Panels stay presentational.

HTTP goes through `src/services/http.ts`, a small `fetch` wrapper — **there is no axios**. It cost 16.7 KB gzipped, more than the console's entire own code, for eight JSON calls. The wrapper keeps what was actually used (base URL, 120s timeout via `AbortController`, JSON encode/decode, throw on non-2xx) and adds a typed `ApiError` carrying `kind` (`network` / `timeout` / `http`) and `status`. Do not reintroduce an HTTP client library without a reason beyond convenience.

`vite.config.ts` splits `react` and `query` into separate chunks. Total bytes are unchanged; the point is that a deploy only invalidates the ~16 KB app chunk instead of all ~88 KB. Note the `manualChunks` **function** form is required — the object form does not capture subpath imports such as `react-dom/client`, which silently leaves react-dom in the app chunk. Do not code-split the app's own panels: they are a small fraction of the bundle and each extra chunk costs a round trip.

Two properties are load-bearing and must not regress:

- **Never display evidence the backend did not report.** `ConfidenceEvidence` previously fell back to a hardcoded `["Events", "Pod Logs", …]`; panels now render an empty state instead. In a product whose premise is that nothing is asserted without evidence, placeholder content is a correctness bug.
- **Progress is real.** The old `progressSteps` array advanced on a 900ms timer with no relationship to backend work. Every row in `LiveTimeline` is an event the backend actually emitted.

The env prefix is `react_PUBLIC_` (not `VITE_`), registered in `vite.config.ts`'s `envPrefix`; `react_PUBLIC_API_BASE_URL` sets the backend base URL and is also used to build the absolute `EventSource` URL. Backend CORS defaults to `http://localhost:3000` only (`settings.cors_origins`).

Response shapes are typed in `src/types/investigation.ts`, but the backend returns `dict[str, Any]` for `investigation` and `diagnosis` — **the TS types are the only contract and Pydantic will not catch drift.** `scratchpad/contract_check.py` style verification (run the backend against a fake cluster, assert every field the console reads is present) is the way to check it.

`vite.config.ts` imports `defineConfig` from `vitest/config`, not `vite` — vitest owns the merged config type. Tests need `IS_REACT_ACT_ENVIRONMENT` from `src/test/setup.ts`. Avoid Testing Library's `waitFor` in fake-timer tests: it polls on timers and will hang; advance timers explicitly instead.

## Notes

- Prompts are inline in `app/ai/prompt_builder.py`. The former `prompts/` directory described a loading convention that was never implemented and has been removed.
- Dead code with no importers: `app/ai/client.py`, `app/kubernetes/inspector.py` (its `inspect_nodes()` is a hardcoded stub, unrelated to the real `node_inspector.py`), and `start_investigation()` at the bottom of `investigation_service.py`. The live entry points are `LLMClient`, the per-resource inspectors, and `InvestigationService.run()`.
