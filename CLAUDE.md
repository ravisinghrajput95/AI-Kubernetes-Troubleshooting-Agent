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
python -m evals         # reasoning + grounding regression report, no model called
python -m evals.live    # the same corpus, scored against the configured model
```

`evals/` is a golden corpus enforced by `tests/test_evals.py` and printed in CI. It exists because rules, prompts and grounding checks can all change without breaking a unit test while making the platform worse at reasoning. **The grounding corpus must keep cases that are expected to be *accepted*** — a corpus of only-rejections passes while an over-strict check has silently routed every investigation to the deterministic fallback. See `docs/EVALUATION.md`.

`evals.live` measures the one thing the offline corpus cannot see: **of the cases where the model actually answered, how many survived grounding.** That is the same failure stated the other way round — an over-strict check does not fail loudly, it routes everything to the fallback while 20/20 golden cases keep passing, and a prompt edit that degrades a real model has the identical signature. Gated on that rate; agreement with the deterministic ranking is *reported and not gated*, because the model is asked to select and explain and a defensible disagreement is not a defect.

**It refuses rather than skips.** No configured model is exit 2, never exit 0, and a run where every call failed is refused rather than reported as zero rejections — which is what it looks like. Both guards are unit-tested against a local HTTP stub speaking the chat-completions shape, reached through `LLM_BASE_URL`, so the gate is exercised on every CI run whether or not a key is set. The workflow decides whether the job runs; the program decides whether it passed.

Two values must be read at their seams rather than from the diagnosis, and both were wrong first: a failed call and a rejected answer both return `ai_generated: false` carrying the *deterministic fallback's own* grounding block, so the payload cannot tell an outage from a reasoning regression — the first version scored a total provider outage as twenty perfectly grounded answers.

Docker: `docker compose up --build` starts the backend, console, Postgres and Redis. The image installs a pinned `kubectl` and compose mounts `~/.kube/config` read-only (override with `KUBECONFIG_FILE`). The backend is published on a **fixed** `8000:8000`, because the console's bundle hardcodes that address and Docker's port allocator walks a range rather than handing out its low end — as a range this worked on the first `up` after a daemon start and drifted on every recreate afterwards. `docker compose -f docker-compose.yml -f docker-compose.scale.yml up --scale backend=3` is the multi-worker demonstration; it restores the range, and that file documents how to find the port a replica actually got. Local processes remain the getting-started path and need none of it.

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

**`AUTH_MODE` has no default, and that is a decision rather than an
omission.** It defaulted to `disabled`, which was never the open deployment it
read as — `disabled` has always also required `ALLOW_INSECURE_NO_AUTH`, and an
audit that scored this platform as shipping open was wrong about it. What the
default cost is subtler and real: the acknowledgement doubled as the mode
selection, so `ALLOW_INSECURE_NO_AUTH=true` **on its own** served every
endpoint unauthenticated with nobody having chosen `disabled`, and an
`AUTH_MODE` that failed to arrive — an unmounted ConfigMap key, an unloaded
`.env`, a misspelled variable — selected the insecure mode instead of
reporting itself missing. Absence selects nothing now, in the platform, in
`docker-compose.yml` (which used to pass `${AUTH_MODE:-disabled}` and teach the
one-variable form) and in the chart (whose `auth.mode` defaulted to `oidc` —
secure, and still the chart deciding). Do not reintroduce a default in any of
the three. `TestNoModeIsChosenForYou` pins the refusals and is the class that
used to argue the other way.

`docker-compose.yml` deliberately does **not** set `ALLOW_INSECURE_NO_AUTH` for
you. Pre-setting it was the "careless deployment" F13 warns about, shipped in
this repository — a `docker compose up` that publishes a port, authenticates
nobody, and supplies its own acknowledgement. Compose now refuses to start
until an operator chooses, and the refusal names the variable.

**Security status:** authentication (F13), per-tenant authorisation, tenancy under row-level security, rate limiting and an append-only audit log are all built — see the sections below. What remains is in `SECURITY.md` (*Known gaps*) and `docs/PRODUCTION_READINESS.md`, chiefly a development agent CA unless one is supplied and redaction being best-effort on free text. `AUTH_MODE` no longer defaults to `disabled` — it has no default and an unset value is refused at startup naming all three modes, because the old default made `ALLOW_INSECURE_NO_AUTH=true` sufficient on its own and let a mode that failed to arrive select the insecure one silently. This line said "chiefly no rate limiting" for several milestones after rate limiting shipped; a stale status line is the same failure as a stale "this is dead" note.

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

### Forking out of a process that holds gRPC (`app/__init__.py`, F22)

Every `kubectl` read is a `subprocess.run`, and gRPC installs
`pthread_atfork` handlers — so on a worker that also runs an agent gateway, the
child writes gRPC diagnostics to the stderr `capture_output` is collecting the
command's own error from. The read is still recorded as failed evidence and
nothing is misreported; what is lost is the reason, which reads
`ev_poll_posix.cc:593 FD from fork parent still in poll list`. Same cost as the
agent-path `unknown` that `detailFor` fixes.

**`GRPC_ENABLE_FORK_SUPPORT=0`, and where it is set is the whole fix.** The
variable is read when gRPC's core initialises, so setting it after `import
grpc` does nothing — measured at 0/40 polluted before the import, 40/40 after
it. `app/__init__` is the only module guaranteed to run before any `app.*`
module, `app/gateway/` included. **Do not add an import above that
`setdefault`**: it makes the fix inert while every other test passes, and the
symptom is an intermittent line in a stderr nobody reads. Nothing here uses
gRPC in a forked child — fork is immediately followed by exec — so the
handlers protect nothing.

**Intermittent, because gRPC skips its handlers when another thread is inside
gRPC at the moment of the fork.** A one-hour soak caught 3 of roughly 23,000
reads; a tight loop reproduces it 40 times in 40.

**And it only reproduces on macOS, which was established after the fix rather
than before it.** `ev_poll_posix` is the poll-based engine darwin uses; the
identical script against the identical gRPC gives 40/40 polluted on darwin and
0/40 in a `python:3.12-slim` container, with or without the fix. The soak ran
on the development machine, so on current evidence this never affected a
shipped Linux deployment — a finding measured on a laptop, attributed to the
platform. The line is kept because it costs nothing and makes local runs match
the containers, but the significance is development-environment, not
production. **This is also why the behavioural test cannot be the only
guard**: it skips where the defect does not reproduce, and pushed as the sole
check it reported SURVIVED in CI on Linux while being caught locally. `tests/test_forked_reads.py`
forks a real subprocess out of a real gRPC server and reads the stderr rather
than asserting the variable is set — the latter passes with the fix inert —
and carries a control requiring the defect to reproduce without the fix, or
both arms come back clean and the check proves nothing.

The suggested fix in the backlog, "give the subprocess its own stderr pipe
rather than an inherited fd", was already true: `capture_output=True` has
always given it a pipe, and the handler writes to that.

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

**`payload_bench` cannot see F5's remaining half, which is why that half went
unmeasured for so long.** Its fake overrides `KubectlExecutor.run`, so neither
`json.loads` nor `_cap_items` executes and `MAX_LIST_ITEMS` is never applied —
run it above 2,000 pods and it reports a stored result that keeps growing,
because in that harness nothing caps anything. It measures the *derived*
payload, which is what M8b wanted. `--parse-scan` measures the read instead,
through the real executor: peak parse is **5.9 MB at 2,000 pods, 29.7 MB at
10,000, 74.3 MB at 25,000** — ~2.95 KB per pod — while *retained* stays flat at
1.09 MB. The cap truncates a document already built in full, so it bounds the
payload and not the spike, and raising or lowering it changes nothing here. The
lever on a very large cluster is scoping to a namespace. Deferred deliberately:
at 10,000 pods four concurrent investigations transiently touch ~119 MB against
a 159 MB resident platform, while the ceiling that actually binds is per-worker
throughput at ~12/s with the worker 92% idle in socket waits.

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

**A revoked cluster refuses too, and revoked is not disconnected.** A revoked
agent leaves no presence record, so `holder()` says nothing and the fallback
read a local context that merely shares the cluster's name — the opposite of
what revoking asked for. `_agent_was_revoked()` closes it: refuse when the
cluster has a revoked certificate and **no unexpired unrevoked one left**. That
second half is the whole feature. An agent that dropped a moment ago still
holds a valid certificate and will reconnect, and refusing there would turn
every flap into an outage — which is why presence is TTL-based in the first
place. Re-enrolling lifts the refusal without un-revoking anything; a merely
*expired* certificate never triggers it. Fails closed on a store error, like
M8a's own refusal and unlike the rate limiter. Found by verifying revocation
against a live deployment, where the post-revoke investigation came back
`provider=kubeconfig`.

**Neither the routing nor the refusal requires *this* worker to run a
gateway, and making that true was F21.** The presence index and the enrolment
store used to be installed inside `if settings.agent_gateway_enabled` in
`app/state.py`, so on a worker without one `get_agent_presence()` was `None`,
`agent_affinity` returned the shared queue, and `select_provider` went straight
to `LocalKubectlProvider` — no presence lookup, no refusal, and no revocation
check either. The shipped topology cannot reach it (one Deployment, one config,
N replicas), but a fleet mid-way through enabling `AGENT_GATEWAY_PORT` can, and
there the guarantee was silently absent: tenant A's `prod` answered from a local
context that merely shares the name. Found by a soak that gave the first worker
a gateway and the second none — a third of investigations reported
`provider=kubeconfig` with an agent attached and no refusal anywhere.

**The split is by dependency, not by feature.** `install_fleet_index()` is
called from `build_state`, because presence is JSON in Redis and enrolment
records are rows — neither needs grpc, which is the only thing the gateway flag
was ever protecting against loading. What stays behind the flag, in *both*
callers, is the local `AgentRegistry` lookup: collecting **through** an agent
needs the stream, so it needs grpc. Refusing does not.

Two things that fix had to avoid. `_agent_was_revoked` **refuses when it cannot
read the store**, so moving it off the gateway flag put it on the single-process
getting-started path, where `get_enrolment_store()` lazily builds a file store —
an unreadable `AGENT_IDENTITY_DIR` would then have failed every investigation on
the simplest deployment there is. It is gated on there being somewhere an agent
could exist at all (`agent_gateway_enabled or distributed_state`). And
`test_selection_does_not_load_grpc_when_no_gateway_is_configured` asserted that
nothing under `app.gateway` was imported, which was the *package name* standing
in for the *cost*; it now asserts on grpc itself, with a control proving the
import watch can still see the gateway when there is one.

**Verified by reproducing the misconfiguration, not only by unit test.** Two
workers, one gateway between them, a real Go agent attached to the worker that
has it, six investigations submitted to the worker that does not: **6/6
refused**, each naming the holder. F21 reverted into the same harness: **6/6
answered `provider=kubeconfig`** with the agent attached elsewhere. The mutant
also showed a second symptom the unit tests cannot — `GET /agents` returned an
empty worker attribution, because presence had never been installed at all.

**What it costs**: a distributed fleet with no gateway anywhere now pays one
`agent_certificates` read per investigation that reaches the fallback, where it
previously paid none. That is the price of the fix rather than an oversight —
the query is indexed and single-row against a table that is empty in such a
deployment, and it is sub-millisecond beside a collection measured in hundreds.
The shipped topology already paid it, because every worker there has a gateway. With every
worker running a gateway, the same soak measured **100% agent-path collection
across two workers** — the first live number for M8a routing, against
synthetic ones before.

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
- **A record is matched back to its request by kind *and target*, never by kind alone.** A wave routinely holds several reads of one kind differing only by target — `LogsCollector` issues one `k8s.logs` per problematic pod — and nothing obliges an agent to answer them in the order they were asked. `fetch_many` used to group by kind and hand records out in arrival order, which filed one pod's logs under another pod's name: **5.5% of pod-log entries over an hour against a real agent**, counting only the ones detectable because the message named a different pod. A mis-paired *success* is the same defect with no trace at all — a diagnosis quoting the wrong container's output, with a citation. The information was on the wire the whole time: the agent copies `spec.target` onto every record it returns, refusals included. An unmatched request is now a gap naming what is missing, which is what the comment there always claimed.
- **kubectl rewrites list envelopes.** `kubectl get pods -o json` returns `kind: List`; the API server returns `PodList`. Evidence is therefore compared on objects, never bytes — see `tests/test_agent_transport.py`.
- **A `rest.Config` that leaves QPS and Burst at zero is not unlimited — it is client-go's 5/10.** One investigation issues ~20 reads, so everything past the tenth waited about a second and the agent logged `client-side throttling`; collection time tracked the rate limiter rather than the cluster. `--api-qps`/`--api-burst` (50/100) are applied **inside `loadConfig`, the only constructor**, not at the call site: as a separate step in `main()` they were correct, tested, and deletable without a single test noticing, because `main()` is not under test. Mutation-tested both ways — removing the call site survived until it was folded in.

`K8S_AGENT_CLUSTER_INTEGRATION=1` runs the differential suite against a real cluster (`kind create cluster --name m4b`); it skips otherwise, so `python -m pytest` still needs nothing. The suite creates the one workload it compares, in a namespace of its own, and removes it — it used to assume a pod called `web` already existed, so an otherwise-empty cluster produced three failures that looked exactly like a divergence.

**Nothing set that variable for six milestones** — not CI, not `integration_verify.sh` — so this suite ran when a person remembered, which is the standing the mutation tests had before `scripts/mutation_check.py`. It now runs in the `integration-verify` job, which builds the binary on the host and **checks how many tests ran rather than the exit status**, because a fully-skipped pytest run exits 0. Two holes in the suite closed on the way in: it now creates the pod it compares (three tests assumed one called `web` existed, and their failure was indistinguishable from a real divergence), and it **pins the agent's kubeconfig** — the binary has no `--context` and followed *current-context*, so the comparison rested on ambient kubectl state; with a decoy current-context, 23 of 36 fail unpinned and all pass pinned. Running it is what found `statusFor` mapping **every** 404 to `EMPTY`: a status the platform counts as *usable*, so an absent metrics-server read as "we looked and there is no usage" through an agent and "we could not look" through a kubeconfig, for the same cluster at the same moment. That inflates `evidence_coverage.completeness`, and with it the confidence of a diagnosis that saw less — the exact thing "missing metrics must never read as healthy metrics" forbids. A 404 on a **named** read means that object is gone (`EMPTY`); on a **list** read it means the API is not served here (`UNAVAILABLE`), and `policy.Read.Named` is what carries the difference.

### Reading as the caller, through an agent (`agent/internal/collectors/`)

F13's guarantee is that **the platform cannot see more than the calling user
can**, delivered by Kubernetes impersonation so the API server applies *their*
RBAC. It held on the kubeconfig path via `kubectl --as`. On the agent path the
caller travelled on the wire in `CollectionRequest.actor` and the agent
**discarded it** — every read ran as the agent's own broad-read ServiceAccount,
for any caller who could reach the platform. `collection.proto` documented the
opposite from the day it was written.

The agent now sets `Impersonate-User` / `Impersonate-Group` per request. Five
things are load-bearing:

- **Per request, never on the `rest.Config`.** One agent serves every caller
  through one shared client; an identity on the config would be whichever
  caller set it last, applied to whoever's read went out next.
- **Groups are repeated headers, not one joined string.** Joining them produces
  a group literally named `sre,oncall`, which matches no binding — so the read
  is refused and looks like the user simply lacking access.
- **Off unless `--impersonate`.** An agent enrolled earlier has no `impersonate`
  verb; sending the header anyway would have the API server refuse everything
  and `app/kubernetes/access.py` would blame the *caller's* RBAC. The enrolment
  manifest writes the flag and the ClusterRole grant in one document.
- **An impersonating agent refuses an unattributed read.** Falling back to its
  own ServiceAccount is the hole this closes, reachable by omitting one wire
  field. Same refusal `EVENT_SOURCES` makes by requiring a subject. The
  consequence: `--impersonate` and `AUTH_MODE=disabled` are not a working pair.
- **One decision, both providers.** `app/auth/impersonation.identity_for()` is
  asked by `_impersonation_args` and by `build_remote_provider`. They used to
  decide separately and disagree: the local path declined for an anonymous
  caller, the agent path sent `principal.subject` regardless — so an
  unauthenticated deployment asked the cluster to read as a user named
  `anonymous`. Pinned by a test that reads the **kubectl argv and the protobuf
  actor**, not the shared helper; comparing the helper to itself survives the
  mutation.

**A refusal has to name who was refused**, and that took a second fix.
client-go reports `unknown` for *every* error on a raw request — the agent reads
raw on purpose, so that is the normal path — while the API server's own sentence
sits in the body `DoRaw` returns alongside the error. `detailFor` reads it, and
filters client-go's placeholder precisely (`unknown`, or `unknown (get pods)`)
rather than by prefix, because `unknown field "spec.replicas"` is a real server
message. Without this every agent-path permissions problem was indistinguishable
from a broken cluster.

Proved against a real API server, not just on the wire:
`TestTheClusterAppliesTheCallersRbac` binds a user to one namespace and asserts
the cluster-wide read is refused, the namespaced read succeeds, and **the same
read through a non-impersonating agent returns the whole cluster** — the control
that makes the refusal mean something.

### Agent identity (`app/security/`, `app/gateway/identity.py`, `agent/internal/identity/`)

**The certificate is the identity.** An agent names itself exactly once — in `Register`, where a single-use token has already bound that name — and never again. Every `Connect` stream is placed by reading the peer certificate, carried as a URI SAN in SPIFFE form (`spiffe://<trust-domain>/cluster/<id>`; the CN is for humans and is never trusted).

