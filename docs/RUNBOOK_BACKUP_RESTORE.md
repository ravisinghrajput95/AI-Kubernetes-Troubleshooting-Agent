# Runbook — backup and restore

Backlog item 28. What has to survive a lost disk, in priority order, and what
happens if it does not.

## What is actually irreplaceable

Most of this platform's state is reproducible: reports can be regenerated,
investigations can be re-run, metrics are a time series someone else owns. Two
things are not.

| State | If lost | Replaceable? |
|---|---|---|
| **Agent CA private key** | **Every agent in the fleet must be re-enrolled by hand** | **No** |
| **Role bindings** | Everyone falls back to `RBAC_DEFAULT_ROLE`; on a default single-tenant install that is `admin` | No |
| Enrolment store (tokens, issued certs, revocations) | Revocations are forgotten — a revoked agent can reconnect | Partly |
| Investigation reports | Historical incident records gone | No, but not operationally fatal |
| Audit log | Compliance trail gone | No |
| Redis | Nothing. It is the latency layer; Postgres is the truth | Yes |

**The CA key is the one that ends a weekend.** It is a *file*, never a database
row, and it is the root of trust for every agent certificate in the fleet. Lose
it and no existing agent certificate can be renewed or verified against a
rebuilt CA; every cluster needs a fresh enrolment token and a redeploy.

## Where everything lives

Two deployment shapes, and the split matters for backup because half the state
moves into Postgres in the second.

### Single-process (`DATABASE_URL` unset)

All state is on local disk, **relative to the backend's working directory** —
which is why the backend must be started from `backend/`.

```
backend/
├── data/
│   ├── agent-identity/          # AGENT_IDENTITY_DIR
│   │   ├── ca.crt               # CA certificate
│   │   ├── ca.key               # CA PRIVATE KEY  <- the irreplaceable one
│   │   └── enrolment.json       # tokens (SHA-256 hashed), certs, revocations
│   ├── rbac/                    # RBAC_STORE_DIR — role bindings
│   └── investigations/          # rendered PDF / JSON / Markdown + history.json
└── audit.log                    # AUDIT_LOG_PATH, if set
```

### Distributed (`DATABASE_URL` + `REDIS_URL`)

The CA key stays a file. Everything else moves to Postgres.

| State | Location |
|---|---|
| CA certificate and **private key** | still files at `AGENT_CA_CERT_FILE` / `AGENT_CA_KEY_FILE` |
| Enrolment tokens and certificates | `agent_bootstrap_tokens`, `agent_certificates` |
| Role bindings | `tenant_members` |
| Investigations, events, reports | `investigations`, `investigation_events`, `investigation_reports` |
| Queue, presence, rate-limit counters | Redis — **do not back up**, it is rebuildable by design |

---

## Backup

### The CA (do this first, and once)

The CA key does not rotate and does not change. Back it up **once, out of band**,
and treat it like a signing key rather than like data:

```bash
# On the platform host
tar czf ca-backup.tgz -C backend/data/agent-identity ca.crt ca.key

# Encrypt it before it goes anywhere
age -r age1... ca-backup.tgz > ca-backup.tgz.age     # or gpg -c
rm ca-backup.tgz

sha256sum ca-backup.tgz.age    # record this somewhere separate
```

Store it where your organisation stores signing material — an HSM-backed secret
manager, or offline. **Not** in the same object store as the database backups:
the threat model that loses one usually loses both, and a CA key sitting beside
the certificates it signed defeats the purpose.

For a Kubernetes deployment, supply the CA as a mounted Secret and back the
Secret up through your normal secret-management path:

```bash
kubectl create secret generic k8s-agent-ca \
  --from-file=ca.crt=ca.crt --from-file=ca.key=ca.key
# then AGENT_CA_CERT_FILE=/etc/ca/ca.crt  AGENT_CA_KEY_FILE=/etc/ca/ca.key
```

If the platform generated a development CA on first start, it said so loudly in
the logs. Do not back that one up — replace it with a real one before enrolling
anything you care about.

### Postgres (distributed)

