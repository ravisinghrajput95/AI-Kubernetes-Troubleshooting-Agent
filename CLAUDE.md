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

**Authentication configuration is validated at startup**, like every other
setting. It was the only one checked lazily — the authenticator is built on
first use, so a typo'd `AUTH_MODE`, a missing `OIDC_ISSUER` or `disabled`
without its acknowledgement **started successfully and then 500'd every
request**, with `/health` still green because it is unauthenticated. A
readiness probe passing while the service serves nothing is the hardest shape
of misconfiguration to notice. `Settings.validate_auth()` calls the same
`build_authenticator` the dependency does, so the two cannot drift.

`docker-compose.yml` deliberately does **not** set `ALLOW_INSECURE_NO_AUTH` for
you. Pre-setting it was the "careless deployment" F13 warns about, shipped in
this repository — a `docker compose up` that publishes a port, authenticates
nobody, and supplies its own acknowledgement. Compose now refuses to start
until an operator chooses, and the refusal names the variable.

**Security status:** authentication (F13), per-tenant authorisation, tenancy under row-level security, rate limiting and an append-only audit log are all built — see the sections below. What remains is in `SECURITY.md` (*Known gaps*) and `docs/PRODUCTION_READINESS.md`, chiefly `AUTH_MODE=disabled` still being the shipped **default**, a development agent CA unless one is supplied, and redaction being best-effort on free text. This line said "chiefly no rate limiting" for several milestones after rate limiting shipped; a stale status line is the same failure as a stale "this is dead" note.

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

**The same copying is why `require_principal` must stay `async`.** It was `def` from M6 until M6.5, FastAPI runs a synchronous dependency in a worker thread, and a worker thread gets a *copy* — so the `set()` was discarded and **every request ran as `default`**: all tenants' rows in one, every tenant reading every other tenant's, with the policies enabled, forced and correct. The isolation tests missed it because they enter `tenant_scope()` by hand, proving the schema rather than the request path.

**The one thing that would make all of it inert: connecting as a superuser.** `ENABLE`/`FORCE ROW LEVEL SECURITY` were both set and correct, and every tenant could still read every row, because superusers and `BYPASSRLS` roles skip policies entirely — a deployment with no isolation and no symptom. `Database.assert_row_level_security_applies()` refuses to start `shared` on such a role. `tests/test_tenancy.py` connects as an unprivileged role for exactly this reason; run as `postgres` its isolation assertions all pass while proving nothing.

`system_scope()` is the one deliberate hole — the queue consumer and reaper cannot know a tenant before reading the row that names one. A test asserts `jobs/consumer.py` is its only user.

Agent identities carry their tenant in the SPIFFE path (`spiffe://<domain>/tenant/<t>/cluster/<id>`); the untenanted M4b form still parses as `default`. `AgentRegistry` is keyed by `(tenant, cluster)`, so two customers may both call a cluster `prod` without either evicting or reaching the other.

### Authorisation (`app/authz/`, migration `004`)

M6 made a tenant a data boundary; this makes it an organisation. Four roles,
and each boundary is a capability rather than a tier:

| Role | Gains |
|---|---|
| `viewer` | read what they own, see the fleet |
| `operator` | **may cause a read against a customer cluster** and spend model budget |
| `admin` | **may change the fleet** (enrol, revoke) and **who is in it** |
| `owner` | **may grant `owner`**; read every investigation in the tenant; is the floor |

`owner` exists for one invariant — **you cannot grant a role you do not hold**.
Without it, `admin` granting `admin` makes the two identical, there is no
ceiling on escalation, and an admin can demote every other admin. The last
un-suspended owner cannot be demoted, removed or suspended, or ownership becomes
a state you can leave and cannot re-enter over HTTP.

**The check is one router-level dependency plus a route → permission table
(`routes.py`), and a route with no entry is *denied*.** That is what makes a
forgotten endpoint fail closed; there are no per-route permission checks to
forget because there are none. `tests/test_authz.py` derives the route list from
the OpenAPI schema and asserts every route has an entry — leaving one open means
naming it `AUTHENTICATED` in the table, where the decision is visible. `/me` is
the only one, and has to be: a caller with no role must be able to discover that.

**403 for a permission, 404 for ownership, and permission is checked first.**
The 404 is a disclosure control about *ids*; a permission denial discloses only
the caller's own role, which `/me` already tells them. Reversing the order would
let a caller who cannot read investigations at all use 404-vs-403 as an
existence oracle.

Role resolution (`resolver.py`), in order: **suspension denies outright**; then
the *higher* of the group-derived role and the stored binding — grants combine,
because a binding that could lower an IdP grant is the second directory that
group mapping exists to avoid; then the fallback:

| Deployment | Unbound caller |
|---|---|
| `AUTH_MODE=disabled` | `owner` — one function, the only manufactured role |
| `TENANCY_MODE=single` (default) | `RBAC_DEFAULT_ROLE`, default **`admin`** |
| `TENANCY_MODE=shared` | nothing; denied everything but `/me` |

`admin` by default is *today's behaviour preserved exactly*, so no existing
install has to be administered back into working order on upgrade — same
discipline as the single-process job store. It is **refused above `viewer` in
`shared` mode**: a permissive default there means anyone the IdP can place in a
tenant administers it.

**`investigation.read_all` is owner-only, not admin, and the reason is that
default.** Every unbound caller being an admin plus `read_all` on admin equals
upgrading to this milestone silently removing the per-user report isolation
those deployments already had. Caught by `test_auth.py::TestOwnership`.

**A membership row created by a sighting carries no role** (`role=None`, not
`viewer`). Every authenticated request upserts one so an admin can find real
people in `GET /members`; a role there would demote a caller holding the
deployment default on their next request.