Five things here are load-bearing:

- **`AgentHello.cluster_id` cannot override the certificate, and a contradiction aborts the stream** with `PERMISSION_DENIED` naming both values. Silently preferring the certificate is defensible against an attacker but not against a mistake: a wrong `--cluster` flag would file evidence under one name while the agent's own logs said another, forever. An *empty* hello is fine — the certificate supplies it.
- **The CSR contributes a public key and nothing else.** Subject, SANs and extensions are discarded; the leaf is built from the token's cluster binding. Its self-signature is verified, because a CA that skips proof-of-possession is a signing oracle.
- **Single-use is a conditional `UPDATE`** (`WHERE consumed_at IS NULL`) on Postgres — the same mutual exclusion as the job claim — or an in-process lock plus atomic replace on the file store. Tokens are stored SHA-256 hashed, never in the clear. Pinned by a *concurrent* test; a read-then-write passes every other assertion and fails that one.
- **Renewal is authenticated by the current certificate**, at 2/3 of its life, and **never touches the running stream**. The new material is swapped into a `Holder` that Go's `GetClientCertificate` consults per handshake, so the *next* dial picks it up while the open connection keeps the old certificate — still valid for the remaining third. That overlap is why rotation drops no in-flight collection and needs no restart. Do not revoke on renewal; that would kill the stream this design exists to protect.

  **The renewal *rate* is bounded by certificate life, not by how often the agent checks the clock, and it has to be.** The CA backdates `NotBefore` by five minutes for clock skew and `RenewAt` counts that backdate as life, so for any certificate lifetime under 150 seconds the renewal point is already in the past when the certificate is issued — and then every check tick mints another one. Measured against a real agent at `AGENT_CERT_TTL_HOURS=0.025` with `--renewal-check 5s`: twelve certificates a minute, indefinitely, each a CA signature and a row in `agent_certificates`. The agent cannot detect this by arithmetic — a certificate records when it became *valid*, never when it was *issued* — so it bounds what it can, at a third of the certificate's remaining life, fixed at the moment of an attempt rather than recomputed each tick (recomputing shrinks the gap as the certificate ages and converges on a quarter of the life instead of the third it reads as). It bounds attempts, not successes: a gateway refusing renewals is exactly when a retry loop costs most. `Settings.validate_agent_gateway()` warns below the floor, being the only side that holds both constants — the backdate lives in `app/security/ca.py`, the fraction in Go, and `MINIMUM_SENSIBLE_CERT_SECONDS` is derived from the CA's own value rather than typed, with a test asserting they have not drifted.
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

