# Per-tenant usage reporting for chargeback

Backlog item 34. What each tenant consumed, for billing or internal showback.

```bash
python scripts/tenant_usage.py --dsn "$REPORTING_DATABASE_URL" --days 30
```

```
Tenant usage  2026-07-04 .. 2026-08-03  (exclusive)

tenant       runs   ok    fail  canc  users  clusters  avg s   reports  bytes
------------------------------------------------------------------------------
acme         16     12    3     1     3      2         0.416   16       527.3KB
globex       7      5     2     0     3      2         0.279   7        270.0KB
default      4      4     0     0     3      2         0.275   4        165.0KB
------------------------------------------------------------------------------
TOTAL        27     21    5
```

`--format csv` and `--format json` for anything downstream. `--since` / `--until`
take ISO dates for a fixed billing period; `--until` is exclusive, so consecutive
months do not double-count a boundary day.

---

## Why it is a script and not an endpoint

Chargeback is inherently a **cross-tenant** question, and cross-tenant reads are
the thing this platform's data layer is built to prevent.

Tenant isolation is row-level security in Postgres: `Database.cursor()` emits
`set_config('app.current_tenant', …, true)` on every transaction, tenanted tables
default `tenant_id` to that setting, and a policy compares against it. No store
method mentions a tenant, which is what makes isolation a property of the schema
rather than of everyone remembering. There is exactly one escape —
`system_scope()` — and `tests/test_tenancy.py::test_only_the_queue_consumer_uses_it`
pins it to a single caller:

> A deliberate hole stays deliberate only if it stays small.

A reporting path inside `app/` would either widen that hole or need a second
one, and it would put a cross-tenant read behind the same HTTP surface that
`app/authz` spends its whole design keeping tenant-scoped. So this reads the
database directly, as an operator tool in the same category as `pg_dump`, and
deliberately does not import `app`.

## The role it needs, and the role it must not use

The report needs a role that can see every tenant's rows, which means one that
**bypasses row-level security**:

```sql
CREATE ROLE k8sagent_report LOGIN PASSWORD '…' BYPASSRLS;
GRANT CONNECT ON DATABASE k8sagent TO k8sagent_report;
GRANT USAGE ON SCHEMA public TO k8sagent_report;
GRANT SELECT ON investigations, investigation_reports TO k8sagent_report;
```

`SELECT` only, on two tables. It has no reason to read `tenant_members`,
`agent_certificates` or `agent_bootstrap_tokens`, and no reason to write
anything.

**Never point this at the application's role, and never give the application a
`BYPASSRLS` role.** That is not advice, it is the failure that made
`Database.assert_row_level_security_applies()` necessary: `ENABLE` and `FORCE
ROW LEVEL SECURITY` were both set and correct, every policy was right, and every
tenant could still read every row — because the application connected as
`postgres`. A deployment in that state has no isolation and no symptom. The
platform now refuses to start `shared` mode on such a role.

### Verified, not assumed

Against a seeded three-tenant database:

| Connecting as | Tenant set | Rows visible |
|---|---|---|
| `k8sagent_app` (NOBYPASSRLS) | none | **0** — the report is empty |
| `k8sagent_app` (NOBYPASSRLS) | `acme` | 16, acme's only |
| `k8sagent_report` (BYPASSRLS) | none | all 27, all three tenants |

The first row is the important one: run this with the application's role and you
get a **clean, empty, plausible-looking report** rather than an error. Check the
totals against something you know before trusting a billing run.

---

## What is counted

| Column | Meaning |
|---|---|
| `runs` | Investigations created in the window |
| `ok` / `fail` / `canc` | Terminal status |
| `users` | Distinct non-empty `owner` values |
| `clusters` | Distinct `request->>'cluster'` values |
| `avg s` | Mean `finished_at - started_at`, completed runs only |
| `total_seconds` (json/csv) | Summed execution time — the compute basis |
| `reports` / `bytes` | Rendered artefacts currently stored |

**An investigation is the unit worth billing**, because it is the platform's only
outbound action: it reads a customer's production cluster under an impersonated
identity and spends a model call. That is the same reasoning that made
`COSTED_PERMISSIONS = {investigation.run}` the rate limiter's key. Reads of
already-collected data cost neither and are not counted.

Two details that will otherwise surprise you at reconciliation time:

- **`result` is never selected.** A stored result averages 2.7 MB at the
  `MAX_LIST_ITEMS` ceiling, so a report that pulled it would move gigabytes to
  count rows. Same rule as `_JOB_SUMMARY_COLUMNS`, for the same reason.
- **`reports` and `runs` legitimately disagree.** `REPORT_RETENTION_DAYS`
  (default 14) prunes rendered artefacts while the history entry survives marked
  `expired`, so a 30-day window shows storage for only part of it. Bill compute
  from `runs`/`total_seconds` and storage from a sampled point-in-time figure,
  not from this column over a long window.

## What is not counted, and cannot be

- **Model spend.** `k8sagent_llm_calls_total` is fleet-wide and carries no
  tenant label — deliberately, since per-tenant series would publish the
  customer list to any scraper and fall over at 1,000 clusters. Token counts are
  not recorded per investigation at all. If model cost must be attributed, take
  it from your provider's billing export and apportion by `runs`, or add token
  accounting to `LLMClient` first.
- **Evidence volume and collection cost.** Not attributed per tenant.
- **Agent count per tenant.** Available from `agent_certificates` (which carries
  `tenant_id`) but not joined here; a fleet-size charge would query that table.
- **Anything before tenancy shipped.** Migration `003` added `tenant_id` with a
  default rather than backfilling, so pre-M6 rows read `NULL` and are grouped as
  `default`. That is correct — they *were* the single implicit tenant — but do
  not read it as a customer named "default" if you also have one.

## Single-tenant deployments

In `TENANCY_MODE=single` (the default) there is one implicit tenant and every
row reads `default`. The script still works and gives a fleet-wide usage
summary, which is useful for capacity planning even with nothing to charge back:
`runs`, `avg s` and `total_seconds` against `docs/PERFORMANCE_ENVELOPE.md` tell
you how much of one worker's ~12/s ceiling you are using.

## Scheduling it

Monthly, into object storage, from a cron job or a Kubernetes `CronJob`:

```bash
python scripts/tenant_usage.py \
  --dsn "$REPORTING_DATABASE_URL" \
  --since "$(date -u -d 'last month' +%Y-%m-01)" \
  --until "$(date -u +%Y-%m-01)" \
  --format json > "usage-$(date -u -d 'last month' +%Y-%m).json"
```

Keep the reporting DSN in the same secret manager as the CA key, and out of the
platform's own environment — the application has no reason to hold a credential
that can read every tenant.
