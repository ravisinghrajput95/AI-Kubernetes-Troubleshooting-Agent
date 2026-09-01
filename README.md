# AI Kubernetes Troubleshooting Agent

Investigates Kubernetes incidents the way an experienced SRE does — gather
evidence, form hypotheses, gather more evidence to test them, then explain the
conclusion with citations back to the commands that produced it.

Every claim it makes is traceable to a specific piece of evidence. It cannot
modify your cluster, and it will tell you what it could not see.

> [!WARNING]
> **No production deployment exists.** Every number in this repository was
> measured on kind clusters and synthetic fleets — there is no install anywhere
> that has run for a week, and no user but its author. The gaps that remain are
> tracked honestly in
> [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md), and
> [SECURITY.md](SECURITY.md) lists what is still weak before you expose it.

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
| Fleet | Cluster agents over mTLS, per-worker routing, presence | [ENTERPRISE_ARCHITECTURE](docs/ENTERPRISE_ARCHITECTURE.md) |
| Tenancy & roles | Row-level security, four roles, audit log | [SSO_GROUP_MAPPING](docs/SSO_GROUP_MAPPING.md) |
| Integrations | Alert-triggered investigations, signed egress, MCP tools | [MCP](docs/MCP.md) |
| Operations | Probes, retention, backup, upgrade, SLOs | [UPGRADE](docs/UPGRADE.md) |

### Failure classes it investigates deeply

CrashLoopBackOff · OOMKilled · ImagePullBackOff · Pending and unschedulable ·
ResourceQuota exhaustion · unbound PersistentVolumeClaims · services with no
endpoints · NetworkPolicy denial · cluster DNS · stalled rollouts · failing
probes · node pressure.

## Configuration

Read from the environment or `backend/.env`.

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_MODE` | — | **Required.** `oidc`, `token`, or `disabled`; unset is refused at startup naming all three, because a mode nobody chose is how a deployment ends up open without anyone having said so |
| `ALLOW_INSECURE_NO_AUTH` | `false` | Required to run with auth off. **Checked at startup**, so a misconfiguration refuses to boot rather than 500ing every request behind a green health check |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` | — | Required when `AUTH_MODE=oidc` |
| `API_TOKENS` | — | `token:subject[:group\|group][:tenant]`, comma separated |
| `OIDC_ROLE_MAPPINGS` | — | `group=role` pairs, e.g. `sre=operator,platform=admin` |
| `RBAC_DEFAULT_ROLE` | `admin` | Role for a caller with no binding and no matching group |
| `RBAC_STORE_DIR` | `data/rbac` | Where role bindings live without `DATABASE_URL` |
| `IMPERSONATE_USERS` | `true` | Run cluster reads as the calling user |
| `LLM_PROVIDER` | inferred | `openai`, `anthropic` or `compatible`. Unset infers from whichever key is set, OpenAI first |
| `OPENAI_API_KEY` | — | Optional; without it the deterministic path runs |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `ANTHROPIC_API_KEY` | — | Selects the Anthropic provider when `LLM_PROVIDER` is unset |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `LLM_BASE_URL` | — | Point the OpenAI wire format somewhere else — vLLM, Ollama, a gateway. Required for `compatible` |
| `LLM_MAX_TOKENS` | `16000` | Output ceiling; Anthropic requires one and truncates at it |
| `KUBECONFIG_PATH` | — | Defaults to the standard kubeconfig |
| `PROMETHEUS_URL` / `LOKI_URL` | — | Optional; absent is recorded, not fatal |
| `MAX_LIST_ITEMS` | `2000` | Cap on objects retained per list read |
| `COLLECTION_CACHE_TTL_SECONDS` | `60` | Reuse a cluster read for this long; `0` re-reads every time |
| `COLLECTION_CACHE_MAX_BYTES` | `67108864` | Cache bound, LRU by bytes; process memory only |
| `AGENT_CERT_TTL_HOURS` | `2160` | Agent certificate life; agents renew at 2/3 of it. Fractional, so a short-lived certificate is testable |
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
user, so the cluster applies *their* RBAC rather than the service account's —
which is what makes "the platform cannot see more than you can" true rather than
aspirational. It works on both paths: `kubectl --as` locally, and
`Impersonate-User` headers through a cluster agent. Either identity needs the
`impersonate` verb on users and groups, and an agent enrolled before that
existed says so in its logs until you re-apply its manifest.

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
python -m pytest -q        # 1,436 tests
python -m evals            # 20 golden investigations, 11 grounding cases

