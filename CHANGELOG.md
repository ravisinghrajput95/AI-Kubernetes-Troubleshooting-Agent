# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org/); while the major version is `0`, a minor bump
may carry a breaking change and each one says so under **Breaking**.

Entries record *why* a change was made and, where it matters, what it cost —
which is the same standard the rest of this repository's documentation is held
to. A change that fixed a defect names the defect.

## [Unreleased]

### Fixed

- **F23**: M8a's fail-open is countable. `k8sagent_agent_presence_failopen_total`
  plus `AgentPresenceUnreadableEnoughToMisroute`. The existing 10% rule is for
  routing being *broken*; `cluster_access_total` structurally cannot express a
  fail-open, since it and a correct local read are both `provider=kubeconfig`.
- **F22**: a kubectl read forked from a process holding gRPC keeps its own
  stderr (`GRPC_ENABLE_FORK_SUPPORT=0`, set in `app/__init__` because the
  variable is read at gRPC's first initialisation and after `import grpc` is
  already too late). **Reproduces on macOS only** — 40/40 polluted on darwin,
  0/40 in a Linux container with or without the fix — so the soak that found it
  was measuring the development machine, and this never affected a shipped
  deployment. Kept anyway; it costs one line.

### Breaking

- **`AUTH_MODE` has no default.** An unset value is refused at startup, naming
  `oidc`, `token`, and `disabled`-with-`ALLOW_INSECURE_NO_AUTH`. A deployment
  that set only the acknowledgement and inherited the mode will not start until
  it names one; a deployment that already names a mode is unaffected.
  `docker-compose.yml` stops passing `${AUTH_MODE:-disabled}` and the Helm
  chart's `auth.mode` becomes required, both refusing rather than choosing.
  Migration in `docs/UPGRADE.md`.

  The old default was **not** the open deployment it read as, and it is worth
  being exact about that because an audit of this repository got it wrong and
  scored the platform as shipping open: `disabled` has always also required
  `ALLOW_INSECURE_NO_AUTH`, so a fresh install with no configuration refused to
  boot. What the default actually cost is that the acknowledgement doubled as
  the mode selection — `ALLOW_INSECURE_NO_AUTH=true` on its own was sufficient
  to serve every endpoint unauthenticated, and `docker-compose.yml` taught that
  one-liner — and that an `AUTH_MODE` which failed to arrive, from an unmounted
  ConfigMap key or an unloaded `.env`, selected the insecure mode silently
  instead of reporting itself missing. Absence now selects nothing, and the
  open deployment costs two deliberate statements rather than one.

  The test class that used to argue *for* keeping the default is the one that
  now pins its removal, and one of its cases asserted the defect directly:
  `test_the_acknowledgement_is_what_permits_it` set only
  `allow_insecure_no_auth` and required `validate_auth()` to succeed. Two
  mutations in `scripts/mutation_check.py` — the settings default and the
  `or "disabled"` fallback in `build_authenticator`, which is the same defect
  from the other side — both caught.

## [0.1.0] — 2026-09-01

The first tagged release. Everything below already existed on `main`; this is
the point at which it becomes something you can pin.

**No production deployment exists.** Every number in this release was measured
on kind clusters and synthetic fleets on one machine, and there is no user but
the author. Read `docs/PRODUCTION_READINESS.md` before trusting it with an
incident.

What that caveat no longer has to say is "nothing has run longer than a few
minutes". It has now run for **one hour continuously**: 1,168 investigations
through a real Go agent against a real cluster, **all of which collected usable
evidence**, spread evenly across the hour rather than bunched at the start —
resident memory flat, three certificate renewals with no dropped stream, 23,589
SSE frames with none out of order or duplicated, and the retention sweep firing
on the platform's own timer. `docs/PERFORMANCE_ENVELOPE.md` has the table and,
just as importantly, what an hour still does not tell you.

