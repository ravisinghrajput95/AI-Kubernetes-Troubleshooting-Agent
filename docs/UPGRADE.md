# Upgrade and migration

Backlog item 36. How to move a running deployment to a new version, what is
safe to do live, and the specific places where an upgrade changes behaviour
without changing configuration.

## The short version

```bash
# 1. Back up (see docs/RUNBOOK_BACKUP_RESTORE.md — the CA key first)
pg_dump --format=custom --no-owner "$DATABASE_URL" > pre-upgrade.dump

# 2. Read the behaviour-change section below for the versions you cross

# 3. Roll one replica, watch it, then the rest
helm upgrade k8s-agent deploy/helm/k8s-agent -f values.yaml --atomic --timeout 10m

# 4. Verify
curl -s http://platform/health
curl -sH "Authorization: Bearer $TOKEN" http://platform/me
curl -sH "Authorization: Bearer $TOKEN" http://platform/agents | jq 'length'
```

`--atomic` rolls back the *Kubernetes* objects on failure. It does **not** roll
back the database, and cannot — see below.

---

## Schema migrations

Numbered, forward-only SQL in `backend/app/persistence/migrations/`, applied
automatically at startup. Not Alembic: there is no ORM, so autogenerate — its
whole value — does not apply.

```
001_investigations.sql    jobs, events, reports
002_agent_identity.sql    bootstrap tokens, agent certificates
003_tenancy.sql           tenant_id columns and row-level security
004_rbac.sql              tenant_members
```

**Applied under `pg_advisory_lock`, so N replicas booting together cannot
race.** Rolling upgrades need no migration job, no init container, and no
ordering between pods. The first pod to acquire the lock migrates; the others
wait and then find nothing to do.

### There are no downgrades, by design

A bad migration is fixed by the next one. That is a deliberate constraint and it
has a consequence you must plan for: **rolling back the application does not
roll back the schema.** If `v1.4` adds a column and you revert to `v1.3`, the
column stays. Migrations are therefore written to be additive — `ADD COLUMN IF
NOT EXISTS` with a default rather than a rewrite — so the previous version keeps
working against the newer schema.

If you need a true rollback, it is a restore from `pre-upgrade.dump`, and it
loses every investigation submitted since. Take the dump.

### Verifying before you commit to it

Migrations run at startup, so a syntax error in one is a CrashLoopBackOff across
every replica at once. Rehearse against a copy:

```bash
createdb k8sagent_upgrade_test
pg_restore --no-owner -d k8sagent_upgrade_test pre-upgrade.dump
DATABASE_URL=postgresql://…/k8sagent_upgrade_test \
  python -c "import psycopg; from app.persistence.migrator import migrate; \
             migrate(psycopg.connect('postgresql://…/k8sagent_upgrade_test'))"
```

---

## Rolling upgrades

The platform is designed for this: workers are stateless behind a queue, and a
claim is a conditional `UPDATE` (`WHERE status = 'pending'`), so two workers may
pop the same id and only one wins. Mixed versions during a roll are safe for the
job path.

Three things to know.

**In-flight investigations are drained, up to a deadline.** On SIGTERM the
worker fails readiness, stops claiming new work, waits up to
`SHUTDOWN_DRAIN_SECONDS` (30) for what is already running, then cancels the
rest. Verified live: a 30-second investigation ran to its own conclusion under
SIGTERM rather than being recorded as a lost worker.

What is *not* built is mid-run resumption — anything still running when the
deadline expires is reaped to `failed` by lease expiry rather than resumed,
because resuming would be a re-run and needs ADR-007's state machine.

Keep `SHUTDOWN_DRAIN_SECONDS` below `terminationGracePeriodSeconds`, and
remember the Helm chart's `preStop` sleep is consecutive with it: preStop plus
drain must fit inside the grace period, or the pod is SIGKILLed part-way
through a drain, which is worse than not draining.