### The two providers must be able to serve the same reads (F7)

There are two lookup tables between a collector and a cluster — the agent's
kinds (`agent/internal/policy/kinds.go`, mirrored by `_KINDS` in
`app/providers/remote_agent.py`) and the resource name the collector wrote —
and until F7 nothing checked they agreed. **Eight of the deep-investigation
reads named a resource the agent had no kind for**: EndpointSlice, Ingress
(`ingresses` against a key spelled `ingress`), `configmap` *singular* against a
plural key, StorageClass, VolumeAttachment, ResourceQuota, LimitRange and
ServiceAccount.

**It degraded silently, which is why it lasted.** `spec_for` refuses, the
collector records a non-usable record, and the investigation *succeeds* with a
gap — so an agent-reached cluster produced a shallower investigation than the
same cluster read through a kubeconfig, and nothing compared the two. Same
shape as the M8a regression and the four Prometheus queries that parsed,
returned `success` and matched nothing.

`tests/test_provider_parity.py` is the fix that lasts: it **runs** every
collector against a recording provider and holds each read against
`kind_for()`, so a collector added later is covered without anyone remembering.
It carries its own vacuity guard (a recorder that saw nothing would satisfy
every assertion) and seeds the pod spec `ConfigReferenceCollector` needs,
because with an empty store that collector declines before issuing the two
reads the gap was found in. `describe secret` is the one **named** exception:
`describe` is kubectl's renderer, not an API read, and reproducing it in Go is
the mistake `ResourceMetricsCollector` already refused to make for `kubectl
top`. Adding a kind means changing three places — `_KINDS`, `kinds.go`, and the
ClusterRole in `app/api/agents.py`.