Store follows the same one decision as everything else: `FileMemberStore`
(`RBAC_STORE_DIR`, default `data/rbac`) or `PostgresMemberStore`, both held to
`tests/test_member_store_contract.py`. Never in memory — an operator who
assigned roles by hand must not lose them to a restart.

**Authorisation has no `system_scope` equivalent, deliberately.** The tenancy
escape exists because the queue consumer must read a row before knowing its
tenant; authorisation decides at the HTTP boundary and background work carries
the principal it was submitted with. A test greps `app/` for any other place a
role is manufactured.

Bootstrap is a CLI, not an endpoint — same reasoning as `agentctl`, because a
role-granting endpoint reachable before any role exists is the hole:

```bash
python -m app.rbacctl grant --subject alice@acme.com --role owner
python -m app.rbacctl --tenant acme list
python -m app.rbacctl suspend --subject bob@acme.com [--restore]
```

**No invite flow, by decision.** It needs email, single-use tokens, an
acceptance endpoint and a second identity to reconcile — and could never grant
access, because the platform cannot authenticate anyone the IdP does not know.
Pre-assignment is an invite's whole useful content, and `PUT /members/{subject}`
does it for someone who has never signed in.

**`require_principal` must stay `async`.** FastAPI runs a synchronous dependency
in a worker thread, which gets a *copy* of the context — so `_current.set()`
there is discarded and every request runs as tenant `default`. That was true
from M6 until this milestone: all tenants' rows landed in one and every tenant
could read every other tenant's, with RLS enabled, forced and correct. The
authenticator still runs in a thread (a JWKS cache miss must not block the
loop). Pinned by `TestTheTenantSurvivesTheDependency`, which asserts on the
value the request would hand to Postgres — asserting on `principal.tenant`
passes with the bug present.

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

### MCP (`app/mcp/`, `app/api/mcp.py`, M9)

The platform's capabilities as tools a customer's own agent can call. §3.7 puts
MCP here rather than on the agent link — a valuable product surface and a poor
fleet transport.

**The danger is that it is a second entry point.** M6.5 made authorisation
impossible to forget for HTTP by putting one check in a router-level dependency
and denying any route absent from `ROUTE_PERMISSIONS`. A tool call is not a
route, so none of that machinery reaches it, and a tool server calling
`run_investigation` directly would be a complete authorisation bypass wearing a
different protocol.

So `app/mcp/tools.py` is the same idea in the same shape: every tool declares a
`Permission`, **a tool with no entry cannot be called**, and `tests/test_mcp.py`
asserts the registry is complete. `tools/list` is filtered by what the caller
may actually do — listing a tool every call would refuse teaches an agent to
keep trying it. Costed tools are the same `COSTED_PERMISSIONS` set and share
the *same* rate-limit buckets, because a second entry point with its own budget
would double the quota an operator configured.

**Nothing that mutates the fleet is exposed** — no enrolment, revocation or
member management. Those need `admin` and are the operations M6.5 identified as
destructive; handing them to an autonomous agent is a decision a customer
should make explicitly.

`/mcp` is the *second* route mapped to `AUTHENTICATED`, and it means something
different from `/me`: not "nothing to check" but "checked deeper". A test
asserts exactly those two carry the marker.

The JSON-RPC subset (`initialize`, `tools/list`, `tools/call`, `ping`,
notifications) is hand-written rather than the reference SDK — same reasoning
that kept axios out of the console. The cost is that new spec features do not
arrive for free, which is why the supported subset is named rather than implied.

**Every data router installs `require_permission`, and a test asserts it
structurally.** Behaviour cannot see this: handlers also depend on
`require_principal`, so deleting the router dependency leaves authentication
working and every 401 test passing while authorisation silently stops running.
`events` is the one deliberate exception and is asserted as such.

### Action egress (`app/notify/`, M9)

One outbound interface — a signed JSON POST — rather than a shape per vendor,
which would put six formats in this repository and make a seventh a code
change. Fires on the investigation's terminal path, **after** the result is
durable, so a link in a ticket cannot arrive before the thing it points at.

Three rules, each of which fails silently if it regresses:

- **A summary leaves, never the result.** The stored result is 2.7 MB of
  cluster interior. `build_summary` is an explicit allowlist assembled field by
  field, not a filter — a denylist leaks whatever a future collector adds. It
  carries what a ticket needs and a *link* to the rest.
- **A destination belongs to a tenant.** Announcing acme's incident into
  globex's Slack is M6's failure committed on the way out.
- **A notification can never fail an investigation.** Delivery is detached,
  exceptions are dropped, `announce()` returns nothing to await — a caller that
  could observe delivery would eventually be written to depend on it.

Bounded retries (3, no retry on 4xx: retrying our bug becomes their rate
limit). Guaranteed delivery would need a queue, a dead-letter and an ordering
story, which is a different feature from "tell Slack".

**`NOTIFY_DESTINATIONS` is pipe-delimited, unlike every other list here, and
that is forced.** A URL contains colons — `https://`, and `host:8443` — so the
colon-separated shape `API_TOKENS` uses cannot express one. The first draft
split on `:` and silently truncated `https://hooks.example.com:8443/path` to
the host, which would have sent every notification to the wrong place while
parsing without complaint. Caught by its own test before it shipped.

Failures are **not** announced by default: a failed collection is the
platform's problem, visible in the console and in
`k8sagent_investigations_total`, and paging someone about it trains them to
ignore the channel.

### Event ingress (`app/events/`, `app/api/events.py`, M9)

A signed webhook that *triggers* investigations — §3.7's "what turns the
product from human-invoked to autonomous". `POST /events/{source}`, disabled
until `EVENT_SOURCES` names one.

