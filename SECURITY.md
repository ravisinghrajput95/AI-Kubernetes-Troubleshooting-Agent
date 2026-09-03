# Security Policy

## Current status

**The authentication blocker is closed.** This document previously said "there
is no authentication on any endpoint", which stopped being true when F13 shipped
and stayed on the page afterwards — a security document describing a posture the
code no longer had, in both directions at once.

What is true today:

- Every endpoint requires a credential. `AUTH_MODE=disabled` exists for local
  development and **refuses to boot** unless `ALLOW_INSECURE_NO_AUTH=true` is
  supplied deliberately; nothing in this repository supplies it for you.
- Cluster reads are **impersonated as the calling user**, so the cluster applies
  their Kubernetes RBAC rather than the service account's.
- Authorisation is four roles per tenant, checked by one router-level
  dependency against a route → permission table in which **a route with no entry
  is denied**.
- In `TENANCY_MODE=shared`, tenant isolation is Postgres row-level security, and
  the platform refuses to start on a role that bypasses it.

**It is still not a finished product.** The remaining gaps are listed under
*Known gaps* below and tracked in
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md). The most important
one for a reader skimming: the **agent CA is a development CA** unless you
supply one. `AUTH_MODE` no longer defaults to anything — an unset value is
refused at startup naming all three modes — so a deployment authenticates
nobody only if someone typed `AUTH_MODE=disabled` and acknowledged it. Set
`AUTH_MODE=oidc` or `AUTH_MODE=token` for anything reachable by someone you do
not trust.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- Use GitHub's **Report a vulnerability** (Security → Advisories) on this
  repository.
- Include reproduction steps, affected version or commit, and impact.

You can expect an acknowledgement within 3 working days and an assessment within
10. Please allow 90 days before public disclosure, or less by agreement if a fix
ships sooner.

## Threat model

The platform investigates clusters that may already be compromised, so it treats
cluster content as hostile input.

**In scope:**

- Prompt injection via cluster-controlled text (log lines, event messages,
  resource names, ConfigMap keys)
- Credential leakage through reports, the HTTP API, or the LLM prompt
- Any path by which the platform mutates a cluster
- Path traversal or injection through API parameters
- Denial of service through unbounded cluster reads
- Cross-tenant read or write in `TENANCY_MODE=shared`
- Privilege escalation through the role model (granting above your own role,
  removing or suspending someone stronger, leaving a tenant with no owner)
- Forged or replayed event-ingress deliveries
- Data leaving the deployment through notifications, the model call, or
  `/metrics`

## Controls in place