**A rolling upgrade still drops the occasional request**, and the honest number
is measured rather than assumed: over five rolling upgrades under continuous
load, 4 of 5 dropped requests before the chart wired `/health/ready` and a
`preStop` hook, and 1 of 5 after. Readiness cannot close that window on its own
— the probe is polled, and the listener is gone before the next poll.

If neither is acceptable, drain first: stop submitting, wait for
`k8sagent_investigations_running` to reach zero across the fleet, then upgrade.

```promql
sum(k8sagent_investigations_running)
```

**Agent streams re-establish, and the CA does not change.** An agent's
certificate is renewed at 2/3 of its life into a `Holder` that Go's
`GetClientCertificate` consults per handshake, so the *next* dial picks up new
material while the open connection keeps the old certificate. Restarting the
platform drops streams; agents redial. No re-enrolment, provided you did not
change the CA. Watch them come back:

```bash
curl -sH "Authorization: Bearer $TOKEN" http://platform/agents | jq '[.[] | select(.online)] | length'
```

Expect full recovery within `AGENT_STALE_SECONDS` (45) plus the agents' own
backoff.

**Redis is not upgraded state.** Every message has a committed Postgres row
behind it, so flushing Redis during an upgrade costs latency and nothing else.
Do **not** restore a stale Redis afterwards — it reintroduces queue entries
whose rows have moved on. Empty is correct.

---

## Version-specific behaviour changes

These change what the platform *does* without any configuration changing. They
are the ones that surprise people.

### Upgrading into M6.5 (authorisation)

**Every authenticated caller previously could do everything.**
`RBAC_DEFAULT_ROLE` defaults to `admin` precisely to preserve that, so no
existing install has to be administered back into working order. Nothing breaks
on upgrade — and nothing is protected either, until you assign roles.

To actually get RBAC in a single-tenant install:

```bash
python -m app.rbacctl grant --subject you@example.com --role owner
# then, once at least one owner exists:
helm upgrade … --set rbac.defaultRole=viewer
```

Do it in that order. Setting `defaultRole: viewer` before granting an owner
leaves nobody able to grant one over HTTP, and you are back to the CLI anyway.

**`investigation.read_all` is owner-only, not admin, and that default is why.**
Every unbound caller being an admin plus `read_all` on admin would mean
upgrading silently removed the per-user report isolation those deployments
already had.

### Upgrading into M6 (tenancy)

`TENANCY_MODE` defaults to `single`, so nothing changes unless you opt in.
Migration `003` adds `tenant_id` with a default rather than backfilling, so
pre-M6 rows read `NULL` and group as `default` — correct, since they *were* the
single implicit tenant.

Switching to `shared` afterwards is not a flag flip. **Existing rows have no
tenant**, and with row-level security on they become invisible to every tenant.
Decide deliberately:

```sql
-- Assign all pre-existing data to one tenant before enabling shared mode.
UPDATE investigations       SET tenant_id = 'acme' WHERE tenant_id IS NULL;
UPDATE investigation_events SET tenant_id = 'acme' WHERE tenant_id IS NULL;
UPDATE investigation_reports SET tenant_id = 'acme' WHERE tenant_id IS NULL;
```

And check the role you connect as. `ENABLE`/`FORCE ROW LEVEL SECURITY` can both
be set and correct while every tenant reads every row, because superusers and
`BYPASSRLS` roles skip policies entirely — a deployment with no isolation and no
symptom. `Database.assert_row_level_security_applies()` refuses to start
`shared` on such a role, which is the check that turns the claim into a control.

### Upgrading into M8a (agent routing)

A cluster reachable only through an agent now gets a **409 `ClusterUnreachable`**
naming the worker holding the stream, where it previously fell back to the
platform's local kubeconfig. That is the fix, not a regression: the fallback
resolved a cluster *name* against whatever contexts the platform held and had no
tenant, so tenant A's `prod` could be answered by someone else's `prod`.

If 409s appear after this upgrade, they were previously wrong answers.