**A source is an identity, not a secret, and that is the whole design.**
`_impersonation_args` returns nothing for an absent or anonymous principal, so
an alert-triggered investigation with no identity reads as the platform's
*service account* — obtaining access no authenticated user could ask for,
through the one door with no user behind it. Impersonation is what makes "the
platform cannot see more than you can" true, and automation must not be the
exception. So `EVENT_SOURCES` is `name:secret:subject[:groups][:tenant]`, the
subject is **required**, and the investigation is impersonated as it exactly as
a person's would be. A source without one is refused at startup.

**The tenant comes from configuration, never the payload.** Anything that can
write an alert rule can influence its labels, so a payload-chosen tenant would
be a cross-tenant trigger. Pinned by a test that observes the ambient tenant at
the moment the handler uses it — asserting the parser alone let the mutation
survive.

**The signature covers a timestamp as well as the body.** A signature makes a
body unforgeable, not un-replayable; without the timestamp inside the signed
material a captured request could be replayed with a fresh header. Five-minute
tolerance, `hmac.compare_digest`.

**Deduplication is not an optimisation.** Alertmanager re-sends on its
`repeat_interval` and whenever a group's membership changes; investigating each
delivery turns one flapping alert into an unbounded series of production
cluster reads. A fingerprint that has already fired is refused for
`EVENT_COOLDOWN_SECONDS` (1800). `TriggerLedger` follows the usual seam —
in-memory, or Redis `SET NX EX` so three replicas do not each investigate the
same alert.

**It fails *closed* where the rate limiter fails open.** A missed deduplication
is an unbounded series of cluster reads; a missed investigation is one alert the
operator still sees in their own alerting. The expensive mistake is the one to
avoid, and the two asymmetries are deliberate opposites.

**Always 202 on a good signature**, even for a duplicate or an alert with no
cluster label. Alertmanager retries a non-2xx, so reporting a normal outcome as
an error produces exactly the storm deduplication exists to prevent.

### Rate limiting (`app/ratelimit/`)

**What is limited is deliberately narrow.** An investigation is the platform's
only outbound action — it reads a customer's production cluster under the
caller's impersonated identity and spends a model call. Reads of what was
already collected cost neither and are already owner-scoped.

**Keyed off the permission, not off a list of paths.** `COSTED_PERMISSIONS` is
`{investigation.run}`, and `require_permission` — the router-level dependency
that already runs on every route — applies the limit when the matched route
needs one. A new endpoint that runs an investigation is limited by virtue of
declaring the permission it needs anyway; there is no second table to forget.
Same reasoning that put the permission check in one place.

**Checked *after* the permission**, so a viewer is told they may not run
investigations at all rather than handed a 429 implying they would be allowed
if they waited.

**A per-worker limit is not a limit.** On three replicas a process-local
counter enforces three times the configured rate, and the effective limit
changes when an operator scales. Same seam as everything else: no `REDIS_URL`
→ `InMemoryRateLimiter` (one process *is* the fleet); `REDIS_URL` →
`RedisRateLimiter` sharing one counter. They are two classes rather than one
with a flag precisely because only the second is observable in a test.

**It fails open, and authorisation fails closed.** A rate limiter is
availability protection against a noisy caller, not a security control against
a hostile one — refusing every investigation because Redis blinked turns a
degraded dependency into an outage. `app/authz` denies on a store failure;
this allows. `tests/test_rate_limiting.py` asserts both, next to each other.

Fixed window (`INCR` + `EXPIRE`, one round trip), so a caller can spend a
window's budget at the end of one and again at the start of the next — a true
short-term ceiling of 2×. Stated rather than hidden: at these defaults that is
far below capacity, and it is the assumption to revisit first if the limits are
ever tightened toward the measured ~600/min per worker.

| setting | default | job |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | 60 | runaway-caller protection; above any human, below capacity |
| `RATE_LIMIT_TENANT_PER_MINUTE` | 0 (off) | fairness between customers; **`shared` warns when unset** |

The tenant quota defaults off because in `single` mode it would cap the whole
platform — a different decision, and the operator's to make. `shared` without
one **warns rather than refuses**: it is a fairness gap, not an unsafe
configuration, unlike the M6 refusals where the alternative was two customers
in one unprotected table.

### Self-observability (`app/observability/`)

`/metrics` in Prometheus exposition format, on the unauthenticated health
router, switchable off with `METRICS_ENABLED=false`. The metric set is chosen
from `docs/PERFORMANCE_ENVELOPE.md` rather than from what is easy to
instrument: every number that document tells an operator to act on has a series
— throughput, queue depth, running-versus-capacity, agent count, collection
duration, evidence status, LLM and grounding outcomes.

**The load-bearing rule is what is never a label: no cluster, tenant,
namespace, user or investigation id.** Two arguments point the same way, which
is why it holds. *Cardinality* — one series per cluster across a 1,000-cluster
fleet is how a Prometheus falls over, and this platform is built for exactly
that size. *Disclosure* — a scraper is infrastructure and carries no tenant, so
labelling by cluster would publish the customer list to anyone who can reach
the port, undoing M6 in one label. When someone wants per-cluster rates, the
answer is the audit log, not a label here.

That rule is also what makes the endpoint safe to leave unauthenticated, and
`tests/test_metrics.py` asserts it end to end — an investigation runs against a
named cluster and the name must appear nowhere in the exposition.

**Grounding rejection reasons are mapped to a closed category set**
(`_rejection_category`). The raw reason quotes the model, which quotes cluster
text, which is attacker-influenced; using it as a label would reopen at the
metrics boundary the injection surface `app/ai` closes at the prompt boundary.

**Every label set is closed, so every series is seeded to zero at import.**
Prometheus does not create a labelled series until first use, so an alert on
`investigations_total{outcome="failed"}` reads "no data" while the platform is
healthy and fires on the *second* failure. Seeding is what makes an alert
correct from a cold start — and it is only possible because no label is
unbounded.

