# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org/); while the major version is `0`, a minor bump
may carry a breaking change and each one says so under **Breaking**.

Entries record *why* a change was made and, where it matters, what it cost —
which is the same standard the rest of this repository's documentation is held
to. A change that fixed a defect names the defect.

## [Unreleased]

### Fixed

- **A remediation plan named an object no evidence had identified.**
  `workload.missing_configuration` fires from `pod.config_error` alone — a pod
  in CreateContainerConfigError, which carries the pod and the namespace and
  nothing about what it references — so `kind` and `name` fell back to
  `ConfigMap` and `<name>` *silently*. The plan then asserted "ConfigMap
  payments/`<name>` is referenced by the pod but does not exist" as a finding,
  generated a `<name>-configmap.yaml` containing `name: <name>`, and handed the
  operator `kubectl get configmap <name> -n payments`. The `kind` was a guess
  that could as easily have been Secret — which would have cost that branch its
  "values are never generated" note.

  `MemoryLimitRule` already had the shape: when the evidence is absent it says
  so, uses a placeholder naming what is missing rather than the thing itself,
  and carries a caveat. This rule now does the same — an honest title and
  summary, `<name-from-the-pod-spec>`, a caveat leading the list, and **no
  generated manifest**, because a file built round a placeholder is not
  appliable and offering one implies knowledge the platform does not have.

  Found by rendering a report to PDF and reading it, which is where an operator
  meets it. Four mutations watched fail, including the control proving the
  refusal is not blanket; one added to `scripts/mutation_check.py` (34 → 35).

- **Three console routes scrolled sideways, and the sidebar went with them.**
  Fleet rendered 2,827px of content in a 1,440px viewport; `/investigations`
  and `/ask` the same, for the same reason. A `<li>` that is a grid item keeps
  `min-width: auto` — "at least min-content" — and its content is `truncate`
  (`white-space: nowrap`), so min-content is the whole unwrapped sentence. A
  cluster whose last investigation produced a long health message stretched a
  1,032px card to 2,511px. In every case the inner flex chain already carried
  `min-w-0`; the grid item that needed it did not.