**The kind can be right and the read still wrong, because a kind is not the
whole request.** `spec_for` also sends `ResourceRequest.options` as string
parameters, and the agent compares those literally
(`parameters["previous"] == "true"`). Python's `str(True)` is `"True"`, so
**every boolean option arrived as a value the agent tests for and never
matches** — and the one boolean it reads is `previous`. Losing it does not fail
the read: the log endpoint simply serves the *current* container. So the agent
path recorded the running container's output under `k8s.pod.logs.previous`,
status OK, counted toward completeness — evidence labelled "the container
instance that existed before the last restart" holding the one after it, on
exactly the CrashLoopBackOff investigations where the previous instance is the
only thing that says why it crashed. Booleans now serialise lowercase in
`_parameter_value`, which is the whole fix.

Found the same way as the `OutputFormat.TEXT` defect on the *baseline* log
read: an agent-served investigation beside a kubeconfig-served one of the same
namespace in the same minute, diffed by evidence id and status. Before, one
status differed and coverage read 39/48 against 40/48; after, **57 records and
zero differences**. Neither the differential suite nor `test_provider_parity`
could see it — the first compares the reads it names and this is not among
them, the second held every read against `kind_for()` and the kind was correct.
What was wrong was a parameter, and nothing compared parameters.

**So parameters are compared now, and that found a second one.**
`test_every_parameter_the_platform_sends_is_one_the_agent_reads` greps the keys
`kinds.go` actually reads and holds the platform's emitted set against them,
with known-ignored keys listed with reasons the way `UNSERVED` lists unserved
reads. `output` is on that list and is benign — the agent knows logs are text
without being told. **`all_containers` was on it and was not** — that
was F24, now fixed. kubectl expands `--all-containers` client-side by reading
the pod and fetching each container's log; the agent issued one read naming no
container, and the API server answers a multi-container pod with `BadRequest: a
container name must be specified`, so **any pod with a sidecar lost its logs
entirely on the agent path** while the same pod read through a kubeconfig kept
them.

`Collector.collectEveryContainer` performs the same expansion. Three things
carry it: **every read still goes through `policy.Resolve`** — the pod read and
each per-container log read alike — so the expansion adds no capability and
cannot reach a path the policy package would have refused; **kubectl's
container order, init containers first**, established against a live cluster
rather than assumed, with a silent container contributing nothing and not being
an error; and **the first container's error becomes the read's error**, which
is what keeps `PodPreviousLogsCollector`'s mapping of "previous terminated"
onto EMPTY working identically on both paths.