```bash
pg_dump --format=custom --no-owner "$DATABASE_URL" > k8sagent-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Nightly is a reasonable default given `REPORT_RETENTION_DAYS` is 14. Restore is
`pg_restore --clean --if-exists -d "$DATABASE_URL" <file>`.

**Row-level security does not restrict `pg_dump` when it runs as the table owner
or a superuser**, which is what you want for a backup and is exactly what you
must not use for the application. Keep the backup role separate from the
application role, and see `docs/TENANT_USAGE_REPORTING.md` for the same
distinction applied to reporting.

### File state (single-process)

```bash
tar czf k8sagent-state-$(date -u +%Y%m%dT%H%M%SZ).tgz \
    -C backend data/agent-identity data/rbac data/investigations
```

`enrolment.json` is written by atomic replace, so a copy taken at any moment is
internally consistent. Tokens in it are SHA-256 hashed, never in the clear —
but issued certificates and the revocation list are not secret-but-are-sensitive,
so encrypt this archive too.

### Redis

Do not back it up. Every message has a committed Postgres row behind it: a
queued id is a `pending` row, a cancel is a committed `cancel_requested`, an
event is an inserted row carrying the sequence the message quotes. Losing Redis
makes the system slower, never wrong. Restoring a *stale* Redis is worse than
starting empty, because it reintroduces queue entries whose rows have moved on.

---

## Restore

### Whole-platform restore, distributed

1. Restore the CA files first, at the paths `AGENT_CA_CERT_FILE` /
   `AGENT_CA_KEY_FILE` name, mode `0600`.
2. `pg_restore` into an empty database. Migrations are forward-only and applied
   under `pg_advisory_lock`, so bringing N replicas up together is safe.
3. Start **one** replica and confirm `/health`, then scale out.
4. Leave Redis empty. It repopulates.
5. Verify an agent reconnects without re-enrolment — that is the check that the
   CA is genuinely the same one.

```bash
kubectl -n k8s-agent logs deploy/agent | tail -20      # in a customer cluster
curl -sH "Authorization: Bearer $TOKEN" http://platform/agents | jq '.[].cluster'
```

### If the CA key is genuinely lost

There is no recovery. The honest procedure is a fleet re-enrolment:

1. Generate a new CA, or supply one from your PKI.
2. Restart the platform with `AGENT_CA_CERT_FILE`/`AGENT_CA_KEY_FILE` pointing
   at it.
3. For every cluster: `python -m app.agentctl issue-token --cluster <id>`, then
   redeploy that cluster's agent with the new token **and** the new CA bundle
   (`python -m app.agentctl ca --out ca.crt`).
4. Old certificates are now unverifiable and their streams will not re-establish.

Budget one change window per cluster and treat 1,000 clusters accordingly. This
is the reason the CA backup is item one.

### If only the enrolment store is lost

Less severe, and the failure is *silent*, which is what makes it worth naming:
connected agents keep working, because the certificate is the identity and it is
verified against the CA rather than looked up. What is lost is the **revocation
list** — an agent you revoked can reconnect and will be trusted.

After restoring from an older copy, re-apply every revocation you know about:

```bash
python -m app.agentctl revoke --cluster <id> --reason "re-applied after restore"
python -m app.agentctl list --cluster <id>
```

Revocation is swept, not only checked at connect, so a re-applied revocation
ends a live session rather than waiting for a reconnect that may be weeks away.

### If role bindings are lost

Everyone falls back to `RBAC_DEFAULT_ROLE`. On a default single-tenant install
that is `admin`, so the symptom is **everybody having too much access**, not
being locked out — which is the failure that gets noticed late. Restore
`data/rbac/` or the `tenant_members` table, then:

```bash
python -m app.rbacctl --tenant <t> list
```

If you cannot restore, bootstrap one owner with `rbacctl grant` and re-grant
from your IdP groups — which is the argument for driving roles from
`OIDC_ROLE_MAPPINGS` rather than stored bindings wherever possible, since group
mappings live in configuration you already back up.

---

## Verifying a backup is real

An unverified backup is a belief. Quarterly, into a scratch environment:

```bash
# 1. restore Postgres + CA into an empty stack
# 2. enrol nothing; start an existing agent against it
# 3. confirm the stream establishes and the cluster appears
curl -s http://restored-platform/agents | jq '.[] | {cluster, online, worker}'
# 4. confirm roles resolve
curl -sH "Authorization: Bearer $TOKEN" http://restored-platform/me
# 5. confirm a stored report still renders
curl -so /dev/null -w '%{http_code}\n' http://restored-platform/investigations/<id>/pdf
```

Step 3 is the one that actually tests the CA, and it is the step most likely to
be skipped.