**`PRESENCE_TTL_SECONDS` (45) must stay below `UNCLAIMED_GRACE_SECONDS` (60).**
Raise the TTL above the grace period and recovery of a dead worker's queued jobs
becomes a permanent loop. Pinned by `tests/test_agent_routing.py`.

### Upgrading into M8b (payload sizes)

`JOB_MAX_CONCURRENT` became configurable; it was a constructor default of 4 that
`app/state.py` never passed. The default is still 4, so throughput does not
change on upgrade. Raising it is an operator decision against your own cluster
sizes — and it does **not** raise throughput on one worker: slots fill,
`collect` inflates in proportion, and the number does not move. Add replicas.

`GET /investigations/{id}/status` is additive; `GET /investigations/{id}` still
returns everything.

### Upgrading into M9.1 (observability integrations)

Only affects deployments with `PROMETHEUS_URL` set. Memory signals that never
fired now do, because the memory limit is read from kube-state-metrics rather
than from a cAdvisor series kube-prometheus-stack drops. Expect
`metrics.memory_near_limit` and `metrics.memory_peaked_at_limit` to appear for
the first time, and confidence on OOM hypotheses to rise.

`NodeMetricsCollector` renames `memory_available_bytes` → `used_memory_bytes`.
Nothing consumed the old field (it was always `None`), but a downstream
consumer reading the raw evidence payload should be checked.
See `docs/OBSERVABILITY_INTEGRATIONS.md`.

### Upgrading into M9.3 (retention covers the stored payload)

**Read this one before upgrading if you have `DATABASE_URL` set.**

`ReportStore.prune()` now nulls `investigations.result` alongside the rendered
report blobs. It previously deleted only the blobs, so the JSON payload they
were rendered *from* — the larger copy — survived retention indefinitely and
`GET /investigations/{id}` kept serving the full contents of investigations
whose PDF had already 404'd.

**The first sweep after upgrade deletes those payloads**, for everything older
than `REPORT_RETENTION_DAYS` (default 14). The sweep runs on start and every
`REPORT_RETENTION_SWEEP_HOURS` (6), so this happens within minutes of the
rollout, not at some later boundary.

Nothing is lost that retention did not already intend to delete, and the
`investigations` row and its history entry still survive — an investigation
that happened must not come to look like one that never did. But a deployment
reading old payloads back through `GET /investigations/{id}` was relying on
this gap, and will stop being able to.

If you need those payloads, take a `pg_dump` before rolling, or set
`REPORT_RETENTION_DAYS=0` to disable pruning entirely while you decide.

```sql
-- What the first sweep will null. Run before upgrading.
SELECT count(*), pg_size_pretty(sum(pg_column_size(result)))
  FROM investigations
 WHERE created_at < now() - interval '14 days' AND result IS NOT NULL;
```

---

### Upgrading into agent impersonation

**The behaviour change that matters most in this document.** Until now, a
cluster read performed through an agent ran as the **agent's own
ServiceAccount** — broad read across the whole cluster — for any caller who
could reach the platform. The calling user travelled on the wire in
`CollectionRequest.actor` and the agent discarded it, so F13's guarantee that
*the platform cannot see more than you can* was true through a kubeconfig and
false through an agent. `collection.proto` had documented the opposite since M2.

The agent now applies `Impersonate-User` and `Impersonate-Group` to every read.
Three consequences, in the order they will reach you:

1. **Nothing changes for an agent already running.** Impersonation is off unless
   `--impersonate` is passed, and an agent enrolled before this shipped has
   neither the flag nor the `impersonate` verb in its ClusterRole. It logs a
   warning naming exactly that on every start. This is deliberate: turning it on
   without the grant would have the API server refuse every read, and
   `app/kubernetes/access.py` would report it as *the caller's* RBAC being too
   narrow — blaming the user for the agent's missing permission.

2. **Re-apply the enrolment manifest to turn it on.** `POST /agents/enrolment`
   now emits the flag and the grant in the same document, so they cannot be out
   of step. After that, an investigation sees exactly what the person who ran it
   would see — which for some users is **less than before**. That is the fix
   working, not a regression, but it will look like one to anyone whose reports
   suddenly have gaps. The gaps are citable: the evidence record carries the API
   server's own sentence naming who was refused.