Proved by reverting it into the live harness. With the defect present the
sidecar pod's log entry reads `success: false` and `a container name must be
specified for pod ..., choose one of: [app sidecar]`; with the fix it reads
`APP-BOOT / FATAL-app-died / SIDECAR-PROXY-READY`, which is `kubectl logs
--all-containers=true` byte for byte. A scoped differential over the same pod
gives 55 records and **zero status differences** between the two providers.

**A discovery client in the agent was considered and not built.** Every group
version the table hardcodes (`apis/networking.k8s.io/v1`,
`apis/discovery.k8s.io/v1`, `apis/storage.k8s.io/v1`, `apis/batch/v1`,
`apis/metrics.k8s.io/v1beta1`) is GA on every supported Kubernetes release, so
a resolver would compute the path it already has, at the cost of a startup
dependency on a call that can fail. What the assumption lacked was evidence,
not machinery — so `verify_deployment.py` checks all 24 entries against a live
cluster's discovery document in the required CI job, and the release that moves
a version fails there rather than in a customer's degraded investigation. The
same reasoning as F6: answered, not as asked.

`AgentSession.supported_kinds` is **reported, not planned against** — its
docstring claimed otherwise for several milestones. Asking for an unknown kind
returns a `NOT_APPLICABLE` record naming it, which is the citable gap; skipping
the request would produce the same gap with no record of what was skipped.

### Reusing a cluster read (`app/providers/cache.py`, F18)

Every investigation used to collect the whole cluster from scratch — ~20 reads,
each a kubectl subprocess — so two investigations a minute apart did identical
work. Measured on a real 53-pod cluster: a second investigation now spawns
**13 kubectl processes instead of 70** and spends **0.16 s collecting instead
of 0.57 s** (`scripts/cache_bench.py`, numbers in the envelope).

**A cache that lies is worse than no cache, and here "lies" is specific.** Every
conclusion cites an evidence id and every record carries a `collected_at`; a
record dated *now* for a fact read forty seconds ago is a false citation. So:

- **It sits at the `ClusterProvider` seam**, wrapping whatever `select_provider`
  chose. Not in collectors — a collector that knew about caching could tell
  which provider it had, which is what removing `raw_executor()` guaranteed
  against. Not in the evidence store either: that would cache *conclusions*.
- **The key is `(tenant, provider class, cluster, impersonated identity)` plus a
  fingerprint of the request derived from `dataclasses.fields`.** The tenant
  because `AgentRegistry` is tenant-keyed for the reason two customers may both
  call a cluster `prod`. The identity because with impersonation on the cluster
  applies the *caller's* RBAC, so the same read has different correct answers
  per caller — one of them a refusal. The fingerprint is derived rather than
  enumerated so a field added to `ResourceRequest` later cannot silently
  collide two different reads.
- **Only successes are stored.** A cached `FORBIDDEN` would keep refusing after
  the RBAC that caused it was fixed, and `app/kubernetes/access.py` reads
  exactly those statuses to tell a locked door from a broken cluster. On the
  real measurement *every one* of the warm run's 13 misses was a failure.
- **Evidence is dated by the read, never by the run.** `FreshnessWindow` is a
  **mutable holder** in a `ContextVar`, opened per collector by the scheduler
  and written by the provider — because `LocalKubectlProvider.fetch_many`
  gathers into child tasks and a context copy would discard a rebound value.
  Same family as `require_principal` staying `async` and `correlation_scope()`
  installing a holder. `_sanitize` backdates only: a collector that mixed
  cached and live reads understates its freshness, which is the safe direction.
- **`underlying()` exists so `cluster_access` still names the transport.** A
  wrapper is neither an agent nor a kubeconfig, and reading
  `type(self.provider)` through one reports every investigation as
  `kubeconfig` — the exact M8a regression `cluster_access_total` was added to
  make visible.
- **A cached payload is re-parsed from text on every serve.** Handing one dict
  to two investigations means one collector's mutation rewrites the other's
  evidence, and redaction runs *above* this layer. `json.loads` costs far less
  than a subprocess, which is the whole point.
- **`executed_commands` and `truncations` survive the cache.** A warm
  investigation whose command list shrank would look like one that examined
  less of the cluster; a lost truncation would make it claim it saw a whole
  cluster it saw the first 2,000 objects of.

**It applies to the agent path identically**, and that is an argument rather
than an oversight: the freshness contract is a property of the evidence, not of
the transport, and a cache that behaved differently per provider would make an
agent investigation and a kubeconfig one non-comparable — what
`test_metrics_parity.py` exists to prevent. It is also where a round trip costs
most.

**In this process only, never Redis.** "Redis is the latency layer, Postgres is
the truth" means every message has a committed row behind it; a cached read has
none, so it would be the Redis-only fact that rule forbids — and it would put
megabytes of unredacted cluster interior in a shared store.

`COLLECTION_CACHE_TTL_SECONDS` (60, `0` restores the pre-cache code path
exactly — `with_cache` returns the provider untouched) and
`COLLECTION_CACHE_MAX_BYTES` (64 MB, LRU by bytes because a node list is
kilobytes and a pod list is megabytes). `POST /investigate` and
`/investigations` take `refresh: true`; **an alert-triggered investigation
always sets it**, because an alert is a claim that the cluster just changed.
`refresh` bypasses *reading* and still writes, so a storm does not leave the
cache cold for the operator who looks straight afterwards.

`investigation["collection_cache"]` reports hits, misses and the age of the
oldest reused fact, and the console renders it in the Evidence Explorer — same
rule as the SSE-versus-polling transport: a cheaper path has to be visible.
`k8sagent_collection_cache_reads_total{outcome}` is the ratio; the *age* is
deliberately not a metric, because that would need the cluster as a label.

### Collection (`app/collectors/`)

Collectors declare `provides` / `requires` / `optional_requires`; `CollectorRegistry.resolve()` topologically sorts them into waves. `requires` must have a registered provider (a missing one raises `CollectorGraphError` at resolve time); `optional_requires` only affects ordering when a provider exists, which is how optional backends like Prometheus stay absent without breaking the graph.

`CollectionScheduler` guarantees three things regardless of collector behavior:

- A collector that raises, hangs, or exhausts the budget degrades **only its own** evidence.
- Every declared kind lands in the store, worst case as a non-usable record explaining the gap.
- **Redaction happens here, at the collection boundary** — so reports on disk, the HTTP API, and the LLM all see the same scrubbed payload. Do not reintroduce redaction at the prompt boundary; that leaves the persistence and API paths uncovered.

The inspectors are **adapted, not rewritten** (`app/collectors/kubernetes.py`). `InspectorCollector` runs one inspector — fetch what it declares, then let it analyse — and maps the established `{"error": ...}` contract onto evidence status through `app/kubernetes/errors.py`. Everything except pod logs is independent and runs as one concurrent wave; logs form a second wave because `PodLogsCollector` declares `requires={PODS}`.

**A log read must say `OutputFormat.TEXT`, and the baseline one did not.** `OutputFormat` defaults to JSON and that default is what decides whether `KubectlExecutor` calls `json.loads` on the output — so on the kubeconfig path the baseline pod-log read *failed for every pod that had anything to say* and succeeded for the silent ones, whose empty output parsed as `{}`. The failure carried no reason: kubectl exited 0 with an empty stderr. Exactly inverted — the pods whose logs matter are the crashing ones. `PreviousPodLogsCollector` had it right and the baseline read did not, which is why nothing compared them. Found by putting an agent-served investigation next to a kubeconfig-served one of the same cluster in the same minute: the agent path was unaffected, so this was a provider divergence on the single most useful piece of evidence a CrashLoopBackOff has.

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

`PROMETHEUS_TENANT_ID` / `LOKI_TENANT_ID` send `X-Scope-OrgID`. Multi-tenant
Loki, Mimir, Cortex and Thanos all refuse a query without it, so a deployment
pointed at one saw every query fail; the client recorded that correctly as
`unavailable`, which made the failure legible while leaving no way to succeed.
**Configuration, never the ambient platform tenant** — there is one `LOKI_URL`
for the deployment and the platform's tenant ids are its own namespace, so
mapping them onto a customer's org ids would be the platform guessing at
someone else's tenancy scheme. Same reasoning that makes `EVENT_SOURCES` carry
the tenant in configuration rather than read it from the payload. Unset sends
no header at all, because a single-tenant backend rejects one it did not
expect. Asserted on the header that reached the wire through `MockTransport`: a
`headers` property that is correct and never passed to `AsyncClient` reads
identically to a working one in a test that inspects the object.

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

`ReportRenderer` (`app/reports/rendering.py`) renders the three formats and `history_service.py` hands the **bytes** to a `ReportStore` (`app/services/report_store.py`) — filesystem or Postgres. It returns bytes rather than a path because `/investigations/{id}/pdf` may be served by a worker that never rendered the file; that is also the seam M8 swaps for object storage, changing one method and no endpoint. The PDF is hand-rolled object emission (`_pdf_bytes`, base-14 fonts, no PDF dependency), so section bodies are flattened via `ReportSection.as_lines()` and text must be pre-wrapped and non-ASCII escaped. On the filesystem backend `history.json` is capped at 25 entries; on Postgres the 25 is a query limit and nothing is discarded. `POST /investigations/{id}/regenerate` re-renders all three from stored JSON without re-querying the cluster — so improving the composer improves historical reports too.

**The split is what the file's three jobs already were.** `history_service.py` was 998 lines composing, rendering and indexing, and the dependency only ever ran one way — rendering never reached back into persistence — so the seam was already there. What is *shared* is deliberate: severity, incident status, environment and incident id are the same facts on the history row and in the PDF, and the index calls the renderer's derivations rather than repeating them, because two answers to "how severe was this" would eventually disagree about the same investigation depending on which screen you were on. Verified as a pure move by rendering one investigation through both versions: the PDF, JSON and Markdown came back byte-identical.

### Onboarding and the fleet API (`app/api/agents.py`)

`/clusters` merges kubeconfig contexts with agents connected to this worker; each entry carries `connection` (`agent`/`kubeconfig`) and, when present, an `agent` block. A cluster reached only by an agent has no kubeconfig entry and would otherwise be invisible.

**The agent stores its identity in a Kubernetes Secret, not a volume.** A PersistentVolumeClaim does not work everywhere clusters actually are — EKS on Fargate has no EBS so a ReadWriteOnce claim never binds, a cluster with no default StorageClass does the same, and a zonal volume cannot follow a rescheduled pod. Every one of those failures looks identical from outside: a pod stuck in ContainerCreating with nothing in the agent's logs, because the agent never starts. `identity.Store` is an interface; `FileStore` serves `docker run` and a laptop, `SecretStore` is the in-cluster default. This does **not** weaken the read-only guarantee: the ClusterRole is still get/list/watch, and a separate namespaced Role grants `get`/`update` on exactly one Secret by name — the agent's own credential.

`POST /agents/enrolment` mints a single-use token and returns an apply-able manifest (namespace, ServiceAccount, a ClusterRole granting `get`/`list`/`watch` only, Deployment). **It refuses outright when `AUTH_MODE=disabled`** — an unauthenticated endpoint that enrols clusters is worse than the problem it solves — and points at `agentctl` instead. `agent/Dockerfile` builds the image the manifest references (distroless, non-root, no shell).

**On more than one replica, the console reads a shared index, not its own registry** (`app/gateway/presence.py`). `AgentRegistry` is per-process by necessity, so `GET /agents` used to answer from whichever pod the load balancer picked — thirty clusters behind three replicas showed about ten, and a different ten next refresh. Each gateway now announces its agents into Redis with a 45s TTL, refreshed by the heartbeat, and the API returns the union. Expiry rather than deregistration, because a killed worker cannot deregister and phantom agents are worse than a few seconds of staleness. Every record carries `worker` and `local`. **M8a made presence routing as well as visibility**: the submit path asks `holder()` who owns the stream and queues the work there, so an agent held by another replica *is* investigated through that replica. What the record still cannot do is let *this* worker collect through someone else's socket, which is why `select_provider` refuses rather than falling back.

**"Online" is heartbeat-derived, not socket-derived.** An idle stream and a half-open one look identical from the platform's side, so the gateway pings every 15s and the agent's `AgentHealth` reply refreshes `last_seen`; `AGENT_STALE_SECONDS` (45) decides staleness. Do not replace this with "the stream is open".

### Report retention

`ReportStore.prune()` deletes rendered artefacts older than `REPORT_RETENTION_DAYS` (14), swept every `REPORT_RETENTION_SWEEP_HOURS` (6) by a task started in `app/state.py`. **The history entry survives and is marked `expired`** — deleting it too would make an investigation that happened look like one that never did. 0 disables pruning.

**It nulls `investigations.result` in the same transaction, and that is the
larger copy.** Retention used to delete only the blobs, so the JSON payload they
were rendered *from* — 2.7 MB against a couple of hundred kilobytes — survived
indefinitely: `/investigations/{id}/pdf` 404'd on an expired investigation while
`GET /investigations/{id}` still served its whole contents. The same data
deleted on one path and kept on another, under a setting an operator reads as a
deletion schedule. `docs/DATA_PROTECTION.md` recorded it as a gap and offered a
hand-run `UPDATE`; the gap was that `prune()` did not run it. Nulled rather than
deleted, for the same reason the history entry survives. **This deletes payloads
on upgrade that were previously kept** — see `docs/UPGRADE.md`.

### Frontend

`src/App.tsx` is **98 lines and holds the shell only** — the authenticated
routing table and the sign-in gate. **All work goes in `src/routes/`,
`src/components/`, `src/hooks/`, and `src/lib/`.**

The last split (1,053 → 98) was a *pure move*, verified the way the
`history_service` split was: every extracted function compared byte-for-byte
against its original, and the built bundle came back on the same content hash.
Trimming the imports the move orphaned then took the app chunk from 28.94 KB
gzipped to 28.76 — dead imports were not being tree-shaken away, which is
exactly the kind of thing a passing `tsc -b` says nothing about (there is no
`noUnusedLocals` here).
What it corrected beyond size is a **backwards dependency** — `ReportsPage` and
`routes.test.tsx` imported `HistoryTable` and `InvestigationPage` *from*
`App.tsx`, so two routes reached into the shell that mounts them. Both now live
in `src/routes/`.

The remediation builders went to `src/lib/remediation.ts` and **gained their
first tests in the move, which is the argument for it**: `buildRemediationYaml`
writes a manifest a person is invited to apply to a production cluster, and
reaching it used to mean rendering a panel, clicking a tab and reading a
`<pre>`. Eight mutations against it, including the two that had already shipped
— `diagnosis?.root_cause.toLowerCase()` guarding the diagnosis and not the
field, and a Pod handed the Deployment manifest's `spec.template` nesting.

Two components left entirely: `MultiClusterPanel` and `investigationEvidence`
were referenced nowhere in `src/`, superseded by `FleetPage` and
`lib/report.evidenceIndex`. Checked by grep **and** by the build, per the
standing warning about stale "this is dead" notes.

`/connect` (`ConnectClusterPage`) is the onboarding flow: name a cluster, mint an enrolment, copy the manifest, watch for the agent to check in. `AgentDot` renders agent reachability in three states — online, degraded, silent — and never in colour alone.

Investigations run through `useInvestigationJob` (`src/hooks/`), not a React Query mutation. It posts to `/investigations`, streams progress over SSE, and **falls back to polling** when `EventSource` fails — corporate proxies commonly block it, and a stalled screen during an incident is worse than a slower one. Both paths converge on one terminal `GET /investigations/{id}` for the full result. `transport` is surfaced in the UI so a degraded path is visible.

Pure logic lives in `src/lib/analysis.ts` (grouping, filtering, severity ordering, formatting) so it is testable without rendering. Panels stay presentational.

HTTP goes through `src/services/http.ts`, a small `fetch` wrapper — **there is no axios**. It cost 16.7 KB gzipped, more than the console's entire own code, for eight JSON calls. The wrapper keeps what was actually used (base URL, 120s timeout via `AbortController`, JSON encode/decode, throw on non-2xx) and adds a typed `ApiError` carrying `kind` (`network` / `timeout` / `http`) and `status`. Do not reintroduce an HTTP client library without a reason beyond convenience.

`vite.config.ts` splits `react` and `query` into separate chunks. Total bytes are unchanged; the point is that a deploy only invalidates the ~16 KB app chunk instead of all ~88 KB. Note the `manualChunks` **function** form is required — the object form does not capture subpath imports such as `react-dom/client`, which silently leaves react-dom in the app chunk. Do not code-split the app's own panels: they are a small fraction of the bundle and each extra chunk costs a round trip.

Two properties are load-bearing and must not regress:

- **Never display evidence the backend did not report.** `ConfidenceEvidence` previously fell back to a hardcoded `["Events", "Pod Logs", …]`; panels now render an empty state instead. In a product whose premise is that nothing is asserted without evidence, placeholder content is a correctness bug.
- **Progress is real.** The old `progressSteps` array advanced on a 900ms timer with no relationship to backend work. Every row in `LiveTimeline` is an event the backend actually emitted.

Both are pinned by `src/components/panels.test.tsx`, along with the cache-and-transport visibility rule, and each is mutation-tested against the defect as it actually shipped. **Assert on what only a real value can produce, not on prose**: "the words 'evidence strength' are absent" fails when someone edits the panel's subtitle — which names the three weights — and passes when a placeholder row is reinstated. It asserts on the score-times-weight arithmetic instead.

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

### Sustained operation (`scripts/soak_bench.py`)

Every other harness here finishes in seconds and answers "how fast". This one
answers "what happens if you leave it on", which is the question
`docs/PERFORMANCE_ENVELOPE.md` had recorded as unmeasured for nine milestones.

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 postgres:17-alpine
docker compose up -d redis
(cd agent && go build -o /tmp/k8s-agent ./cmd/agent)
python scripts/soak_bench.py --minutes 60 --concurrency 2 --pause 12 --agent
```

