# AI Kubernetes Troubleshooting Agent

Investigates Kubernetes incidents the way an experienced SRE does — gather
evidence, form hypotheses, gather more evidence to test them, then explain the
conclusion with citations back to the commands that produced it.

Every claim it makes is traceable to a specific piece of evidence. It cannot
modify your cluster, and it will tell you what it could not see.

> [!WARNING]
> **Not production ready.** This runs against real clusters and is useful today,
> but it is single-process with no HA, and several operational gaps remain. Read
> [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) before deploying
> anything, and [SECURITY.md](SECURITY.md) before exposing it.

---

## What makes it different

**It never mutates your cluster.** Every command goes through a read-only verb
allowlist enforced at the executor. Remediation is generated as text for a human
to review — and the same policy *rejects those commands*, so there is no path by
which the platform can run its own recommendations. Asserted by test, for every
remediation rule.

**The model selects and explains; it does not diagnose from raw JSON.** Evidence
is turned into deterministic *signals* by rules, signals into ranked
*hypotheses*, and only then is a model asked to choose between them. Its answer
is rejected unless every citation resolves to a real signal **and** the prose
does not contradict what it cites. A response citing a genuine
CrashLoopBackOff while concluding "resolved, no action needed" is discarded and
the deterministic ranking stands.

**Commands shown to you are never model-authored.** Cluster text — log lines,
event messages, resource names — is attacker-controlled if anyone can write to
the cluster, and it reaches the prompt. So remediation commands are generated
deterministically, and every command displayed is classified: unrecognised
strings are dropped, state-changing ones labelled.

**Missing evidence is data, not silence.** A collector that fails, times out, or
has no backend deployed records *why*. "Prometheus was unavailable" is something
a diagnosis can cite. It is never presented as "metrics look fine".

**Investigation is iterative.** The first pass finds what is broken. Each
hypothesis declares what evidence would confirm or refute it, and a playbook
collects exactly that — previous-container logs, exit codes, referenced ConfigMap
keys. In the reference scenario this moves the conclusion from a generic
"application fails on startup" (confidence 76) to "pod references configuration
that does not exist" (confidence 94), while *refuting* the original reading.

## Quickstart

Requires `kubectl` on PATH and a reachable cluster. Python 3.12+, Node 22+.

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Auth is required by default. For local development only:
export AUTH_MODE=disabled ALLOW_INSECURE_NO_AUTH=true   # local only; see SECURITY.md
uvicorn app.main:app --reload --port 8000
```

```bash
# Frontend, in a second terminal
cd frontend
npm ci
npm run dev          # http://localhost:3000
```

`OPENAI_API_KEY` is **optional**. Without it the deterministic pipeline runs and
produces a complete diagnosis — signals, ranked hypotheses, remediation plan and
reports — marked `ai_generated: false`. The model adds explanation, not capability.

> The backend must be started from `backend/`: reports are written to a relative
> `data/` directory.

## How it works

```
kubectl ─► Evidence ─► Signals ─► Hypotheses ─┬─► Playbooks ─► targeted evidence ─┐
          (addressable,  (rules)   (rules)    │   (collect what would confirm)    │
           status-typed)                      │                                   │
                                              └───────────◄───────────────────────┘
                                                          │
                                    Model selects & explains
                                                          │
                                          Grounding validation
                                                          │
                                   Diagnosis · Remediation · Reports