It is worth saying how that number was earned, because the first attempt at it
was not. A 60-minute run had already been declared: 1,172 investigations, and
a report full of healthy-looking memory trends. Docker Desktop had killed the
cluster four minutes in, 1,091 of those investigations failed with `Unable to
connect`, and the harness's vacuity guard — an absolute floor — passed them.
The guard now asks three questions instead of one (did enough happen, was the
platform *working*, was it working *throughout*), prints a breakdown of why
things failed above the verdict rather than below it, and is itself pinned by
`tests/test_soak_guard.py` against the exact run that fooled it.

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
- **The reasoning layer is scored against a real model in CI**, not only against
  a golden corpus. `python -m evals` proves the rules and the grounding checks
  offline; `python -m evals.live` measures the one thing that corpus cannot see
  — of the cases where the model actually answered, how many survived grounding.
  An over-strict grounding check does not fail loudly, it routes every
  investigation to the deterministic fallback while 20/20 golden cases keep
  passing, and a prompt edit that degrades a real model has the same signature.
  It **refuses rather than skips**: no configured model is exit 2, and a run
  where every call failed is refused rather than reported as zero rejections,
  which is what a total provider outage otherwise looks like.

### Performance

- A repeat investigation of the same cluster spawns **13 kubectl processes
  instead of 70** and collects in **0.16 s instead of 0.57 s**, measured on a
  real cluster. Evidence built from a reused read carries the age of the read,
  so a citation still means what it says.
- 1,000 clusters attach to one gateway in 1.04 s; throughput is ~12/s per
  worker and scales linearly with workers on the agent path. The full envelope,
  including a throughput figure that was published wrong twice, is in
  `docs/PERFORMANCE_ENVELOPE.md`.
- **One hour of continuous operation**: 1,168 investigations, 100% collecting
  usable evidence, p50 0.26 s, resident memory flat (+0.8 MB/h on one worker
  and +7.9 on the other, against 6-10 MB of total movement), 74% of cluster
  reads served from the F18 cache, and Postgres growing at 87.9 MB/h before
  retention collects anything.
- The console's own bundle is 28.76 KB gzipped for the app chunk, and `App.tsx`
  is 98 lines — the routing table and the sign-in gate. There is no HTTP client
  library; the `fetch` wrapper that replaced axios saved more bytes than the
  console's own code weighs.

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
- **An agent's evidence records were matched back to their requests by kind
  alone**, and a collection wave routinely holds several reads of one kind
  differing only by target — `LogsCollector` issues one `k8s.logs` per
  problematic pod. Records were handed out in arrival order, so one pod's logs
  were filed under another pod's name: **5.5% of pod-log entries over an hour
  against a real agent**, counting only the ones detectable because the message
  named a different pod. A mis-paired *success* is the same defect with no trace
  at all — a diagnosis quoting the wrong container's output, with a citation.
- **The baseline pod-log read asked for JSON.** `OutputFormat` defaults to it,
  and that default decides whether the executor calls `json.loads`, so on the
  kubeconfig path the read *failed for every pod that had anything to say* and
  succeeded for the silent ones, whose empty output parsed as `{}`. Exactly
  inverted: the pods whose logs matter are the crashing ones. kubectl exited 0
  with an empty stderr, so the failure carried no reason. The agent path was
  unaffected, which is why nothing compared them.
- **Certificate renewal was unbounded below a certificate lifetime of 150
  seconds.** The CA backdates `NotBefore` by five minutes for clock skew and
  the renewal point counts that backdate as life, so the moment of renewal was
  already past when the certificate was issued — and every check tick minted
  another. Measured against a real agent: twelve certificates a minute,
  indefinitely, each a CA signature and a row. The agent cannot detect this by
  arithmetic, because a certificate records when it became *valid* and never
  when it was *issued*, so it bounds what it can — attempts, not successes.
- **M8a's routing and its refusal were both inert on a worker running no
  gateway of its own** (F21), because the presence index and the enrolment
  store were installed inside that branch of startup. Unreachable in the shipped
  topology — one Deployment, one config, N replicas — and reachable by a fleet
  mid-way through enabling `AGENT_GATEWAY_PORT`, where it is the cross-tenant
  answer the refusal exists to prevent. Found by a soak that gave one worker a
  gateway and not the other.

[0.1.0]: https://github.com/ravisinghrajput95/ai-kubernetes-agent/releases/tag/v0.1.0