Four things are watched because each is a claim no short run can test: resident
memory with the F18 cache on, whether the retention sweep fires and what it
costs, whether certificate renewal survives repetition under load, and whether
the SSE and polling transports produce the same outcomes.

**The vacuity guard is why the output is worth reading.** A soak of a platform
doing nothing reports beautifully — flat memory, no errors, no leak — and every
harness failure in this repository has had that shape. The run *refuses* to
publish unless the platform was actually working, which takes **three**
questions, because each admits a run the other two accept:

| check | question | why the others miss it |
|---|---|---|
| volume | did enough happen to trend from? | a share is meaningless over ten runs |
| share | was it *working*, or failing most of the time? | a floor cannot see a rate |
| continuity | was it working *throughout*? | a rate cannot see when it stopped |

**The first version had only the floor, and it published.** Docker Desktop
killed the kind cluster four minutes into a 60-minute run; 81 investigations
out of 1,172 collected usable evidence — a platform failing 93% of the time —
and 81 cleared a floor of `max(20, minutes)`. Every memory trend in that report
was taken from an hour of `Unable to connect`.

The share alone would have caught that one. **Continuity is for the shape it
cannot see**: offered load drops when a cluster dies slowly, so a run can hold a
high success *rate* while measuring nothing after minute ten. It is the gap
between usable investigations, and **both edges count** — the first version
built its gap list only *between* good runs, so run 3's "last success at minute
4 of 60" reported a longest gap of six seconds. That check was written the same
day, looked right, and was inert; it was caught by driving the guard with the
real run's shape rather than by reading it. Allowed gap is
`max(5 minutes, 10% of the run)`.