`app/observability/` owns its own `CollectorRegistry` rather than the
process-global default, which auto-registers process and GC collectors and is
shared with anything else importing the library. Recording is total: `_safe()`
swallows, because instrumentation that can fail the thing it measures turns an
observability bug into an outage.

### Phase timing (`app/observability/tracing.py`)

`k8sagent_investigation_phase_seconds{phase}` — `collect`, `analyse`, `report`,
`persist`, `notify`. Closed set, like every label here. Measured at 500 pods on
the distributed deployment: **collect 65%, report 13%, analyse 11%, persist
10%**, totalling ~0.21 s of platform work per investigation.

That measurement is what corrected the published throughput number — see the
envelope section below. A phase attribution that only confirms what you
expected has not been worth taking.

**OTLP trace export is deliberately not built, for a hard reason.**
`opentelemetry-proto` requires `protobuf<7.0`; this project pins
`protobuf==7.35.1` because protobuf 7 validates generated code against the
runtime and the agent's wire bindings depend on it. Installing the exporter in
a scratch environment silently downgraded protobuf to 6.33.6 — the exact
failure the pin exists to prevent. What that costs is cross-worker correlation
(an investigation submitted on one worker and run on another cannot be one
trace); what it does not cost is the question traces were wanted for. A test
asserts protobuf stays on 7.x so the dependency cannot come back without the
bindings being regenerated deliberately.

`span()` records in a `finally`, so a phase that raised is still timed — a slow
failure is exactly the shape an operator needs, and timing only the happy path
would hide it.

### The performance envelope (M8c)

`docs/PERFORMANCE_ENVELOPE.md` is the published one, with the command beside
every number. `scripts/fleet_bench.py` takes them: N synthetic agents, each
with its own gRPC channel and its own `Connect` stream, answering real
`CollectionRequest`s over the published protobuf contract — not a mock of
anything in `agent/`, so a change that breaks real agents breaks it too.

Measured: **1,000 clusters attached in 1.04 s** on one gateway, all visible,
159 MB platform RSS; a single investigation completing **end to end in
0.223 s**, of which `collect` is 65%.

**The throughput figure has been wrong twice, and the sequence is the useful
part.** First ~10/s was called a platform ceiling on the grounds that
throughput stayed flat while offered load rose 4× — but a saturated *client*
gives that signature identically. Then it was retracted in favour of ~143/s,
extrapolated from a single investigation's 0.223 s — but **single-request
latency does not extrapolate to throughput**.

A **concurrency sweep** then settled it. Scaling platform slots (4→32), agent
processes (1→6) and the Postgres pool (10→64) each left throughput at ~12/s.
Scaling **workers 1→2 gave 12.1 → 23.0/s** — linear. **The ceiling is per
worker process; add workers, not slots.** That is M3's
stateless-workers-behind-a-queue design, measured rather than assumed.

In-process stack sampling shows a saturated worker ~92% *idle*, with every
non-idle sample in a Postgres or Redis socket wait and no CPU hotspot — one
Python process serialises HTTP, every agent's gRPC stream, the queue consumer
and analysis, and `asyncio.to_thread` moves blocking calls off the loop but not
off the GIL. `JOB_MAX_CONCURRENT` above a small number buys nothing on one
worker: slots fill, `collect` inflates in proportion, throughput does not move.

**The rule: throughput that does not rise with `JOB_MAX_CONCURRENT` is not the
platform's.** `fleet_bench.py` now refuses to print "platform-bound" from a
single run and says so. Do not move §12's scalability score to 9 on the
strength of that document.

`fleet_bench.py` reports `stream_failures` and exits non-zero if any stream
died. Its first run printed "5 collections, 0 records" — a plausible-looking
platform result produced entirely by an `AttributeError` in the harness. A
benchmark that fails quietly publishes confident nonsense.

### Payload sizes, measured (M8b)

`scripts/payload_bench.py` runs the real pipeline against a scalable fake
cluster and reports where the bytes are. At 2,000 pods — the `MAX_LIST_ITEMS`
ceiling the platform allows itself — one stored result is **2.7 MB**, and the
composition is not what the roadmap assumed:

| section | share |
|---|---|
| `diagnosis.signals` | 34% |
| `investigation.pods` | 27% |
| `investigation.graph` | 18% |
| everything else | 21% |

**The majority is derived, not collected.** "Evidence payloads to object
storage" aims at roughly a quarter of it; signals and the graph are both
reproducible from evidence by design.

**A listing must never select `result`.** `_JOB_SUMMARY_COLUMNS` exists for
this: the listing query used the full column list and the API then discarded
the payload in Python via `to_dict(include_result=False)`, so a 25-row
dashboard load moved 67.5 MB out of Postgres and returned none of it. The two
column lists are kept as separate constants and a test asserts they differ by
`result` alone, so adding a column to one and not the other is a visible diff
rather than a silent absence.

Note what this is *not*: `result` is NULL until a job finishes, so polling an
in-flight investigation was already cheap. The waste was on reads of finished
rows that never wanted the payload.

**`get_summary()` is the read for callers that want a fact, not the
investigation.** Cancellation reads an owner and a status, the stream handler
reads an owner, the consumer's settle path reads a boolean, and
`GET /investigations/{id}/status` reads a status and a timeline — all of them
used to pull the whole payload. Both stores implement it and
`tests/test_job_store_contract.py` holds them to `result is None`, so a caller
cannot come to depend on the payload being present single-process and absent
distributed.

`/investigations/{id}/status` is **additive**: `/investigations/{id}` still
returns everything, because that is what a client rendering the report wants
and changing it would break every consumer to benefit one. Measured end to end
at 500 pods: **784 KB → 10.7 KB, 73×**, about 90 MB saved over a three-minute
run on the polling transport. The status read is 10.7 KB rather than nothing
because it carries the timeline, which is what a progress display is made of.