cd frontend
npm test                   # 230 tests
npm run build              # tsc -b — the type gate
```

Two more that are worth knowing about, because they catch what the suites cannot:

```bash
python scripts/mutation_check.py      # ~4s: 16 shipped defects vs the tests that catch them
./scripts/integration_verify.sh       # ~8min: kind + Helm + Prometheus + a real agent
```

`mutation_check.py` re-runs every hand-made mutation test: it reverts a defect
that actually shipped and fails if the test written to catch it still passes. A
passing suite is not evidence until you have seen it fail.

CI runs all of the above on every pull request — including the integration job,
which stands the chart up on kind and makes 48 assertions against the live
deployment plus 40 differential agent tests — alongside a dependency audit that
**fails the build** on a known vulnerability, a secret scan, and both Docker
builds.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the five design rules this codebase
holds to. They are load-bearing rather than stylistic.

## Two deployment shapes

The single-process default is *supported*, not a development fallback: nothing
loads `psycopg` or `grpc` unless you configure them, so `uvicorn app.main:app`
against nothing but a kubeconfig stays the getting-started path.

| | Single process | Fleet |
|---|---|---|
| Set | nothing | `DATABASE_URL` + `REDIS_URL` |
| Jobs | in memory, lost on restart | Postgres rows, reaped on worker death |
| Reports | local disk | Postgres blobs, any worker serves any report |
| Clusters | your kubeconfig | agents that dial out, no inbound port |
| Tenants | one | `TENANCY_MODE=shared`, isolated by row-level security |

Setting exactly one of `DATABASE_URL` / `REDIS_URL` is **refused at startup**
rather than half-configured.

## Known limitations

Stated plainly, because the alternative is you finding out during an incident.
Everything here is current as of the last commit; a stale limitation is treated
as a defect in this repository, not as harmless caution.

- **Nobody has run this in production.** Not one deployment, not one user
  besides its author. Every performance number came from kind clusters and
  synthetic fleets on one machine, and nothing has run longer than a few
  minutes. That is the largest gap between this and something you should trust
  with an incident, and no amount of further code closes it.
- **Peak memory scales with cluster size** on the kubeconfig path. `kubectl`
  assembles a whole list before writing it. Item counts are capped and
  truncation is recorded as an evidence gap, but the ceiling needs a streaming
  client. The agent path does not have this problem.
- **No live-model evaluation in CI.** The golden corpus gates the deterministic
  reasoning path — signals, hypotheses, ranking, grounding — on every push, and
  the model path has been exercised against a real cluster by hand. It is not
  gated automatically, so a prompt change that degrades a real model's answers
  would not fail a build. Three providers exist (`openai`, `anthropic`, and any
  OpenAI-compatible endpoint), but only one of them has ever been run in anger.
- **`fix` and `prevention` are model-authored prose** and therefore
  influenceable by injected cluster text. Grounding constrains them; it is not a
  proof. Commands are never model-authored.
- **Scale-out is flat past two workers on the kubeconfig path**, because each
  investigation spawns ~15 kubectl processes and process creation is a host
  resource. Add workers on an agent fleet, add hosts on a kubeconfig fleet.
  Cross-host is unmeasured.
- **`docs/SLO.md` proposes targets; it does not report attainment.** There is no
  production signal to measure against.

The full backlog, with severity and effort, is in
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md); the measured
numbers and what was *not* measured are in
[docs/PERFORMANCE_ENVELOPE.md](docs/PERFORMANCE_ENVELOPE.md).

## Documentation

Every document here is reachable from this page, and a test fails the build if
one stops being — an unreferenced document is one nobody reads and everybody
stops updating.

**Understanding it**
[Evidence](docs/EVIDENCE_ARCHITECTURE.md) ·
[Reasoning](docs/REASONING_ARCHITECTURE.md) ·
[Playbooks](docs/PLAYBOOKS.md) ·
[Dependency graph](docs/DEPENDENCY_GRAPH.md) ·
[Remediation](docs/REMEDIATION.md) ·
[Reports](docs/INCIDENT_REPORTS.md) ·
[Console](docs/SRE_CONSOLE.md) ·
[API](docs/INVESTIGATION_API.md) ·
[MCP tools](docs/MCP.md)

**Running it**
[Upgrade](docs/UPGRADE.md) ·
[Backup and restore](docs/RUNBOOK_BACKUP_RESTORE.md) ·
[Data protection](docs/DATA_PROTECTION.md) ·
[SLOs](docs/SLO.md) ·
[SSO group mapping](docs/SSO_GROUP_MAPPING.md) ·
[Tenant usage reporting](docs/TENANT_USAGE_REPORTING.md) ·
[Observability backends](docs/OBSERVABILITY.md) ·
[What Prometheus must scrape](docs/OBSERVABILITY_INTEGRATIONS.md)

**Trusting it**
[Production readiness](docs/PRODUCTION_READINESS.md) — the backlog, honestly scored ·
[Performance envelope](docs/PERFORMANCE_ENVELOPE.md) — measured, and what was *not* ·
[Integration verification](docs/INTEGRATION_VERIFICATION.md) — what CI stands up and asserts ·
[Evaluation](docs/EVALUATION.md) — the corpus gating reasoning quality ·
[Live-cluster audit](docs/QA_AUDIT_2026-08-03.md) — 26 findings against a real cluster, including five of the auditor's own that were wrong

**Design records**, kept as written rather than edited with hindsight:
[Enterprise architecture](docs/ENTERPRISE_ARCHITECTURE.md) ·
[Console redesign](docs/CONSOLE_REDESIGN.md)

## Releases

Versions are tagged and described in [CHANGELOG.md](CHANGELOG.md). While the
major version is `0`, a minor bump may carry a breaking change; each release
says which. `docs/UPGRADE.md` covers migrations, what a drain does and does not
cover, and the per-version behaviour changes.

## License

[Apache 2.0](LICENSE).