`tests/test_soak_guard.py` pins all three, each against a run the other two
accept, plus the case that matters just as much: **a healthy hour must still
publish.** An over-strict guard is not the safe direction here — it means no
soak ever produces a number, which is indistinguishable from not running one.

**The report says why things failed, and that is what let the first one look
publishable.** "failed 1091" with no breakdown is a number; `1091x Unable to
connect to the server` is a diagnosis, and it was in every one of those runs the
whole time. Grouped by shape, the way `log_findings()` already grouped log
noise, and printed **above** the guard — a refused run is precisely the one
whose failures someone needs to read. Correlation ids collapse too: they are on
every log line, and their four-character groups survive an eight-or-more hex
rule, so a five-minute smoke run reported six findings at "1x" each — six
investigations rather than six findings.

**The headline run uses a production-like certificate TTL.**
`--cert-ttl-hours` defaults to 0.5, which renews about three times in an hour:
enough to say rotation survives repetition under load, which is the claim,
without the CA becoming the subject. 0.025 (90 seconds) is kept as the
pathological setting for stressing renewal itself — it mints a certificate every
few seconds for the reason recorded under agent identity, and 120 renewals an
hour measures the CA, not the platform. The shipped default is 90 days and would
renew never.

Four things it had to be taught, three of them by being wrong first:

- **The caller needs RBAC, or the whole run is vacuous in the most convincing
  way available.** Impersonation is on by default, the soak's subject is in no
  binding, every read comes back FORBIDDEN, and the platform correctly reports
  a locked door — for an hour. The ClusterRole it binds is lifted from the
  platform's *own* enrolment manifest rather than written here, because a third
  list of resources is the mistake F7 already cost eight reads.
- **Every worker runs a gateway, because that is the shipped topology.** Giving
  one worker a gateway and not the other made a third of investigations fall
  back to the local kubeconfig — which measured the harness. It also found F21.
- **SSE sequences are not contiguous.** `investigation_events.seq` is a
  `bigserial` shared by every investigation in the database, so one stream's ids
  are naturally sparse and the first version reported a 10% "gap rate" that was
  other investigations interleaving. The properties that exist are monotonicity
  and uniqueness.
- **The retention sweep's cost needs the store the platform actually installs.**
  `get_report_store()` returns the *filesystem* store — the Postgres one is a
  startup step, not something `DATABASE_URL` implies — so the first measurement
  reported "0 blobs in 0 ms", a plausible number for a store that was never
  pointed at the rows.

**Probes fail soft, and that is not defensive coding.** The Docker daemon on
the development machine died three times during this work, twice mid-run,
taking Postgres with it — and because every probe read the database, an
unreachable database ended the run with a traceback and reported *nothing*.
Fifty minutes of measurement lost to the instrumentation rather than to the
subject. The run now ends with a `TRUNCATED` line naming when and why, and an
absent sample is recorded as absent rather than as a database that shrank to
zero. Same rule as `_safe()` in `app/observability`.

**Load is a sustained rate, not a peak one.** 180 investigations a minute
against one kind cluster is a stress test of Docker Desktop, not a workload;
it took the daemon down twice before an hour was reached. An hour is the point.

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

18 rules on burn rate rather than instantaneous violation.