The property under test is *not reading* the payload, not the response being
small — switching the endpoint back to `store.get()` would still produce a
small response while the megabytes had already left Postgres. The test asserts
the store call.

**Concurrency, not memory, was the ceiling.** `JobConsumer.max_concurrent` was
a constructor default of 4 that `app/state.py` never passed, so every
deployment ran four investigations per worker with no way to change it —
reaching 5,000 concurrent would have needed 1,250 workers. It is now
`JOB_MAX_CONCURRENT`. Peak heap per investigation is about **5× the stored
result**, measured flat across cluster sizes: 13.4 MB at the `MAX_LIST_ITEMS`
ceiling, roughly **76 investigations per GB**
(`python scripts/payload_bench.py --pods 2000 --memory`).

The default stays 4 on purpose. Memory is not the only cost — collection and
analysis occupy worker threads and anyio's pool defaults to 40 — so raising it
is an operator's decision against their own cluster sizes rather than one
inherited from a changed default.

**Streaming ingest was not built, and the measurement is why.** §10 lists
"budgets at source, streaming ingest, object storage, per-tenant quotas" against
"evidence volume overwhelms the platform". The first of those is built
(`MAX_LIST_ITEMS`) and, with bounded worker concurrency, already caps a worker
at tens of megabytes. Streaming ingest would shave a 5× multiple off a 13 MB
base — real, and not the constraint.

### Routing an investigation to the right worker (M8a)

A gRPC stream belongs to whichever worker holds the socket, so a cluster
reachable only through an agent has **exactly one worker** that can investigate
it. Before M8a nothing expressed that: `select_provider` consulted only the
*local* registry, so it could not tell "no agent anywhere" from "an agent, on
another worker", and both fell back to `LocalKubectlProvider`. On three replicas
roughly two thirds of agent-cluster investigations were answered by the
platform's own kubeconfig — and since `LocalKubectlProvider` resolves a cluster
*name* against whatever contexts the platform holds and has no tenant, tenant
A's `prod` could be answered by someone else's `prod`.

Two mechanisms, and the split is the design:

- **Routing is a hint.** `agent_affinity()` (`app/jobs/runner.py`) asks the
  presence index who holds the stream; `enqueue(job_id, worker_id)` puts the
  work on `{prefix}:jobs:queue:{worker}`. `BLPOP` takes a key list and returns
  from the first non-empty, so a consumer naming its own queue first gets
  affinity priority in the round trip it was already making.
- **Refusal is the guarantee.** `select_provider` raises `ClusterUnreachable`
  naming the holding worker rather than reading a same-named local context.
  Surfaced verbatim — 409 on `/investigate`, the job's `error` on the async
  path — because the generic detail says "check your kubeconfig", which is
  exactly the wrong instruction here.

**`agent_affinity` must ask the local registry before `holder()`**, exactly as
`select_provider` does. `holder()` returns nothing when the presence record
names *this* worker — right for `select_provider`, which only reaches it after
the local registry said no — and affinity had no such check, so a submission
landing on the worker holding the stream fell through to the *shared* queue.
Landing on the right worker was the case that un-pinned the job: measured
in-cluster at 1 investigation in 3 reaching the agent, 8 of 8 after.

**Nothing about correctness rests on routing being right.** The row stays
`pending` and the claim stays the conditional `UPDATE`, so a mis-route is a
scheduling miss, never a double run.

**A presence record naming *this* worker is never a routing target.**
`holder()` is consulted only after the local registry has said no, so a record
still claiming us means the agent disconnected here within the TTL. Returning it
would queue work onto a queue we are already draining, and would make the
refusal read "attached to worker-a, not this one" where worker-a *is* this one.

**`PRESENCE_TTL_SECONDS` (45) must stay below `UNCLAIMED_GRACE_SECONDS` (60).**
A job routed to a worker that dies waits on that worker's queue until the reaper
re-offers it — to the **shared** queue, never back to a worker queue. That only
terminates because the dead worker's presence has already lapsed by then;
raise the TTL above the grace period and recovery becomes a permanent loop.
`tests/test_agent_routing.py` asserts the inequality.

**Presence is read with `GET`, not a scan** — routing is on the submit path of
every investigation, and scanning the tenant's agents would make the fleet's
size the cost of starting one. Measured flat at ~0.33ms p50 from 200 to 1,000
clusters.

```bash
docker compose up -d postgres redis
python scripts/routing_bench.py --clusters 1000 --workers 3 --submissions 2000
```

Prints the routing hit rate (100% at 1,000 clusters; 1/N before M8a) and the
submit-path percentiles. Needs Postgres and Redis only — a thousand real agent
streams measures a different thing and belongs to M8c.

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

**The gateway's serving certificate must name every address an agent dials.**
`AGENT_GATEWAY_DNS_NAMES` defaults to `localhost`, which is the one address an
agent never uses — it is in another cluster. The Helm chart derives the Service
DNS names automatically and takes `agentGateway.dnsNames` for external ones;
before it did, every chart-deployed gateway failed the agent's TLS verification
before enrolment began.

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

### Dependency graph (`app/graph/`, M7)

**Derived from evidence, not emitted by collectors.** §3.6 calls the graph "a byproduct of collection"; taken literally that means every collector emitting edges and a second thing to keep correct. Deriving it instead — the way signals are — means it is reproducible from a stored report, inherits redaction and fault isolation, and adds no collection path that the local and agent routes could diverge on.

Edge rules live in `edge_rules.py` and are declarative. **No rule invents a node**: an edge is emitted only when both ends were observed, so "depends on a ConfigMap we could not see" and "depends on nothing" cannot look alike to a traversal. Placeholders are refused explicitly — a pod whose node reads `Pending` is not placed on a node called Pending, a claim whose class reads `none` is not linked to a class called none.

