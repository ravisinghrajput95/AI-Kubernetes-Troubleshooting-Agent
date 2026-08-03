# Data protection — what is stored, encryption at rest, retention and residency

Backlog items 29 and 33. They are one document because the answer to "how do I
encrypt the reports" depends on knowing what is in them, and the answer to
"where does customer data live" is the same list.

---

## 1. What the platform actually stores

An investigation reads a customer's cluster and keeps what it read. At the
`MAX_LIST_ITEMS` ceiling of 2,000 pods one stored result is **2.7 MB**, and the
majority of it is *derived* rather than collected:

| Section | Share |
|---|---|
| `diagnosis.signals` | 34% |
| `investigation.pods` | 27% |
| `investigation.graph` | 18% |
| everything else | 21% |

Concretely, a stored investigation contains pod names, namespaces, node names,
container images and tags, resource limits, event messages, **pod log lines**,
ConfigMap *key names*, environment variable *names*, owner references, and the
cluster name. That is enough to reconstruct a meaningful picture of a customer's
production estate, which is what makes the rest of this document necessary.

### What is deliberately never stored

- **Secret values.** Secrets are read via `describe`, which prints key names and
  sizes but never values. `test_secret_values_are_never_requested` asserts no
  command issues `get secret`.
- **ConfigMap values.** Read as JSON, but only key names are emitted.
- **Kubernetes credentials.** The platform holds a kubeconfig; it is never
  copied into evidence.

### What is redacted, and where

Redaction happens at the **collection boundary** (`CollectionScheduler`), not at
the prompt boundary — so reports on disk, the HTTP API, and the model all see
the same scrubbed payload. Do not reintroduce it later; that leaves the
persistence and API paths uncovered.

`app/ai/evidence_redactor.py` scrubs by keyword (`password`, `token`, …), by
keyword shape (`password=…`, `Authorization: Bearer …`), and by credential
shape independent of any surrounding key — JWTs, AWS access key ids and secret
keys, Google API keys, private key blocks, and credentials embedded in URLs
(`scheme://user:pass@host`).

**This is best-effort on free text and must not be described to a customer as a
guarantee.** It reliably catches credentials in the shapes above. It cannot
catch a secret that a log line prints with no marker and no recognisable shape —
an account number, a customer email, a bare high-entropy string. Pod logs are
the highest-risk surface here, because their content is entirely the customer's
application's choice.

If an application logs regulated data, the correct control is not a better regex
— it is to keep the platform out of those namespaces, or to accept that stored
investigations inherit the classification of the logs they contain.

---

## 2. Encryption at rest

The platform implements **no application-layer encryption of report blobs**, and
that is a deliberate position rather than an omission. Below is what to use
instead, and the honest argument for why.

### Where the bytes are

| Backend | Location | How to encrypt |
|---|---|---|
| `PostgresReportStore` (with `DATABASE_URL`) | `investigation_reports.content` — a `bytea` blob per format | Storage-level encryption on the database |
| `FilesystemReportStore` (default) | `data/investigations/` | Encrypted volume |
| Audit log | `AUDIT_LOG_PATH` | Encrypted volume; ship to a SIEM |
| Agent CA key | `AGENT_CA_KEY_FILE` | Secret manager — see `docs/RUNBOOK_BACKUP_RESTORE.md` |
| Redis | queue, presence, rate counters | Nothing durable; see below |

Redis holds no fact that is not already a committed Postgres row, so it is not a
separate at-rest concern. It *does* transit investigation ids and event payloads,
so use `rediss://` on any network you do not control.

### Recommended configuration

**Managed Postgres.** Enable encryption at rest with a customer-managed key:

- RDS / Aurora: `StorageEncrypted=true` with a CMK in KMS
- Cloud SQL: CMEK
- Azure Database for PostgreSQL: customer-managed key in Key Vault

This covers `investigation_reports`, `investigations`, `agent_certificates` and
`tenant_members` in one decision, with key rotation and revocation handled by
infrastructure that already has an audit trail.

**Self-managed Postgres.** LUKS or equivalent under the data directory, plus
`ssl=on` for connections. Do not use `pgcrypto` on the report blob: it moves the
key into the application's configuration, breaks `pg_dump` portability, and buys
nothing against the realistic threat, which is a stolen disk or snapshot rather
than a compromised database session.

**Filesystem store.** Only for single-process deployments. Put
`data/investigations/` on an encrypted volume; on Kubernetes, a PVC from an
encrypted StorageClass.

**In transit.** TLS terminating at the platform, `sslmode=require` (prefer
`verify-full`) to Postgres, `rediss://` to Redis, and mTLS to agents — which is
the default and is refused-not-downgraded when an agent offers `--insecure`.

### Why not application-layer encryption

It would encrypt the blob and leave the interesting parts in the clear. The
report blob is only one of four places customer data sits: `investigations.result`
is a `jsonb` column carrying the same 2.7 MB, and it must stay queryable;
`investigation_events` carries progress messages naming resources; the audit log
names actors and targets. Encrypting the blob alone produces a compliance
artefact rather than a security improvement, and it would break
`POST /investigations/{id}/regenerate`, which re-renders from stored JSON.

It also introduces a key the platform must hold to serve
`/investigations/{id}/pdf`, which means the key lives next to the ciphertext in
the same process — the arrangement that makes storage-level encryption the
better answer for the threat that actually applies.

