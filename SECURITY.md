# Security Policy

## Current status: not production ready

This project is **not yet safe to deploy against a production cluster.** The
known blocking issue is tracked in
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md):

> **There is no authentication on any endpoint.** The service holds a kubeconfig,
> so anyone who can reach the port has read access to everything that kubeconfig
> can reach, plus every previous investigation report.

Run it only against non-production clusters, on a trusted network, bound to
localhost. CORS is not a security control.

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

**Out of scope today** (known gaps, not vulnerabilities to report):

- Missing authentication and authorization — tracked as F13
- Multi-tenancy and history isolation
- **Agent enrolment bootstrap trust.** An agent's first call has nothing to
  verify the platform with unless `--ca-file` is supplied out of band; without
  it that one call is trust-on-first-use, and the CA it is handed is pinned
  thereafter. Supply the CA file in any deployment you care about.
- **The agent CA is a development CA.** It is generated on first start and its
  private key sits on the gateway's disk. A production deployment should supply
  `AGENT_CA_CERT_FILE`/`AGENT_CA_KEY_FILE` from an issuer it controls.
- **Per-request Kubernetes impersonation is not enforced by the cluster.** The
  calling principal travels on the wire, but an agent's ServiceAccount still
  bounds what can actually be read (ADR-005, second half)
- Model-authored *prose* (`root_cause`, `explanation`, `fix`) being influenced by
  injected text — the *command* path is fixed, the prose path is tracked as F9

## Controls in place

| Control | Mechanism |
|---|---|
| No cluster mutation | Read-only verb allowlist in `command_policy.py`, enforced on every call. Remediation commands are rejected by this same policy — the platform structurally cannot run its own recommendations. |
| No model-authored commands | Commands surfaced to operators are generated deterministically. Anything the model returns is discarded. |
| Command classification | Every displayed command is classified; unrecognised strings are dropped, mutating ones labelled. |
| Secret redaction | At the collection boundary, so reports, API and prompts see the same scrubbed data. Keyword and shape based, with a corpus test. |
| Secret values never read | Referenced Secrets go through `kubectl describe`, which prints key names only. Asserted by test. |
| Citation integrity | Model output is rejected if it cites signals or hypotheses that do not exist. Note this validates *provenance*, not semantics. |
| Path containment | Report ids are format-validated and resolved paths are checked against the reports directory. |
| Agent identity | Cluster agents authenticate by mTLS certificate. `AgentHello` cannot override it and a contradiction aborts the stream. Enrolment tokens are single-use, short-lived and stored hashed; certificates rotate at 2/3 life and are revocable against live streams. |
| Agent keys never transmitted | An agent generates its own P-256 key and sends only a CSR. The platform certifies the public key and discards everything else the request claims about itself. |

## Security-relevant tests

```bash
cd backend
python -m pytest tests/test_prompt_injection.py \
                 tests/test_redaction_corpus.py \
                 tests/test_remediation_safety.py \
                 tests/test_command_policy.py \
                 tests/test_history_durability.py \
                 tests/test_agent_identity.py \
                 tests/test_agent_mtls.py
```

These encode past vulnerabilities as regression tests. Please do not weaken them
without replacing the control they protect.