`ClusterGraph.depends_on()` / `dependents()` are breadth-first, depth-limited (5) and cycle-safe. The direction is a flag, not the identity of the step function: `step is self.out_edges` is always False because attribute access builds a new bound method, which made every forward traversal stop after one hop while still returning plausible results.

`graph_signal_rules.py` holds the signals a single section cannot reach. The test for belonging there is that the finding is a *path*: `storage.pvc_unbound` needs no graph, but "this Pending pod is blocked by that claim, and that claim's class is blocking others too" is three sections that mean nothing apart. Graph signals cite every edge walked, not just the destination.

`evals/cases/investigations/graph-*.json` are the exit criterion — removing `GRAPH_SIGNAL_RULES` drops the corpus from 13/13 to 10/13.

### Telling a locked door from a broken cluster (`app/kubernetes/access.py`)

With impersonation on — the default — the platform reads as the *caller*, so a
user whose Kubernetes RBAC is narrower than the service account's gets every
read refused. That used to surface as a degraded investigation of a broken
cluster, with the one fact that resolves it in seconds appearing nowhere.

**Not the preflight F6 asked for, deliberately.** `kubectl auth can-i` is a
*command*, and `ResourceRequest` cannot carry one — that closed verb set is the
property that makes a request safe to send to a customer's cluster. Instead
this reads `EvidenceStatus.FORBIDDEN`, which both providers already record, so
it works identically through the agent and the kubeconfig where a preflight
command would have worked only on one.

Three conditions, and each stops a different false accusation:

- **Nothing usable was collected.** Four good reads and six refusals is a
  *partial view* the investigation can still reason over. An earlier version
  fired on share alone and produced "every cluster read was refused" for a run
  where four had succeeded; its own test caught it.
- **At least `MINIMUM_REFUSALS` (3).** "Nothing usable, all refusals" is
  technically true of a scope that attempted one read.
- **Refusals dominate the failures.** Otherwise a cluster that is merely down
  gets diagnosed as a permissions problem — the same confusion pointing the
  other way.

The message names the impersonated identity, because the confusing part is that
the *platform* can read the cluster and the user cannot.

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

**A standard metric name is not the same as a metric that is present**, and until
M9.1 nothing here had been run against a real backend. Four defects, all of the
same shape — a query that parses, returns `success`, and matches nothing. Full
account in `docs/OBSERVABILITY_INTEGRATIONS.md`; the load-bearing parts:

- **The memory limit came from `container_spec_memory_limit_bytes`, which
  kube-prometheus-stack drops wholesale** (`{"action":"drop","regex":"container_spec.*"}`)
  for cardinality. So the limit was `None` on the most common Prometheus
  deployment there is, both derived percentages vanished with it, and **both**
  memory signals were unreachable: a 96Mi container OOMKilled eight times
  produced no memory finding while its evidence read `OK`. Now
  kube-state-metrics first (`kube_pod_container_resource_limits`) with cAdvisor
  as the fallback — preferred on merit too, since it reports the *declared*
  limit and emits nothing for a container with no limit, where cAdvisor reports
  the cgroup sentinel that divides every usage down to "0.0% of the limit"
  (`_plausible_limit` discards it).
- **`node_memory_MemAvailable_bytes{node=…}` never matched anything.**
  node-exporter series carry `instance`/`job`, never `node`. Replaced with a
  cAdvisor sum that does carry it, and renamed `used_memory_bytes` to say what
  it measures.
- **`max_over_time(...)` over a pod selector is not aggregated.** It returns one
  series per container *and* per restarted instance — ten for a pod that had
  crashed six times — and `scalar()` takes `samples[0]`, which Prometheus does
  not order. The peak was 5.9 MB or 92 MB from the same query, by luck. Every
  query is now wrapped; `test_every_query_reduces_to_a_single_series` pins the
  property against captured responses rather than each query individually.
- **`peak` and `current` were chained with `elif`, so the restart erased the
  evidence for the restart.** A container sampled just after an OOM kill reads
  0.2% current against a 91.6% peak, and the low current skipped the peak check
  — discarding exactly the history `max_over_time` was queried to recover. Now
  judged on the worst of the two. The 98% peak threshold was also unreachable:
  working set is a *sampled* gauge and the kill happens between scrapes, so it
  is 90%.

**`tests/fixtures/real_observability_kps_loki.json` is captured, keyed by the
query the collector actually issued** (recorded by intercepting httpx while the
shipped collectors ran, so the keys cannot drift from the code). The replay
fixture answers an **unrecognised query with an empty vector** — what the real
Prometheus returned for the names this code used to ask for — which is what makes
it a regression harness instead of a recording. The old hand-written handlers
answer every query with the same value, which is exactly why they could not see
any of this: an absent metric and a metric reading zero are the same thing, and
`peak` can never differ from `current`. Do not add cases to those handlers;
add them to the captured fixture.

`docs/qa/observability-faults.yaml` induces the three faults, chosen to exercise
distinct query shapes rather than distinct workloads.

### Reports (`app/reports/`)

`IncidentReportComposer` builds a structured `IncidentReport`; the PDF, Markdown and JSON writers all render **that one composition**, so the formats cannot disagree and a new section is one change rather than three. The JSON report carries the composition under its `report` key.

Sections with nothing behind them are **omitted, not padded** — same rule as the console.

