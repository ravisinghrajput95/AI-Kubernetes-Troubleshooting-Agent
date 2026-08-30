# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org/); while the major version is `0`, a minor bump
may carry a breaking change and each one says so under **Breaking**.

Entries record *why* a change was made and, where it matters, what it cost —
which is the same standard the rest of this repository's documentation is held
to. A change that fixed a defect names the defect.

## [0.1.0] — 2026-08-30

The first tagged release. Everything below already existed on `main`; this is
the point at which it becomes something you can pin.

**No production deployment exists.** Every number in this release was measured
on kind clusters and synthetic fleets on one machine. Nothing has run longer
than a few minutes, and there is no user but the author. Read
`docs/PRODUCTION_READINESS.md` before trusting it with an incident.

### Investigation

- Evidence-driven pipeline: every collected fact is an addressable record with
  a deterministic id, a status, and the command that produced it. A failed
  collection is *citable data* — "Prometheus was unavailable" — never silence.
- Deterministic reasoning before any model call: evidence becomes signals by
  rule, signals become ranked hypotheses by rule. The model selects and
  explains; it never diagnoses from raw JSON.
- Iterative investigation. Each hypothesis declares what evidence would confirm
  or refute it, and a playbook collects exactly that. In the reference scenario
  this moves the conclusion from "application fails on startup" (confidence 76)
  to "pod references configuration that does not exist" (confidence 94), while
  refuting the original reading.
- A cluster dependency graph derived from evidence rather than emitted by
  collectors, so it is reproducible from a stored report.
- Remediation plans keyed on the hypothesis, risk-rated, and **never
  applicable**: the read-only policy rejects the commands the platform itself
  generates, asserted for every rule.

### Safety

- All cluster access is read-only by construction. `ReadVerb` is a closed enum
  with no field that can carry a command, and a second allowlist runs at the
  executor.
- Prompt injection closed at the boundary that mattered. A hostile pod log line
  was verified to produce `kubectl delete ns kube-system` as an operator-facing
  recommendation; commands are now never taken from the model, and every
  surfaced command is classified.
- Grounding rejects a model response whose citations do not resolve **and** one
  whose prose contradicts what it cites — a response citing a genuine
  CrashLoopBackOff while concluding "resolved, no action needed" is discarded.
- Secret values are never read. Referenced Secrets go through `describe`, which
  prints key names only.

### Platform

- **Authentication and authorisation**: OIDC or static tokens, four roles per
  tenant, checked by one router-level dependency against a route → permission
  table in which a route with no entry is *denied*.
- **Multi-tenancy** under Postgres row-level security, with the tenant ambient
  rather than an argument — no store method mentions one.
- **Per-request Kubernetes impersonation on both paths**, so the cluster
  applies the caller's RBAC rather than the service account's.
- **Distributed deployment**: set `DATABASE_URL` and `REDIS_URL` and any worker
  serves any investigation; a dead worker's job is reaped rather than hanging.
  The single-process default remains supported.
- **Cluster agents** that dial out over mTLS, so no inbound port is opened into
  a customer cluster. Single-use enrolment tokens, certificate rotation at 2/3
  of life without dropping the stream, and revocation swept against live
  sessions rather than checked only at reconnect.
- Rate limiting, an append-only audit log, `/metrics` with 17 burn-rate alert
  rules, phase timing, correlation ids, and split liveness/readiness probes.
- Alert-triggered investigations, signed outbound notifications, and an MCP
  server exposing the platform's read capabilities as tools.
- A Helm chart that reproduces the platform's startup refusals at render time.

### Performance

- A repeat investigation of the same cluster spawns **13 kubectl processes
  instead of 70** and collects in **0.16 s instead of 0.57 s**, measured on a
  real cluster. Evidence built from a reused read carries the age of the read,
  so a citation still means what it says.
- 1,000 clusters attach to one gateway in 1.04 s; throughput is ~12/s per
  worker and scales linearly with workers on the agent path. The full envelope,
  including a throughput figure that was published wrong twice, is in
  `docs/PERFORMANCE_ENVELOPE.md`.

### Breaking

Nothing to break — this is the first tagged release. Two behaviours are worth
knowing before you deploy:

- An agent started **without** `--impersonate` reads as its own ServiceAccount
  and logs a warning saying so. The enrolment manifest sets the flag and grants
  the verb together.
- `--impersonate` and `AUTH_MODE=disabled` are not a working pair: with
  authentication off there is no caller to read as, and an impersonating agent
  refuses an unattributed read rather than falling back to its own identity.

### Known defects fixed close to the release

Recorded because they say something about where the remaining risk is, not to
pad the list. Each was found by *running* the system, not by reading it.

- Eight deep-investigation reads named a resource the cluster agent had no kind
  for, so an agent-reached cluster produced a shallower investigation than the
  same cluster read locally, and nothing compared the two.
- The agent mapped every `404` to `EMPTY` — a status the platform counts as
  usable — so an uninstalled metrics-server read as an idle cluster and *raised*
  the confidence of a diagnosis that had seen less.
- Every agent-reported failure said `unknown`, because client-go reports that
  for any error on a raw request. A permissions problem was indistinguishable
  from a broken cluster.
- The differential suite that exists to catch agent/kubeconfig divergence was
  comparing whichever cluster `current-context` happened to name, and ran
  nowhere: nothing in CI set the variable that enables it. Both fixed; it now
  runs on every push.

[0.1.0]: https://github.com/ravisinghrajput95/ai-kubernetes-agent/releases/tag/v0.1.0