```

| Layer | Does what | Docs |
|---|---|---|
| Evidence | Addressable facts with deterministic ids and status | [EVIDENCE_ARCHITECTURE](docs/EVIDENCE_ARCHITECTURE.md) |
| Reasoning | Signals → hypotheses → grounded diagnosis | [REASONING_ARCHITECTURE](docs/REASONING_ARCHITECTURE.md) |
| Playbooks | Second-pass targeted collection per failure class | [PLAYBOOKS](docs/PLAYBOOKS.md) |
| Remediation | Risk-rated plans and patch artifacts, never applied | [REMEDIATION](docs/REMEDIATION.md) |
| Observability | Optional Prometheus and Loki evidence | [OBSERVABILITY](docs/OBSERVABILITY.md) |
| API | Sync and job-based investigation, SSE progress | [INVESTIGATION_API](docs/INVESTIGATION_API.md) |
| Console | Panels for signals, hypotheses, evidence, remediation | [SRE_CONSOLE](docs/SRE_CONSOLE.md) |
| Reports | One composition rendered as PDF, Markdown, JSON | [INCIDENT_REPORTS](docs/INCIDENT_REPORTS.md) |
| Evaluation | Golden corpus gating reasoning quality in CI | [EVALUATION](docs/EVALUATION.md) |
| **Enterprise** | **Proposed fleet architecture: agents, tenancy, scale** | [ENTERPRISE_ARCHITECTURE](docs/ENTERPRISE_ARCHITECTURE.md) |

### Failure classes it investigates deeply

CrashLoopBackOff · OOMKilled · ImagePullBackOff · Pending and unschedulable ·
ResourceQuota exhaustion · unbound PersistentVolumeClaims · services with no
endpoints · NetworkPolicy denial · cluster DNS · stalled rollouts · failing
probes · node pressure.

## Configuration

Read from the environment or `backend/.env`.

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_MODE` | `disabled` | `oidc`, `token`, or `disabled` |
| `ALLOW_INSECURE_NO_AUTH` | `false` | Required to run with auth off. **Checked at startup**, so a misconfiguration refuses to boot rather than 500ing every request behind a green health check |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` | — | Required when `AUTH_MODE=oidc` |
| `API_TOKENS` | — | `token:subject[:group\|group][:tenant]`, comma separated |
| `OIDC_ROLE_MAPPINGS` | — | `group=role` pairs, e.g. `sre=operator,platform=admin` |
| `RBAC_DEFAULT_ROLE` | `admin` | Role for a caller with no binding and no matching group |
| `RBAC_STORE_DIR` | `data/rbac` | Where role bindings live without `DATABASE_URL` |
| `IMPERSONATE_USERS` | `true` | Run cluster reads as the calling user |
| `OPENAI_API_KEY` | — | Optional; without it the deterministic path runs |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `KUBECONFIG_PATH` | — | Defaults to the standard kubeconfig |
| `PROMETHEUS_URL` / `LOKI_URL` | — | Optional; absent is recorded, not fatal |
| `MAX_LIST_ITEMS` | `2000` | Cap on objects retained per list read |
| `JOB_MAX_CONCURRENT` | `4` | Investigations one worker runs at once (~13 MB each at the cap) |
| `AUDIT_LOG_PATH` | — | Append-only audit trail; falls back to stdout |
| `METRICS_ENABLED` | `true` | Serve `/metrics`; no cluster, tenant or user is ever a label |
| `RATE_LIMIT_PER_MINUTE` | `60` | Investigations one caller may start per minute |
| `RATE_LIMIT_TENANT_PER_MINUTE` | `0` | Per-tenant quota; 0 is unlimited, set it on `shared` |
| `EVENT_SOURCES` | — | `name:secret:subject[:groups][:tenant]`; the subject is impersonated |
| `EVENT_COOLDOWN_SECONDS` | `1800` | How long a repeated alert fingerprint is ignored |
| `NOTIFY_DESTINATIONS` | — | `name\|url\|secret[\|tenant][\|severity][\|outcomes]` — pipe-delimited; URLs contain colons |
| `CONSOLE_URL` | — | Used to link a notification to its investigation |

**Impersonation matters.** With it on, every cluster read runs as the calling
user, so the cluster applies *their* RBAC rather than the service account's. The
service account needs `impersonate` on users and groups.

**Roles.** Four per tenant — `viewer` reads, `operator` may run investigations
against a cluster, `admin` may enrol clusters and manage members, `owner` may
grant `owner`. `RBAC_DEFAULT_ROLE=admin` is deliberate: it is exactly how the
platform behaved before roles existed, so a single-tenant install upgrades
unchanged. Set it to `viewer` and assign roles to get real RBAC; it is refused
above `viewer` when `TENANCY_MODE=shared`.

```bash
python -m app.rbacctl grant --subject alice@example.com --role owner
python -m app.rbacctl list
python -m app.rbacctl suspend --subject bob@example.com   # deny now, whatever their groups say
```

A CLI rather than an endpoint, for the same reason `agentctl` is one: a
role-granting endpoint reachable before any role exists is the hole it would be
closing. There is no invite flow — grant a role by subject and it applies the
first time that person signs in.

## Development

```bash
cd backend
pip install -r requirements-dev.txt ruff
ruff check . && ruff format --check .
python -m pytest -q        # 438 tests
python -m evals            # reasoning + grounding regression report

cd frontend
npm test                   # 47 tests
npm run build              # tsc -b — the type gate
```

CI runs all of the above on every pull request, plus a dependency audit that
**fails the build** on a known vulnerability, a secret scan, and both Docker
builds.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the five design rules this codebase
holds to. They are load-bearing rather than stylistic.

## Known limitations

Stated plainly, because the alternative is you finding out during an incident.

- **Single process.** Jobs live in memory; they do not survive a restart and are
  not shared between workers. Run one replica.
- **No multi-tenancy** beyond per-user ownership of investigations.
- **Peak memory scales with cluster size.** `kubectl` assembles a whole list
  before writing it, so a very large cluster produces a large parse. Retention is
  capped and truncation is reported; the ceiling needs a streaming client.
- **No platform self-observability.** No metrics or traces of its own yet.
- **OpenAI only.** No provider abstraction, so no Anthropic, Bedrock, or local
  models.
- **`fix` and `prevention` are model-authored prose** and therefore influenceable
  by injected cluster text. Commands never are.

The full backlog, with severity and effort, is in
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).

## License

[Apache 2.0](LICENSE).