`history_service.py` renders the three formats and hands the **bytes** to a `ReportStore` (`app/services/report_store.py`) — filesystem or Postgres. It returns bytes rather than a path because `/investigations/{id}/pdf` may be served by a worker that never rendered the file; that is also the seam M8 swaps for object storage, changing one method and no endpoint. The PDF is hand-rolled object emission (`_pdf_bytes`, base-14 fonts, no PDF dependency), so section bodies are flattened via `ReportSection.as_lines()` and text must be pre-wrapped and non-ASCII escaped. On the filesystem backend `history.json` is capped at 25 entries; on Postgres the 25 is a query limit and nothing is discarded. `POST /investigations/{id}/regenerate` re-renders all three from stored JSON without re-querying the cluster — so improving the composer improves historical reports too.

### Onboarding and the fleet API (`app/api/agents.py`)

`/clusters` merges kubeconfig contexts with agents connected to this worker; each entry carries `connection` (`agent`/`kubeconfig`) and, when present, an `agent` block. A cluster reached only by an agent has no kubeconfig entry and would otherwise be invisible.

**The agent stores its identity in a Kubernetes Secret, not a volume.** A PersistentVolumeClaim does not work everywhere clusters actually are — EKS on Fargate has no EBS so a ReadWriteOnce claim never binds, a cluster with no default StorageClass does the same, and a zonal volume cannot follow a rescheduled pod. Every one of those failures looks identical from outside: a pod stuck in ContainerCreating with nothing in the agent's logs, because the agent never starts. `identity.Store` is an interface; `FileStore` serves `docker run` and a laptop, `SecretStore` is the in-cluster default. This does **not** weaken the read-only guarantee: the ClusterRole is still get/list/watch, and a separate namespaced Role grants `get`/`update` on exactly one Secret by name — the agent's own credential.

`POST /agents/enrolment` mints a single-use token and returns an apply-able manifest (namespace, ServiceAccount, a ClusterRole granting `get`/`list`/`watch` only, Deployment). **It refuses outright when `AUTH_MODE=disabled`** — an unauthenticated endpoint that enrols clusters is worse than the problem it solves — and points at `agentctl` instead. `agent/Dockerfile` builds the image the manifest references (distroless, non-root, no shell).

**On more than one replica, the console reads a shared index, not its own registry** (`app/gateway/presence.py`). `AgentRegistry` is per-process by necessity, so `GET /agents` used to answer from whichever pod the load balancer picked — thirty clusters behind three replicas showed about ten, and a different ten next refresh. Each gateway now announces its agents into Redis with a 45s TTL, refreshed by the heartbeat, and the API returns the union. Expiry rather than deregistration, because a killed worker cannot deregister and phantom agents are worse than a few seconds of staleness. Every record carries `worker` and `local`. **M8a made presence routing as well as visibility**: the submit path asks `holder()` who owns the stream and queues the work there, so an agent held by another replica *is* investigated through that replica. What the record still cannot do is let *this* worker collect through someone else's socket, which is why `select_provider` refuses rather than falling back.

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

### Operability (`app/core/correlation.py`, `app/core/readiness.py`, M9.2)

**Liveness and readiness are different questions and must stay separate.**
`/health/live` never consults a dependency — a liveness probe that checks
Postgres restarts every worker in the fleet on a blip, turning a recoverable
dependency failure into an outage. `/health/ready` consults the store and fails
while starting, while draining, or when something it cannot work without is
gone. `/health` is unchanged for backward compatibility: the console reads it
for `auth_mode` before it can authenticate.

**`check_health()` returns three values and the *store* picks which**: `ok`,
`degraded` (reduced capability, stays in rotation), `unavailable` (leaves
rotation). Postgres unavailable, **Redis only degraded** — every worker shares
one Redis, so failing readiness on it takes the whole fleet out at once while
every read still resolves from Postgres. That inverts "if Redis drops
everything the system is slower, never wrong". The first implementation got
this wrong and `scripts/chaos_bench.py redis-loss` is what caught it.

**Shutdown order is the whole of graceful shutdown** (`StateBackend.shutdown`):
readiness false → consumer stops claiming → drain up to
`SHUTDOWN_DRAIN_SECONDS` (30) → cancel the rest. Each is wrong without the
others: readiness last means the pod drains while still receiving traffic
(SIGTERM and Endpoints removal race), and draining before the consumer stops
refills the worker as fast as it empties. Keep `SHUTDOWN_DRAIN_SECONDS` below
`terminationGracePeriodSeconds`; a drain SIGKILLed halfway is worse than none.

**The correlation id is the investigation id wherever one exists**, which is
what makes it span workers — the job id already *is* the investigation id, so a
job claimed on another worker re-establishes the same id from the row.
`_execute` opens the scope; `submit` binds it so the request and its
`X-Correlation-ID` response header adopt it too.

Two things here are load-bearing and both fail silently:

- **`logger.configure` must set no `correlation_id` in `extra`.** loguru merges
  the configured extra into every record *before* the patcher runs, so a
  default there means `setdefault` never fires and every line logs the
  placeholder while looking exactly like a working correlation id. It shipped
  that way for one commit. A test calling `_inject_correlation({"extra": {}})`
  passes with the bug present — assert on records loguru actually produced, and
  specifically that **two scopes yield two different ids**.
- **The `ContextVar` holds a mutable holder, not a string.** Starlette runs the
  route handler in a child task, so `bind()` there lands in a context copy and
  the middleware would still return the inbound `req-…`. Mutating a shared
  holder crosses that boundary; `correlation_scope()` installs a *new* holder
  so a background investigation cannot rename the request that started it.
  Same family as `require_principal` having to stay `async`.

### Chaos and scale-out (`scripts/chaos_bench.py`, `scripts/scaleout_bench.py`)

Opt-in, needs Docker, not in CI — same precedent as `K8S_AGENT_INTEGRATION`.

```bash
docker compose up -d postgres redis
python scripts/chaos_bench.py all          # worker death, redis loss, postgres loss, drain
python scripts/scaleout_bench.py --workers 1,2,3,4
```