- **Duplicate React keys on report body lines.** A report legitimately repeats
  a line — two collectors reading nodes emit the identical
  `kubectl … get nodes -o json`, and the Evidence section repeats a gap line per
  target — so keying by text collided and React logged an error on every report
  view. React documents duplicate keys as unsupported ("children may be
  duplicated and/or omitted"); no omission was observed, and the keys are now
  `${line}-${position}`, the shape this file already used for table rows.

  All four were invisible to the 256 frontend tests, which pass with every one
  of them present: jsdom has no layout engine, so a test that queries by role
  passes against a page that looks wrong. `scripts/console_check.mjs` drives
  headless Chrome and checks both properties across every route, refusing to
  report a clean run for a page that rendered nothing — a blank page and the
  sign-in gate both pass every assertion otherwise.

- **MCP announced a version the project has never released.**
  `app/mcp/server.py` hardcoded `serverInfo.version: "1.0.0"` while
  `/openapi.json` served `0.2.0`, so an agent gating on the handshake — or a
  person reading it out of a log — got a wrong answer from a public surface.
  Nothing objected because the MCP test asserted `serverInfo["name"]` alone and
  never looked at the version.

  There is now one version, `app/core/version.VERSION`, read by both the
  FastAPI app and the MCP handshake — the two copies had already drifted, which
  is the argument. `tests/test_documentation.py` holds it against a matching
  `CHANGELOG.md` section and `tests/test_mcp.py` holds the handshake against
  it, so a bump without release notes fails and a release without a bump fails.

  Found by exercising the MCP surface live rather than by reading it.

- **F25: `MAX_LIST_ITEMS` did not apply on the agent path.** The cap lived
  inside `KubectlExecutor`, so it bounded the kubeconfig path and nothing else;
  `RemoteAgentProvider._truncations` was initialised and never appended to,
  existing only to satisfy the protocol. An agent-reached cluster was therefore
  read with no ceiling, and `collection_limits.truncated` reported `false` for
  a read that had never been bounded — so the memory envelope the platform
  publishes did not hold on the transport it is built around for real fleets,
  and the same cluster investigated two ways disagreed about how many pods it
  has.

  Measured at `MAX_LIST_ITEMS=3` against a ten-pod namespace: kubeconfig gave
  `total_pods: 3`, `truncated: true` and four truncation records naming
  returned and retained; the agent gave `total_pods: 10`, `truncated: false`
  and none. After the fix both give four identical records.

  The rule now lives in one place, `app/kubernetes/list_limit.cap_items`, which
  both providers call — two implementations of one rule drift.

  **The first version of the fix introduced a divergence in the opposite
  direction**, and the live run is what caught it: gated on the payload merely
  having an `items` key, it also truncated `kubectl top`, which is text on the
  kubeconfig path and a metrics.k8s.io list through an agent. That run reported
  five truncation records against the kubeconfig path's four. It is now gated
  on `request.is_list`, the counterpart of the executor's own `_is_list_read`,
  so both providers bound exactly the same set of reads.

  Found by diffing the recorded `equivalent_command` of an agent-served
  investigation against a kubeconfig-served one after the status diff came back
  clean across four scopes — the kubeconfig reads carried `--chunk-size=500`
  and the agent's carried no limit at all.

## [0.2.0] — 2026-09-03

One breaking change and five defects, and **every one of the five was found by
running the platform rather than by reading or testing it** — two by the
one-hour soak, three by standing a live cluster up and using it. The suite was
green throughout, and stayed green while three of these were live.

Two of them are the same shape and worth naming as a class: an agent-path read
that came back describing something other than what was asked for, while the
identical read through a kubeconfig was correct. Neither was visible to the
kind tables or to the differential suite, because in both cases the *kind* was
right and what was wrong was a **parameter** — and nothing compared parameters.
Something does now. It found the second defect within minutes of being written,
and then objected to its own stale exception the moment that fix landed.

The method behind both is worth more than either fix: run an agent-served
investigation and a kubeconfig-served one against the same namespace in the
same minute, and diff the evidence by id and status. That diff has now produced
three defects across two sessions, and it is the first thing to reach for with
a live cluster.

The breaking change is `AUTH_MODE` losing its default. Read `docs/UPGRADE.md`
before upgrading — every claim in that section has now been checked by running
it, including the chart's refusal at `helm template` and both compose paths.

### Fixed

- **F24: a pod with more than one container had no logs at all through an
  agent.** Both log collectors send `all_containers`, which kubectl expands
  client-side — read the pod, fetch each container's log, concatenate. The
  agent had no such expansion, so it issued one read naming no container and
  the API server answered `BadRequest: a container name must be specified`.
  Sidecars are the common case, so an agent-reached cluster lost the single
  most useful evidence a crash has while the same cluster read through a
  kubeconfig kept it — silently, as a failed record inside an investigation
  that succeeds.

  The agent now performs the same expansion, with the pod read and every
  per-container log read still resolved through `policy.Resolve`, so it adds no
  capability that package would not already have allowed. kubectl's container
  order — init containers first — was established against a live cluster rather
  than assumed. Verified by reverting the defect into a live harness: with it
  present the sidecar pod reads `a container name must be specified ... choose
  one of: [app sidecar]` and no lines; with the fix, exactly what `kubectl logs
  --all-containers=true` returns. A scoped differential over that pod gives 55
  evidence records and zero status differences between the two providers.

  Found by the parity check added for the `previous` defect below, which asks
  whether a parameter the platform sends is one the agent reads at all.

- **Previous-container logs were the current container's, through an agent.**
  `spec_for` serialised option booleans with Python's `str()`, so `previous`
  reached the agent as `"True"` where it compares literally against `"true"`.
  The option was dropped, the log endpoint served the current container, and
  the record was filed under `k8s.pod.logs.previous` with status OK — evidence
  labelled "the container instance that existed before the last restart"
  holding the one after it, cited as such, on the CrashLoopBackOff
  investigations where the previous instance is the only thing that says why it
  crashed. It counted as a usable read, so completeness rose rather than fell.
  Booleans now serialise lowercase.

  Found by diffing an agent-served investigation against a kubeconfig-served
  one of the same namespace in the same minute — the way the `OutputFormat.TEXT`
  defect on the baseline log read was found. One evidence status differed and
  coverage read 39/48 against 40/48; after the fix, 57 records and no
  difference at all. Neither the kind tables nor the differential suite could
  see it: the kind was right and the parameter was wrong, and nothing compared
  parameters. They are compared now, which immediately found F24 below.

- **`docker compose up` published the backend on a port the console was not
  reading.** The backend was published as the range `8000-8009:8000` on the
  belief that the first replica takes the low end. Docker's allocator keeps a
  cursor per range and walks forward on each allocation, wrapping at the top —
  so the *first* `up` on a given daemon bound 8000 and the console worked, and
  every recreate after that drifted to 8001, 8002, ..., back to 8000 about one
  run in ten. Measured over eleven consecutive up/down cycles and confirmed
  against a never-used range, which starts low and then walks identically.

  That is the worst shape available for a getting-started path: it works the
  first time you try it, which is when you write it down, and then silently
  stops. The backend is now published on a fixed `8000:8000`, and the
  multi-worker demonstration moves to `docker-compose.scale.yml`, where a
  variable port is inherent and is documented with the command that reports it
  rather than hidden.

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