**Two rules cover M8a, at opposite ends of the same guarantee, and the pair is
the point.** `InvestigationsFallingBackToLocalKubeconfig` fires at a 10%
kubeconfig share — routing *broken*, which before M8a was two thirds of
agent-cluster investigations. `AgentPresenceUnreadableEnoughToMisroute` fires
at 1% of a different series, because the refusal fails **open** when presence
cannot be read and a soak measured that at 1 investigation in 1,168 (0.086%),
~116x below the first rule. Lowering that threshold instead would not work:
`cluster_access_total` records how the cluster *was reached*, so a fail-open
and a correct local read are both `provider="kubeconfig"`, and a 0.5%
threshold would fire on any deployment holding a few genuinely-kubeconfig
clusters. The fail-open is therefore counted where it happens —
`k8sagent_agent_presence_failopen_total`, unlabelled, exported from import.
Its test carries a **control** (a readable index holding nothing must not
count), because a recorder called unconditionally passes the positive
assertion while making the series mean nothing. `tests/test_metrics.py`
asserts every series and every filtered label value in the shipped rules
appears in the **real exposition** — a rule naming a series the platform does
not export is valid YAML, passes `promtool`, evaluates forever and fires never,
which is exactly the Prometheus defect class from M9.1. A fourth test refuses
any rule mentioning a cluster, tenant or namespace label.

### Mutation tests, re-run rather than remembered (`scripts/mutation_check.py`)

Every invariant here was mutation-tested by hand — revert the defect, watch the
check go red, restore. In one session that found **seven defects, three of them
in checks that had just been written**, looked correct, and guarded nothing.
It is also the discipline that decays first: a passing suite feels like
evidence, and a mutation not run leaves no trace.

```bash
python scripts/mutation_check.py          # 32 mutations
python scripts/mutation_check.py --list
```

Each entry pairs **a defect that actually shipped** with the test written to
catch it, and the run fails if any test passes with its defect present. Runs in
the `backend` CI job on 3.12.

**Deliberately not `mutmut` or `cosmic-ray`.** A general fuzzer mutates
everything and grades the whole suite, which here would spend minutes
rediscovering that most lines are covered and produce a score nobody acts on.
What is worth keeping is the narrow pairing — a regression suite for the tests
themselves.

**An anchor that matches other than exactly once is an error, never a skip.**
A mutation that fails to apply reports "survived" identically to a missing
test, and that is not hypothetical: the SSE ownership entry refused to apply on
its first run because the same check appears twice in that file. Re-anchor it;
never delete it.

### Integration verification (`scripts/integration_verify.sh`, `deploy/verify/`, CI)

The `integration-verify` CI job stands the chart up on kind — ingress-nginx,
metrics-server, a prometheus-operator Prometheus, and Postgres and Redis
deployed *beside* the chart because it bundles neither — then asserts 32
properties against the live deployment. **Required, not opt-in**: a job allowed
to fail is the same as no job, and this defect class has reached `main` four
times with a green suite.

**The dividing line against `K8S_AGENT_INTEGRATION`**: that suite runs *our
code* against real Postgres and Redis in one process and asserts *our*
contracts. Everything here needs a **second product to agree with us** —
Prometheus's parser, nginx's buffering, the kubelet's probe path, the
operator's label selector — and is only observable from outside the pod. If an
assertion can be made by importing `app`, it is a pytest and belongs there.

It is a script rather than workflow steps so **the thing CI runs is the thing a
laptop runs** (`scripts/integration_verify.sh --keep`); every defect it exists
to catch was found by hand. `--kube-context` is pinned on every command.

Every check carries a guard against its own subject being absent, because the
recurring failure in this repository's harnesses is a *vacuous* assertion, not
a wrong one: **zero scrape targets is not "no unhealthy targets"**, the
alert-series check refuses fewer than 13 referenced series, the SSE check
compares arrivals against the platform's *own* emission timestamps rather than
a wall-clock constant, and a `succeeded` investigation that collected nothing
is refused.

**The SSE check does not pin `X-Accel-Buffering: no`, despite being written
to.** Mutation testing showed the header's removal survives: ingress-nginx
defaults to `proxy_buffering off`, and even with buffering on nginx forwards as
buffers fill rather than holding the response, so at this traffic shape the
header is not observable. It verifies incremental end-to-end delivery through a
real proxy — which `TestClient` cannot check at all — and claims nothing more.

`deploy/verify/prometheus.yaml` reproduces kube-prometheus-stack's
**restrictive** `serviceMonitorSelector` (`release:`) rather than the permissive
`{}` — so `metrics.serviceMonitor.labels` is on the path under test. Mutation
tested by reverting `2f60f76` into a rebuilt image: 32/0 healthy, **27/4 and
exit 1** on the mutant, 32/0 restored. See `docs/INTEGRATION_VERIFICATION.md`.

**The agent leg submits its investigations on the worker holding the stream**,
via `kubectl exec`, and that is the whole of its value. Submitted through the
ingress the same check passed 6/6 against a rebuilt image with M8a's affinity
fix reverted: on four replicas three quarters of submissions land on a
*non*-holder, where `holder()` answers correctly. The defect only bites on the
holder, where the missing local-registry check sends the job to the shared
queue — 3 of 4 failed there with "attached to worker &lt;the one that accepted
it&gt;". Three rounds put a mutant's survival below 2%.

**Revocation is asserted on behaviour, not on the store.** The agent's
certificate is revoked with `agentctl` and the check requires it to stop
serving — with the three preceding agent investigations as its control, so
"no longer serves" cannot be satisfied by an agent that never worked. Mutation
tested by making `_sweep_revocations` return immediately: **43/2 and exit 1**,
with "the certificate is revoked" still passing. Revoking succeeded; only the
live stream ignored it, which is the whole distance between a revocation list
and revocation taking effect.

**Every check runs under `guarded()`**, which turns an exception into a
recorded failure. The routing check crashed on `None > 0` reading `usable` from
a *refused* investigation, ending the run with no summary while correctly
detecting the defect it was pointed at. A check that could not run has not
passed, and a verdict beats a stack trace — same reasoning as `_safe()` in
`app/observability/`.

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
- **The dead code is gone** (`app/ai/client.py`, and `start_investigation()` at the bottom of `investigation_service.py`); the live entry points are `LLMClient` and `InvestigationService.run()`. This note previously also listed **`app/kubernetes/inspector.py` as dead, and that was wrong** — it is the `Inspector` protocol plus `failure()`/`items()`/`usable()`, imported by eight modules, and deleting it on the strength of that note would have broken the collector layer. The `inspect_nodes()` stub it referred to no longer exists. Stale "this is dead" notes are worse than no note: they invite a deletion nobody re-checks. **A third one has since been caught**: P3 listed `FixRecommendationEngine` as vestigial, and it is live on `RootCauseAnalyzer._fallback()` — the path taken with no `OPENAI_API_KEY`, on a failed model call, and on a grounding rejection — where `prevention`, `next_steps` and `kubectl_commands` come from nowhere else. A fallback diagnosis's `prevention` and `next_steps` are byte-identical to `recommend()`'s; only `fix` is usually superseded, by the hypothesis's `remediation_hint`, which is probably what the note saw. Three for three, the way to check is to run the thing.