Three harness lessons worth keeping, because each produced a *passing* run:

- **A scenario needs a control.** The workers point at a cluster that is not
  there, so every investigation fails on collection regardless; the Redis
  scenario now compares status *and* error against a control run with Redis up.
- **Assert the scenario did something.** The drain scenario reported PASS while
  the process exited 0.2s after SIGTERM with nothing in flight. It now requires
  a logged drain and a shutdown longer than a second.
- **Making an investigation slow enough to interrupt took three attempts.** An
  unroutable IP returns `error: EOF` in milliseconds; a stalling listener makes
  kubectl prompt `Please enter Username:` and die on EOF just as fast. A
  stalling listener **plus a token in the kubeconfig** is what blocks.

**Scale-out is flat past two workers on the kubeconfig path, and that is the
host rather than the platform.** Each investigation shells out to kubectl ~15
times and process spawning is a host resource, so co-located workers compete
rather than add. It does not contradict the envelope's linear 1→2, which was
measured on the **agent** path where collection spawns nothing. Add workers on
an agent fleet; add hosts on a kubeconfig fleet. Cross-host is still unmeasured.

### Alerting (`deploy/alerts/k8s-agent-alerts.yaml`)

17 rules on burn rate rather than instantaneous violation. `tests/test_metrics.py`
asserts every series and every filtered label value in the shipped rules
appears in the **real exposition** — a rule naming a series the platform does
not export is valid YAML, passes `promtool`, evaluates forever and fires never,
which is exactly the Prometheus defect class from M9.1. A fourth test refuses
any rule mentioning a cluster, tenant or namespace label.

### Deployment and operations docs

Written in Tier 4 of the audit backlog. They record decisions, not just
procedures, so read them before changing the thing they describe:

| Doc | Load-bearing content |
|---|---|
| `docs/OBSERVABILITY_INTEGRATIONS.md` | What Prometheus must scrape, and the four defects found by running it |
| `docs/SSO_GROUP_MAPPING.md` | Okta and Entra worked examples; **Entra's >200-group overage silently empties the claim**, so those users get `RBAC_DEFAULT_ROLE` — `admin` on a default install |
| `docs/RUNBOOK_BACKUP_RESTORE.md` | The CA private key is the one irreplaceable file; losing only the enrolment store silently forgets revocations |
| `docs/DATA_PROTECTION.md` | What is stored, why there is no application-layer blob encryption, retention gaps, residency |
| `docs/SLO.md` | Proposed targets, **not measured attainment**; soundness is two-sided and queue depth means "add workers, not slots" |
| `docs/TENANT_USAGE_REPORTING.md` | Chargeback needs a `BYPASSRLS` role; the application role produces a clean empty report instead of an error |
| `docs/UPGRADE.md` | Forward-only migrations, what the drain does and does not cover, and the per-milestone behaviour changes |
| `deploy/helm/k8s-agent/README.md` | The chart reproduces the platform's startup refusals at render time; **the kubeconfig identity needs the `impersonate` verb** or every investigation fails pointing at the user's RBAC. Probes must stay on `/health/live` and `/health/ready` — they were on `/health` once, which made the whole readiness split inert in a Helm deployment — and the `preStop` sleep is what covers the Endpoints propagation window |
| `docs/MCP.md` | The **named** JSON-RPC subset, the four tools and their permissions, and what is deliberately not exposed |
| `docs/DEPENDENCY_GRAPH.md` | The `relation` set is closed and directional; no rule invents a node |

Two conventions worth keeping:

- **`scripts/tenant_usage.py` does not import `app`, deliberately.** Chargeback
  is cross-tenant and `system_scope()` is pinned by test to one caller; a
  reporting path inside `app/` would widen that hole. It reads the database as
  an operator tool, in the same category as `pg_dump`.
- **The Helm chart never pre-sets an insecure value**, and bundles no Postgres
  or Redis. Both are the same decision `docker-compose.yml` already makes.

## Notes

- Prompts are inline in `app/ai/prompt_builder.py`. The former `prompts/` directory described a loading convention that was never implemented and has been removed.
- **`.get(key, default)` does not defend against `null`.** Three instances so far.
  `EndpointSlice.endpoints` is `null` for a Service whose selector matches
  nothing, so `item.get("endpoints", [])` iterated `None`; the scheduler's fault
  boundary caught it and the investigation *succeeded* with the endpoint
  evidence missing on exactly the Service that had the fault. Note the sibling
  is **not** the same: `Endpoints` *omits* `subsets` rather than nulling it, and
  a probe using `.get()` cannot tell the two apart — which is how that one was
  briefly misdiagnosed as a bug. Check with `"key" in payload`, and write
  `or []` regardless.
- **`.get(key, default)` does not defend against `null`.** `kubectl config view
  -o json` succeeds on a kubeconfig with no contexts and returns
  `"contexts": null`, so the key is present, the default never applies, and the
  loop iterates `None`. That 500'd `GET /clusters` on a fresh container and on
  any agent-only fleet, and the console showed "Loading clusters…" forever.
  1,167 tests passed with it present; it was found by opening the page. Use
  `config.get(key) or []` wherever kubectl JSON is read.
- **The dead code is gone** (`app/ai/client.py`, and `start_investigation()` at the bottom of `investigation_service.py`); the live entry points are `LLMClient` and `InvestigationService.run()`. This note previously also listed **`app/kubernetes/inspector.py` as dead, and that was wrong** — it is the `Inspector` protocol plus `failure()`/`items()`/`usable()`, imported by eight modules, and deleting it on the strength of that note would have broken the collector layer. The `inspect_nodes()` stub it referred to no longer exists. Stale "this is dead" notes are worse than no note: they invite a deletion nobody re-checks.