| Control | Mechanism |
|---|---|
| Authentication | OIDC against the provider's JWKS, or static bearer tokens. Applied as a router-level dependency, so a new endpoint is protected by default. Validated at startup through the same builder the dependency uses, so a bad configuration refuses the boot rather than 500-ing every request behind a green `/health`. |
| Authorisation | Four roles (`viewer`/`operator`/`admin`/`owner`) against a route → permission table; **a route with no entry is denied**. A test derives the route list from the OpenAPI schema and fails if any route lacks an entry. You cannot grant a role you do not hold, and the last un-suspended owner cannot be demoted, removed or suspended. |
| Per-request impersonation | Every cluster read runs as the calling user, so the cluster applies their RBAC. Authentication decides *whether* you get in; this decides *what you can see*. |
| Tenant isolation | Postgres row-level security, `FORCE`d, with the tenant set per transaction via `SET LOCAL`. The platform **refuses to start** `shared` mode on a superuser or `BYPASSRLS` role, because policies are silently skipped for those — isolation with no symptom. |
| Ownership | History and jobs are owner-scoped. Denial returns 404 rather than 403 so it does not confirm an id exists; the permission check runs *first*, so 404-vs-403 cannot be used as an existence oracle. |
| Rate limiting | Keyed off the permission (`investigation.run`), not a path list, so a new costed endpoint is limited by declaring what it needs. Shared across workers when `REDIS_URL` is set. Fails **open** by design — availability protection against a noisy caller, not a control against a hostile one. |
| Audit trail | Append-only JSON lines recording actor, action, target, outcome, source IP and auth method, on a separate sink from application logging so a log-level change cannot silence it. |
| No cluster mutation | Two layers: `ReadVerb` is a closed enum that cannot express a mutation, and `assert_read_only()` allowlists verbs and sub-verbs on every call. Remediation commands are rejected by that same policy — the platform structurally cannot run its own recommendations. |
| No model-authored commands | Commands surfaced to operators are generated deterministically; anything the model returns is discarded. Every displayed command is classified, unrecognised strings dropped, mutating ones labelled. |
| Secret redaction | At the collection boundary, so reports, API and prompts see the same scrubbed data. Keyword, keyword-shape and credential-shape based (JWTs, AWS/Google keys, private key blocks, credentials in URLs), with a corpus test. **Best effort on free text** — see *Known gaps*. |
| Secret values never read | Referenced Secrets go through `kubectl describe`, which prints key names only. Asserted by a test that no command issues `get secret`. |
| Grounding | Model output is rejected if it cites signals or hypotheses that do not exist, **and** if its prose contradicts the signals it cites, cites nothing relevant to the selected hypothesis, or names resources appearing in no evidence. A rejected response falls back to the deterministic diagnosis. |
| Path containment | Report ids are format-validated and resolved paths checked against the reports directory. |
| Event ingress | HMAC over the body **and a timestamp**, so a captured request cannot be replayed with a fresh header. Five-minute tolerance, constant-time compare. The tenant comes from configuration, never the payload. |
| Action egress | An explicit field-by-field allowlist, not a filter — a denylist would leak whatever a future collector adds. A destination belongs to a tenant. |
| Metrics disclosure | No series carries a cluster, tenant, namespace, user or investigation id. Asserted end to end: an investigation runs against a named cluster and the name must appear nowhere in the exposition. That is what makes `/metrics` safe to leave unauthenticated. |
| Agent identity | Cluster agents authenticate by mTLS certificate. `AgentHello` cannot override it and a contradiction aborts the stream. Enrolment tokens are single-use (a conditional `UPDATE`, pinned by a concurrent test), short-lived and stored hashed; certificates rotate at 2/3 life and revocation is **swept against live streams**, not only checked at connect. |
| Agent keys never transmitted | An agent generates its own P-256 key and sends only a CSR. The platform certifies the public key and discards everything else the request claims about itself. |
| Agent read-only enforcement | The agent refuses a kind of evidence it does not know, in the customer's own cluster. Enforced only on the platform, the read-only guarantee would be a promise the customer cannot verify. |

## Known gaps

Not vulnerabilities to report — documented limitations.

- **`AUTH_MODE` has no default, as of v0.2.0.** An unset value is refused at
  startup, naming `oidc`, `token`, and `disabled`-plus-acknowledgement. This
  bullet used to describe a documentation hazard: the default *value* was
  `disabled`, which read as an insecure default while behaving safely, because
  `disabled` has always also required `ALLOW_INSECURE_NO_AUTH` and a fresh
  install refused to boot. That much was true, and an audit that scored this
  platform as shipping open was wrong about it.

  What the bullet missed is that the default made the acknowledgement
  *sufficient*: `ALLOW_INSECURE_NO_AUTH=true` on its own served every endpoint
  unauthenticated, so the insecure state was one variable away and nobody had
  said the word `disabled` — and an `AUTH_MODE` that failed to arrive, from an
  unmounted ConfigMap key or an unloaded `.env`, selected it silently rather
  than reporting itself missing. Absence now selects nothing. The insecure
  state costs two deliberate statements, and `TestNoModeIsChosenForYou` pins
  the refusals, including the one this closed. Breaking for a deployment that
  relied on the default — see `docs/UPGRADE.md`.
- **The agent CA is a development CA** unless you supply one. It is generated on
  first start, says so loudly, and its private key sits on the gateway's disk.
  Supply `AGENT_CA_CERT_FILE`/`AGENT_CA_KEY_FILE` from an issuer you control.