3. **`--impersonate` and `AUTH_MODE=disabled` are not a working pair.** With
   authentication off there is no caller to read as, and an impersonating agent
   refuses an unattributed read rather than quietly falling back to its own
   ServiceAccount — the same refusal `EVENT_SOURCES` makes by requiring a
   subject. Configure real authentication before enrolling an impersonating
   agent, or leave the flag off.

A related fix ships with it: every agent-reported failure used to read
`unknown`, because client-go reports that for any error on a raw request and the
agent reads raw on purpose. It now recovers the API server's message from the
response body, so a refusal names the user and a permissions problem stops
looking like a broken cluster.

## Configuration compatibility

Settings are validated at startup and a bad one **refuses to boot** rather than
failing per request. That is deliberate — a readiness probe passing while the
service serves nothing but 500s is the worst shape of misconfiguration — but it
means a typo introduced during an upgrade takes the pod down rather than
degrading it. Rehearse `helm template` before `helm upgrade`; the chart
reproduces the platform's refusals at render time so most of them surface before
anything is deployed.

Refused at startup, and worth re-checking on every upgrade:

- `AUTH_MODE=disabled` without `ALLOW_INSECURE_NO_AUTH`
- `AUTH_MODE=oidc` without `OIDC_ISSUER` / `OIDC_AUDIENCE`
- exactly one of `DATABASE_URL` / `REDIS_URL`
- `TENANCY_MODE=shared` without a database, without authentication, or on a
  row-level-security-bypassing role
- `RBAC_DEFAULT_ROLE` above `viewer` in `shared` mode
- a malformed `OIDC_ROLE_MAPPINGS` entry
- an `EVENT_SOURCES` entry with no subject

`NOTIFY_DESTINATIONS` is **pipe-delimited**, unlike every other list. A URL
contains colons, so the colon-separated shape `API_TOKENS` uses cannot express
one — the first draft split on `:` and silently truncated
`https://hooks.example.com:8443/path` to the host.

## The cluster agent

Agents and platform version independently; the wire contract is the compatibility
boundary. Generated bindings under `app/wire/gen/` and `agent/gen/` are committed
and CI runs `python scripts/generate_proto.py --check`, so a schema change is a
reviewable diff rather than a silent drift.

`EvidenceSpec` names a *kind* of evidence and has no field that can carry a
command, and `ReadVerb` is a closed enum — so a platform that gains a new read
cannot ask an older agent to do something it does not understand. The agent
refuses a kind it does not know (`agent/internal/policy`), which is a security
control rather than validation and is enforced in the customer's cluster where
they can verify it.

Upgrade the platform first, agents after. An older agent against a newer platform
declines unknown kinds and that evidence records as unavailable — a degraded
investigation, not a failed one.

## Post-upgrade verification

```bash
curl -s http://platform/health
curl -sH "Authorization: Bearer $TOKEN" http://platform/me          # role resolves
curl -sH "Authorization: Bearer $TOKEN" http://platform/clusters    # inventory
curl -sH "Authorization: Bearer $TOKEN" http://platform/agents | jq 'length'
curl -s http://platform/metrics | grep -c k8sagent_                 # series present
# an investigation end to end, and a stored report still renders
curl -so /dev/null -w '%{http_code}\n' http://platform/investigations/<old-id>/pdf
```

The last one matters more than it looks: it proves the report store still reads
artefacts written by the previous version.

Then watch, for one full `REPORT_RETENTION_SWEEP_HOURS` cycle:

```promql
sum(rate(k8sagent_investigations_total{outcome="failed"}[15m]))
sum(rate(k8sagent_evidence_records_total{status!~"ok|empty|not_applicable"}[15m]))
sum(k8sagent_queue_depth)
```

Evidence status is the leading indicator — a partial collection still succeeds,
so degradation appears there before it appears in the success rate.