**If a buyer requires application-layer encryption**, the seam is
`ReportStore` (`app/services/report_store.py`): it takes and returns bytes and
is already swapped between filesystem and Postgres. An encrypting decorator
around it is a contained change held by `tests/test_job_store_contract.py`-style
contract tests. `investigations.result` is the harder half and is not behind
that seam.

---

## 3. Retention

### What is built

| Setting | Default | Effect |
|---|---|---|
| `REPORT_RETENTION_DAYS` | 14 | Rendered PDF/JSON/Markdown older than this are deleted |
| `REPORT_RETENTION_SWEEP_HOURS` | 6 | How often the sweep runs |
| `EVENT_COOLDOWN_SECONDS` | 1800 | Alert-trigger dedup window |

`ReportStore.prune()` deletes the rendered artefacts. **The history entry
survives and is marked `expired`** — deleting it too would make an investigation
that happened look like one that never did, which is the wrong answer for both
incident review and audit. `0` disables pruning entirely.

### What is not built, and matters

- **`investigations.result` is not pruned.** Retention removes the *rendered*
  artefacts, not the stored JSON payload they were rendered from — which is the
  larger copy and contains the same data. A deployment that must delete
  investigation content on a schedule needs a scheduled job of its own:

  ```sql
  -- Run as the application role so row-level security still applies.
  UPDATE investigations
     SET result = NULL
   WHERE created_at < now() - interval '90 days'
     AND status IN ('succeeded', 'failed', 'cancelled');
  ```

  Nulling `result` rather than deleting the row keeps the history entry, which
  is the same decision `prune()` already makes. Verify against your own
  retention policy before running it; this is an example, not a default.

- **The audit log is never pruned by the platform.** It is append-only JSON
  lines at `AUDIT_LOG_PATH`, and rotating or shipping it is deliberately the
  operator's decision — a compliance trail the application can delete is a
  weaker trail. Use `logrotate` or ship to a SIEM.

- **There is no per-tenant retention override.** `REPORT_RETENTION_DAYS` is
  global. A customer requiring 30 days alongside one requiring 7 needs separate
  deployments today.

- **There is no right-to-erasure endpoint.** Deleting one subject's data means
  finding it in `investigations.result`, which is unindexed JSON. In practice
  the platform stores infrastructure metadata rather than personal data, but if
  your applications log personal data into pod logs, that data reaches these
  tables and this gap is yours to close.

---

## 4. Residency

### Where data comes to rest

The platform is a single logical deployment: one Postgres, one Redis, one set of
workers. **All investigation data lands wherever that deployment runs**,
regardless of where the cluster it investigated is.

This is the important consequence and it is easy to get wrong when selling into
the EU: an agent running in an EU cluster streams evidence to the platform, and
if the platform runs in `us-east-1` then EU cluster data is now in the US. The
agent's outbound-only connection changes the network direction, not the data
residency.

**To keep data in a region, run a platform deployment in that region.** The
architecture supports this cleanly — agents dial out to a configured address, so
an EU fleet points at an EU platform — but the platform does not shard by region
and there is no cross-region federation. `GET /clusters` on the EU deployment
shows the EU fleet only.

### What leaves the deployment

Three egress paths, and each is off unless configured:

| Path | Configured by | What leaves |
|---|---|---|
| LLM call | `OPENAI_API_KEY` | Signals, hypotheses, scope, health, coverage — **never raw evidence** |
| Notifications | `NOTIFY_DESTINATIONS` | An allowlisted summary plus a link |
| Metrics scrape | `METRICS_ENABLED` | No cluster, tenant, namespace, user or investigation id — asserted end to end |

The LLM path is the one to review with a data-protection officer. `PromptBuilder`
sends the *derived* layer — signal summaries, hypothesis ids, resource
references — rather than collected JSON, which narrows both the prompt and what
the model can assert. It is still customer-identifying: signal summaries name
namespaces, pods and containers, and quote log lines verbatim where
`logs.error_pattern` fired.

Without `OPENAI_API_KEY` the deterministic fallback produces a complete
diagnosis and **nothing leaves the deployment at all**. That is a supported
configuration, not a degraded one — `ai_generated: false` is the only
difference visible to a caller. For a residency-constrained customer, it is the
right default, and self-hosting a model behind an OpenAI-compatible endpoint is
the middle path (`OPENAI_MODEL` plus a base URL change).

`build_summary` for notifications is an explicit **allowlist** assembled field
by field, not a filter — a denylist would leak whatever a future collector adds.
A destination belongs to a tenant, so acme's incident cannot be announced into
globex's Slack.

### Sub-processors

There are none by default. The platform ships with no telemetry, no
phone-home, and no third-party SDK in the data path. Configure
`OPENAI_API_KEY` and OpenAI becomes a sub-processor for the derived layer;
configure `NOTIFY_DESTINATIONS` and the destination does. Both belong in a
customer-facing DPA if enabled.

---

## Checklist for a regulated deployment

- [ ] Postgres encrypted at rest with a customer-managed key
- [ ] `sslmode=verify-full` to Postgres, `rediss://` to Redis
- [ ] CA private key in a secret manager, backed up out of band
- [ ] Audit log shipped to a SIEM with its own retention
- [ ] `REPORT_RETENTION_DAYS` set to your policy, and a job nulling
      `investigations.result` on the same schedule
- [ ] Platform deployed in the region whose data it will hold
- [ ] Decision recorded on `OPENAI_API_KEY` — off keeps everything in-region
- [ ] Namespaces whose logs carry regulated data excluded from investigation scope