- **Agent enrolment bootstrap is trust-on-first-use** without `--ca-file`. That
  one call has nothing to verify the platform with; the CA it is handed is
  pinned thereafter. Supply the CA file in any deployment you care about.
- ~~Impersonation is not enforced by the cluster on the agent path~~
  **closed.** The agent applies `Impersonate-User` / `Impersonate-Group` to
  every read, so the API server — not the platform, and not the agent — decides
  what a request may see. Proved against a real cluster: a caller bound to one
  namespace is refused a cluster-wide list, is served the namespace they hold,
  and the same read through a non-impersonating agent returns the whole cluster
  (`TestTheClusterAppliesTheCallersRbac`). **Two caveats an operator needs.**
  An agent enrolled before this shipped does not impersonate — it lacks both
  the flag and the `impersonate` verb — and says so in its logs on every start;
  re-apply the enrolment manifest. And an impersonating agent **refuses** a read
  that names nobody rather than falling back to its own broad-read
  ServiceAccount, so `--impersonate` and `AUTH_MODE=disabled` are not a working
  pair. See `docs/UPGRADE.md`.
- **Redaction is best effort on free text.** It reliably catches credentials in
  recognisable shapes. It cannot catch a secret a log line prints with no marker
  and no shape — an account number, a customer email, a bare high-entropy
  string. Pod logs are the highest-risk surface because their content is
  entirely the application's choice. See `docs/DATA_PROTECTION.md`.
- **Model-authored *prose*** (`root_cause`, `explanation`, `fix`) can still be
  influenced by injected text. The *command* path is fixed and grounding
  constrains the prose, but it is not a proof.
- **No application-layer encryption of stored reports**, deliberately — it would
  encrypt one of four places customer data sits and leave `investigations.result`
  in the clear. Use storage-level encryption; reasoning in
  `docs/DATA_PROTECTION.md`.
- **The rate limiter fails open**, so a Redis outage removes it rather than
  refusing traffic.
- ~~`investigations.result` is not pruned by retention~~ **stale, and it was
  the wrong way round.** `ReportStore.prune()` nulls `investigations.result` in
  the same transaction that deletes the rendered artefacts, and the payload is
  the *larger* copy — 2.7 MB against a couple of hundred kilobytes. Fixed with
  F19; this bullet outlived it. Left visible rather than deleted, because a
  stale security claim is the same defect as a stale "this is dead" note: it
  invites a reader to plan around a gap that is not there.
- **Peak parse memory is proportional to cluster size, on both providers.**
  kubectl assembles a whole list before writing it, and the agent path is not
  exempt: `decode_payload` runs `json.loads` over the entire payload before
  anything caps it. Item counts *are* capped on both paths now and truncation is
  recorded as an evidence gap either way (F25 — until v0.2.0 the cap applied to
  the kubeconfig path alone), but capping happens after the document has been
  built, so it bounds the stored payload and not the spike. That needs a
  streaming client. Measured only on the kubeconfig path — 5.9 MB at 2,000 pods,
  29.7 MB at 10,000, 74.3 MB at 25,000 (`python scripts/payload_bench.py
  --parse-scan`); the agent path has the same shape by construction and has not
  been measured.

## Security-relevant tests

```bash
cd backend
python -m pytest tests/test_prompt_injection.py \
                 tests/test_redaction_corpus.py \
                 tests/test_remediation_safety.py \
                 tests/test_command_policy.py \
                 tests/test_history_durability.py \
                 tests/test_auth.py \
                 tests/test_authz.py \
                 tests/test_tenancy.py \
                 tests/test_metrics.py \
                 tests/test_event_ingress.py \
                 tests/test_agent_identity.py \
                 tests/test_agent_mtls.py
```

`tests/test_tenancy.py` connects as an **unprivileged** role deliberately: run
as `postgres` its isolation assertions all pass while proving nothing.

These encode past vulnerabilities as regression tests. Please do not weaken them
without replacing the control they protect.
