# Production Readiness

Findings from the 2026-07-26 engineering review, and their current state.

**The P0 section is closed.** That makes a controlled pilot defensible; it does
not make this a finished product — see P1, which still contains the items that
decide whether it survives a real incident at scale.

**The review's own sections are now all closed. That is not the same as nothing
being open**, and *Open — found after the review* below is where anything found
since goes; it currently holds F24, which costs a multi-container pod its logs
on the agent path.

Scores at review time: architecture 7/10 · Kubernetes 5/10 · AI 6/10 ·
security 3/10 · performance 5/10 · maintainability 6/10 · OSS 2/10 ·
enterprise 3/10.

---

## P0 — blocks any deployment

### F13 · No authentication or authorization · **FIXED**

Every endpoint is unauthenticated. The service holds a kubeconfig, so anyone who
can reach the port has read access to everything that kubeconfig can reach, plus
the full archive of previous investigations. CORS is not a security control — it
constrains browsers, not `curl`.

**Fixed by:** pluggable authentication (OIDC against the provider's JWKS, static
tokens, or explicitly-acknowledged disabled), applied as a router-level
dependency so a new endpoint is protected by default. **Per-request Kubernetes
impersonation** means every cluster read runs as the calling user, so the
cluster applies their RBAC rather than the service account's — authentication
alone would decide *whether* you get in, not *what you can see*. History and
jobs are owner-scoped, and denial returns 404 rather than 403 so it does not
confirm an id exists.

**Authorisation followed** (M6.5): four roles per tenant (`viewer` /
`operator` / `admin` / `owner`) under the same row-level security, checked by a
single router-level dependency against a route → permission table in which **a
route with no entry is denied**. OIDC groups map to roles so the customer's IdP
drives it. Single-tenant deployments keep working unchanged via
`RBAC_DEFAULT_ROLE=admin`, which is refused above `viewer` when
`TENANCY_MODE=shared`. Thirteen mutation tests, all caught.

**Two defects fixed on the way:** `GET /investigations/{id}/events` had no
ownership check and streamed any investigation to any authenticated caller who
knew the id; and `require_principal` was a synchronous dependency, so M6's
ambient tenant never survived into the request — every tenant's rows were
written into `default` and readable by every other tenant, with row-level
security enabled, forced and correct.

**Rate limiting followed**: the operations that cost a customer's cluster and a
model call — exactly those needing `investigation.run` — are capped per caller
and, optionally, per tenant. The counter is shared across workers when
`REDIS_URL` is set, because a per-process limit on three replicas is three
times the configured rate. It fails *open* by design, unlike authorisation:
availability protection against a noisy caller, not a control against a hostile
one.

**Startup validation followed**: authentication was the only configuration
checked lazily, so an invalid one started the service and then failed every
request while `/health` stayed green. It is now validated in `build_state()`
through the same builder the dependency uses. And `docker-compose.yml` no
longer sets `ALLOW_INSECURE_NO_AUTH` for the operator — pre-setting it was the
careless deployment this finding describes, shipped in the repository.

**Remaining (P1):** none, and as of v0.2.0 there is no default mode at all.
`AUTH_MODE` unset is refused at startup naming all three ways forward. The
previous default of `disabled` was not the open deployment it read as — it has
always also required an acknowledgement nothing in this repository supplies —
but it made that acknowledgement *sufficient*: `ALLOW_INSECURE_NO_AUTH=true`
alone served every endpoint unauthenticated, with nobody having chosen
`disabled`, and an `AUTH_MODE` that failed to arrive selected it silently.
Absence now selects nothing, in the platform, in `docker-compose.yml` and in
the chart. Breaking for a deployment that relied on the default; two mutations
in `scripts/mutation_check.py`, both caught.

Phase timing closed the traces item, though not in the shape the finding
assumed: `k8sagent_investigation_phase_seconds` answers "where did the time go"
from a scrape, and OTLP export is blocked by `opentelemetry-proto` requiring
`protobuf<7.0` against this project's load-bearing 7.x pin. Cross-worker trace
correlation is the stated casualty. Taking the phase measurement immediately
corrected a published throughput figure, which is the return on it.

### F14 · Prompt injection reached operator-facing commands · **FIXED**

Verified exploitable: a hostile pod log line caused the model to emit
`kubectl delete ns kube-system` as a recommended command, and grounding accepted
the response because it cited a real signal. The operator is the execution path,
so the read-only executor did not mitigate it.

**Fixed by:** commands are never taken from the model (`_normalize` uses the
deterministic set); every surfaced command is classified by
`classify_command()`, unrecognised strings dropped and mutating ones labelled;
the prompt labels cluster text as untrusted data and no longer requests commands.
Regression tests in `tests/test_prompt_injection.py`.

### F20 · History index corruption and lost entries · **FIXED**

Read-modify-write with a non-atomic write. Concurrent saves lost entries; a crash
or full disk truncated the file; a parse failure **silently discarded all
history**.

**Fixed by:** temp-file + `os.replace()`, `fsync`, an in-process lock, and
quarantining a corrupt index instead of discarding it. Tests include a 12-thread
concurrency check.
**Remaining:** with the filesystem store, cross-process writers can still
race. Setting `DATABASE_URL` makes history a Postgres row and removes the race
entirely; the index-file path stays for single-process deployments.

### F15 · Redaction missed real credential shapes · **FIXED**

Four of eight shapes leaked: bare JWTs, connection-string passwords, AWS keys,
PEM private key blocks. These reached reports on disk, the API, and the model.

**Fixed by:** shape-based detectors alongside the keyword rules, and a corpus
test (`tests/test_redaction_corpus.py`) that also asserts benign log lines are
untouched. Verified end to end: hostile log content containing a JWT and a
connection-string password is scrubbed before it reaches the investigation
payload or the diagnosis.

**Residual (P2):** redaction happens *only* at the collection boundary. An
investigation dict assembled by any other route — a future import path, or
`regenerate` reading a report written before these detectors existed — is not
re-scrubbed. `PromptBuilder` re-runs the redactor so the model is covered, but
the API response and reports are not. Add a redaction pass at the persistence
boundary as defence in depth.

### F9 · Grounding validated provenance, not semantics · **FIXED**

A response could cite a genuine CrashLoopBackOff signal and still conclude
"Resolved - no action needed"; every id resolved, so it was accepted.

**Fixed by** three deterministic checks on top of citation integrity:
contradiction (reassurance language over severe signals), citation relevance (a
citation must support the selected hypothesis), and invented resources
(`namespace/name` references appearing in no evidence).

Deliberately lenient, because an over-strict check does not fail loudly — it
silently sends every investigation to the fallback. False-positive tests guard
the fallback rate alongside the rejection tests.

**Remaining:** `fix` and `prevention` are still model-authored prose. Commands
never are.

### F16 · Path traversal in report id handling · **FIXED**

Ids were interpolated into filesystem paths unchecked. **Not reachable over
HTTP** — Starlette rejected all four probes — so this was defence-in-depth, not a
live vulnerability.

**Fixed by:** id format validation plus a containment check on the resolved path.

### LICENSE missing · **FIXED**

Apache-2.0 added. Without it the work was legally unusable by any enterprise.

---

## P1 — blocks production

| # | Finding | Effort |
|---|---|---|
| — | ~~No audit logging~~ **fixed** (F17): append-only JSON lines recording actor, action, target, outcome, source IP and auth method, on a separate sink from application logging so a log-level change cannot silence it. Exercised in the live audit (§2, row 14). | done |
| F5 | ~~Unbounded all-namespace reads~~ **partially fixed, and the remaining half is now measured rather than estimated.** API-server paging via `--chunk-size`, retained items capped, truncation recorded as an evidence gap. Peak *parse* memory is still proportional to cluster size, and `python scripts/payload_bench.py --parse-scan` now says by how much: one `kubectl get pods -o json` through the real executor peaks at **5.9 MB at 2,000 pods, 29.7 MB at 10,000 and 74.3 MB at 25,000** — about 2.95 KB per pod, 5.5× kubectl's own output — while what is *retained* stays flat at 1.09 MB. The cap applies after `json.loads` has built the whole document, so it bounds the payload and not the spike. **It needed a new measurement because the existing harness could not see it**: `payload_bench`'s fake overrides `KubectlExecutor.run`, so neither the parse nor the cap runs in it, and above 2,000 pods it reports a stored result that grows because nothing in that path caps anything. **Decision: deferred, not scheduled.** At 10,000 pods a worker at the default `JOB_MAX_CONCURRENT=4` transiently touches ~119 MB against a 159 MB resident platform, while the measured ceiling is per-worker throughput at ~12/s with the worker 92% idle in socket waits — CPU and the GIL, not memory. Five days on a streaming client, replacing the only path that shells out, would not move the number that actually binds. The lever that exists today is scope: `MAX_LIST_ITEMS` does not change the spike, investigating a namespace does. | 3d done, 5d **deferred with the numbers** |
| — | ~~In-process job store: no HA, single worker mandated~~ **fixed** (M3): setting `DATABASE_URL` and `REDIS_URL` moves jobs, events and reports to Postgres and Redis, so any worker can serve any investigation and a crashed worker's job is reaped to a terminal state instead of hanging. The in-process store remains the default for single-process deployments. Not delivered: mid-run resumption — a lost worker's investigation is failed, not resumed. | done |
| — | ~~No platform self-observability~~ **partially fixed**: `/metrics` in Prometheus format, with the metric set chosen from `docs/PERFORMANCE_ENVELOPE.md` so every number that document tells an operator to act on is observable. No cluster, tenant, namespace or user is ever a label — cardinality and disclosure both forbid it — which is what makes the endpoint safe to leave unauthenticated. **Phase timing and 17 burn-rate alert rules followed** (`deploy/alerts/`), with every series and filtered label asserted against the real exposition. OTLP export is deliberately not built: `opentelemetry-proto` requires `protobuf<7.0` against this project's load-bearing 7.x pin, and cross-worker trace correlation is the stated casualty. | done, less OTLP |
| F11 | ~~No LLM eval harness~~ **largely fixed**: a golden corpus of 20 investigation cases plus 11 grounding cases gates reasoning accuracy, **detection recall** and grounding behaviour in CI (`docs/EVALUATION.md`). The model path has since been exercised live against a real cluster (10/10 grounded, 0 fabricated citations). **The provider abstraction followed**: `app/ai/providers/` carries three implementations — OpenAI, Anthropic, and any OpenAI-compatible endpoint (vLLM, Ollama, a gateway), so a deployment that cannot send its cluster's interior to a third party can run a model on its own hardware. Written against `httpx` rather than vendor SDKs, matching the decision that keeps the MCP subset hand-written. An unset `LLM_PROVIDER` infers from whichever key is set, OpenAI first, so no existing install changes behaviour. Twelve mutations, all caught — every assertion is on the request that reached the transport, because a header built correctly and never sent reads identically to a working one. **Live-model evaluation now runs in CI** (`python -m evals.live`, the `live-model-evals` job): the same corpus scored against the configured provider, gated on the share of answered cases that survive grounding — which is the one number the offline corpus cannot see, because an over-strict grounding check routes everything to the fallback while staying 20/20. It refuses rather than skips: no configured model is exit 2, and a run where the calls all failed is refused rather than reported as zero rejections. Its guards are unit-tested against a local chat-completions stub, so the gate is exercised on every run whether or not a key is set. | 5d done |

## P2 — blocks scale and adoption

| # | Finding | Effort |
|---|---|---|
| F1 | ~~Mark model-authored prose as untrusted in the UI~~ **done**: `ReportDocument` styles the model-authored sections distinctly and labels them; commands render as commands. |  done |
| F6 | ~~No RBAC preflight~~ **answered, not as asked**: the preflight described needs `kubectl auth can-i`, which is a *command*, and `ResourceRequest` deliberately cannot carry one — that closed verb set is what makes a request safe to send to a customer's cluster. The reads are cheap anyway (a 403 is immediate); what was expensive was the explanation. `app/kubernetes/access.py` recognises "nothing usable, refusals dominate" from evidence status and says whose RBAC is at fault, identically through both providers. | done |
| F18 | ~~No caching; every investigation re-reads the whole cluster~~ **fixed**: a TTL-bounded cache at the `ClusterProvider` seam, keyed on `(tenant, provider, cluster, impersonated identity)` — the identity because impersonation means the same read has different correct answers per caller. Measured against a real cluster: a second investigation spawns **13 kubectl processes instead of 70** and collects in **0.16 s instead of 0.57 s**. Failures are never stored; an alert-triggered investigation always refreshes; and **evidence built from a reused read carries the age of the read, not of the run**, so a citation still means what it says. Five mutations in `scripts/mutation_check.py`, all caught. | done |
| F19 | ~~Report files are never pruned~~ **fixed**: `ReportStore.prune()` deletes rendered artefacts past `REPORT_RETENTION_DAYS` (14) **and nulls `investigations.result` in the same transaction** — the larger copy, at 2.7 MB against a couple of hundred kilobytes, which previously survived retention so an expired investigation 404'd on `/pdf` while `GET /investigations/{id}` still served its whole contents. The history entry survives marked `expired`, because deleting it would make an investigation that happened look like one that never did. Deletes payloads on upgrade that were previously kept — see `docs/UPGRADE.md`. | done |
| F7 | ~~API version assumptions (EndpointSlice, Ingress) with no discovery~~ **fixed, and it was not the version that was wrong.** The kubeconfig path does its own discovery, so the assumption only lives in the agent — and every group version it hardcodes is GA on every supported Kubernetes release, which a discovery client would confirm at the cost of a startup dependency. The assumption lacked *evidence*, not machinery, so `verify_deployment.py` now checks all 24 entries against a live cluster's discovery document in the required CI job. What running it *did* find is worse and in the same tables: **eight of the deep-investigation reads named a resource the agent has no kind for** — including EndpointSlice and Ingress, `configmap` singular against a plural key, and StorageClass, VolumeAttachment, ResourceQuota, LimitRange and ServiceAccount entirely. Every one degraded silently, so an agent-reached cluster produced a shallower investigation than the same cluster read locally and nothing compared the two. Fixed in all three places it lived (`_KINDS`, `kinds.go`, and the ClusterRole the enrolment manifest ships), and pinned by `tests/test_provider_parity.py`, which derives the required set by *running* every collector rather than reading a list. Six mutations, all caught. | done |
| — | ~~README documents a roadmap as if implemented; `docs/` is orphaned.~~ **done.** | done |
| F21 | ~~M8a's routing and its refusal are both inert on a worker that does not itself run a gateway~~ **fixed.** The fleet presence index and the enrolment store were installed inside the `agent_gateway_enabled` branch of `app/state.py`, so on such a worker `get_agent_presence()` was `None` and the enrolment store fell back to an empty local file: `agent_affinity` returned the shared queue and `select_provider` went straight to `LocalKubectlProvider`, reading a local context that merely shares the cluster's name and has no tenant — the exact cross-tenant answer the refusal exists to prevent. The revocation refusal was inert for the same reason. Both now install with the **state backend** (`install_fleet_index`), because neither needs grpc: presence is JSON in Redis, enrolment records are rows. Only the *registry* lookup, which does need grpc, stays behind the gateway flag — in both callers. **The one thing the fix had to not become is a different outage**: `_agent_was_revoked` refuses when it cannot read the store, so moving it off the gateway flag put it on the single-process getting-started path, where an unreadable `AGENT_IDENTITY_DIR` would have failed every investigation; it is gated on there being somewhere an agent could exist at all. Three mutations in `scripts/mutation_check.py`, all caught — and **verified against a live deployment by reproducing the original misconfiguration**: two workers, one gateway between them, a real Go agent attached to the worker that has it, and six investigations submitted to the worker that does not. Fixed: **6/6 refused**, each naming `worker-gw`. F21 reverted into the same harness: **6/6 answered `provider=kubeconfig`** while the agent was attached elsewhere, with a second symptom visible in `GET /agents` — the worker attribution came back empty, because presence had never been installed. | done |
| F22 | ~~A kubectl read forked from a gateway-running worker loses its own error message~~ **fixed, and the suggested fix was the wrong one.** "Give the subprocess its own stderr pipe rather than an inherited fd" was already the case — `capture_output=True` has always given it a pipe, and gRPC's child handler writes to *that*, between fork and exec. The other suggestion was right: `GRPC_ENABLE_FORK_SUPPORT=0`, and nothing here uses gRPC in a forked child, so the handlers protect nothing. **The catch is timing.** The variable is read when gRPC's core initialises, and setting it after `import grpc` does nothing — measured at 0/40 polluted before the import, 40/40 after it or after a channel exists. So it is set in `app/__init__`, the only module guaranteed to run before any `app.*` module including `app/gateway/`, and an import added above it silently makes the fix inert. That is what `tests/test_forked_reads.py` catches: it forks a real subprocess out of a process holding a real gRPC server and reads the stderr, rather than asserting the variable is set — which would pass with the fix inert. It carries its own control (the defect must reproduce without the fix, or both arms are clean and the check is vacuous). Two mutations, both caught, including the import-order one. **Two corrections to the finding, and the second is the larger one.** The reproduction returns **rc=0** with polluted stderr, so the pollution is what is certain; the non-zero exits the soak saw were most likely genuine kubectl failures whose message the noise displaced. And it **does not reproduce on Linux** — `ev_poll_posix` is the poll-based engine macOS uses, and the identical script against the identical gRPC gives 40/40 polluted on darwin and 0/40 in a `python:3.12-slim` container with or without the fix. The soak ran on the development machine, so on current evidence this never affected a shipped deployment: a finding measured on a laptop was attributed to the platform. The fix is kept because it costs one line and makes local runs match the containers, but it is a development-environment defect and is recorded as one. | done |
| ~~F22~~ | *(original finding)* **A kubectl read forked from a gateway-running worker loses its own error message.** gRPC installs fork handlers; a worker that runs an agent gateway *and* falls back to `LocalKubectlProvider` forks kubectl out of a process holding gRPC channels, and gRPC writes to the inherited stderr. `kubectl logs` then exits non-zero with its stderr reading `ev_poll_posix.cc:593 FD from fork parent still in poll list` instead of whatever kubectl was trying to say. The read is correctly recorded as *failed* evidence, so nothing is misreported — the cost is diagnosability, and it is the same shape as the agent-path `unknown` that `detailFor` exists to fix. Measured at **3 of roughly 23,000 reads** over an hour, all inside a single kubeconfig-fallback investigation. Likely fixes: give the subprocess its own stderr pipe rather than an inherited fd, or set `GRPC_ENABLE_FORK_SUPPORT=0` where no forked gRPC use exists. | 0.5d |
| F23 | ~~M8a's fail-open was measured at 0.09% and should have a metric~~ **fixed, and the finding understated it in one way and overstated it in another.** There *was* already an alert — `InvestigationsFallingBackToLocalKubeconfig`, firing at a 10% kubeconfig share — so "no alert on it" was wrong; what is true is that the rule is tuned for *routing being broken* (two thirds, before M8a) and 0.086% is ~116x below it. And no threshold on that rule could ever have caught this, which the finding missed: `k8sagent_cluster_access_total{provider}` records how the cluster **was reached**, and a fail-open and a correct local read are both `kubeconfig` — lowering the threshold would fire on every deployment holding a few genuinely-kubeconfig clusters. So the fail-open is now counted where it happens, in the one `except` that knows presence was unreadable (`k8sagent_agent_presence_failopen_total`, unlabelled, exported from import so a rule on it is correct from a cold start), and `AgentPresenceUnreadableEnoughToMisroute` fires above a 1% share over 6h — ~12x the measured baseline. Deliberately **not** gated on `agents_connected > 0` unlike its neighbour: the counter can only increment where a presence index exists, and a Redis outage bad enough to drop every agent would silence the gate exactly when it matters. Pinned with a control — a readable index holding nothing must *not* count — because a recorder called unconditionally satisfies the positive test while making the metric meaningless. One mutation, caught. | done |
| ~~F23~~ | *(original finding)* **M8a's fail-open was measured at 0.09% and should have a metric.** `_fleet_holder` returns nothing when the presence index cannot be read, deliberately: refusing every investigation on a Redis hiccup turns a degraded dependency into an outage. Presence carries a 45s TTL refreshed by a 15s heartbeat, and over a one-hour soak **1 investigation in 1,168 fell back to the local kubeconfig** with no refusal. The behaviour is correct and `cluster_access` reported it honestly — but on a real fleet that fallback is the same-named-cluster answer M8a exists to prevent, and there is currently no *alert* on it. `k8sagent_cluster_access_total{provider}` already carries the data; what is missing is a burn-rate rule in `deploy/alerts/` firing when the kubeconfig share rises on a fleet that should be all-agent. | 0.5d |

## Open — found after the review

Not from the 2026-07-26 review. Found by running the platform, and recorded
here because the sections above are all closed and a finding with nowhere to
live is a finding that gets lost.

| # | Finding | Effort |
|---|---|---|
| F24 | **A pod with more than one container has no logs at all on the agent path.** `PodLogsCollector` and `PodPreviousLogsCollector` both send `all_containers`, which kubectl expands *client-side* — it reads the pod, fetches each container's log, and concatenates. The agent has no such expansion: `resolveLogs` reads `container`, `tail` and `previous` and nothing else, so it issues one read with no container named, and the API server answers a multi-container pod with `BadRequest: a container name must be specified for pod X, choose one of: [...]`. The whole log read fails. Verified against a live cluster on a two-container pod: `kubectl logs --all-containers=true` returns both, the equivalent raw read returns the error. **Sidecars are the common case** — a service mesh, a log shipper, a secrets agent — so this is not an edge: on those workloads an agent-reached cluster loses the single most useful evidence a crash has, while the same cluster read through a kubeconfig keeps it. It is silent in the usual way: the collector records a failed evidence record, the investigation succeeds with a gap. Found while fixing the `previous` parameter, by asking the prior question the new parity check now asks on every run — is the key the platform sends one the agent reads at all. **Not fixed here because it is a shape change, not a bug fix**: the agent resolves one spec to one `Read` and the collector performs one request, and expanding a log read into a pod read plus N container reads changes that contract in the security-critical `policy` package. It should be its own change, with its own Go tests and a differential run. Pinned meanwhile in `IGNORED_PARAMETERS` in `tests/test_provider_parity.py`, which fails if the entry is removed or if the agent starts reading the key, so it cannot be quietly forgotten either way. | 1d |

## P3 — quality

~~`App.tsx` still ~1,050 lines~~ **done: 1,053 → 98**, a verified pure move —
every extracted function byte-identical to its original, and the built bundle
unchanged. `InvestigationPage` and `HistoryTable` now live in `src/routes/`
rather than being imported *from* `App.tsx` by the routes that render them,
which had the dependency pointing backwards; the remediation builders moved to
`src/lib/remediation.ts` and **gained their first tests** — reaching them used
to mean rendering a panel and reading a `<pre>`, which is why 1,000 lines of
console had none. Two dead components went with it (`MultiClusterPanel`,
`investigationEvidence`), unreferenced anywhere in `src/`. ·
~~`FixRecommendationEngine` vestigial~~ **the note was wrong, and this is the third one** (after `app/kubernetes/inspector.py` and "no frontend component tests"). It is live on `RootCauseAnalyzer._fallback()` — the path taken whenever there is no `OPENAI_API_KEY`, the model call fails, or grounding rejects the answer — and `prevention`, `next_steps` and `kubectl_commands` come from **nowhere else** on it. Verified by running rather than reading: a fallback diagnosis's `prevention` and `next_steps` are byte-identical to `FixRecommendationEngine.recommend()`'s. Only `fix` is usually superseded, by the hypothesis's more specific `remediation_hint`, which is presumably what the note saw. Deleting it on the strength of that word would have stripped the no-API-key diagnosis of its commands, prevention and next steps — the configuration the README calls fully functional ·
~~`history_service.py` ~900 lines doing three jobs~~ **done** · kubectl subprocess-per-call
(**reduced, not removed**: F18 stops a *repeat* investigation paying for it —
70 processes to 13 — but a cold one still spawns one per read) ·
~~no frontend component tests~~ **done**: 19 test files, 256 tests, including `panels.test.tsx` (the three invariants rendering can break), `shell.test.tsx`, `SignIn.test.tsx`, `ReportDocument.test.tsx` and per-route suites · no runtime plugin discovery (entry points).

---

## Testing gaps

Present: 1,350 backend + 225 frontend tests covering unit, integration, API,
fault-injection, safety-property, contract, and opt-in live-transport — plus
backend tests that run only against real Postgres and Redis
(`K8S_AGENT_INTEGRATION=1`, and a dedicated CI job). **Reusable live-cluster
fault fixtures** now ship at `docs/qa/audit-faults.yaml` (nine faults plus a
healthy control) and `docs/qa/observability-faults.yaml`, and several test
fixtures are **captured from real clusters and backends** rather than
hand-written — which is what caught the defects hand-written ones could not.

**A required CI job now stands the platform up** on kind with ingress-nginx,
metrics-server, a prometheus-operator Prometheus and out-of-band Postgres and
Redis **and a real Go agent enrolled over mTLS**, installs the chart, and makes
45 assertions against the live deployment
(`scripts/integration_verify.sh`, `docs/INTEGRATION_VERIFICATION.md`). That
closes the gap every tier from §16 to §21 was actually found through: nothing in
CI had ever *run* the system against a real dependency, so a defect that needs a
second product to disagree with us — Prometheus's parser, nginx's buffering, the
kubelet's probe path — had no way to fail a build. Mutation-tested by reverting
`2f60f76` into a rebuilt image: 27/4 and exit 1 on the mutant, 32/0 restored.

**`K8S_AGENT_CLUSTER_INTEGRATION` is set by nothing** — not CI, not
`scripts/integration_verify.sh`. The differential agent suite (36 tests,
including M4's stated exit criterion that an agent-collected investigation
produces the same evidence as the same read performed locally, and the two
certificate-renewal tests that run the real Go binary against a real gateway)
therefore runs only when someone remembers. Running it found a shipped defect
within minutes: the agent mapped every 404 to `EMPTY`, a *usable* status, so an
absent metrics-server read as an idle cluster and raised the confidence of a
diagnosis that had seen less. Fixed, mutation-tested three ways, and pinned by
a Go test plus a tripwire in `tests/test_metrics_parity.py`. **The suite now
runs in the `integration-verify` job**, which builds the agent binary on the
host and fails unless most of the suite actually ran — a fully-skipped pytest
run exits 0, so the count is checked rather than the exit status. Wiring it in
also closed a hole in the suite itself: the agent binary has no `--context` and
followed the caller's *current-context*, so the differential comparison rested
on ambient kubectl state. The kubeconfig is now pinned, mutation-tested with a
decoy current-context (23 of 36 fail without it).

Renewal, specifically, is *not* the gap it was thought to be: it is covered by
`TestRenewalHappensAtTwoThirdsOfLife` (Go, arbitrary clock) and
`test_the_agent_renews_itself_without_dropping_the_stream` (real binary, real
gateway, 30-second certificate, asserting the **same session object** still
serves reads afterwards). Both pass. What blocked moving it into
`integration_verify.sh` was that `AGENT_CERT_TTL_HOURS` was an **integer in
hours**, so the shortest deployable certificate was one hour and its renewal
point forty minutes away — the harness sidestepped it by constructing
`AgentIdentityService(leaf_lifetime=timedelta(seconds=30))` directly, which
proves the mechanism but not that a deployment can be configured into it. **The
setting is now a float** (`AGENT_CERT_TTL_HOURS=0.025` is ninety seconds,
renewing at sixty; existing integer values parse unchanged), asserted on the
validity of a certificate `build_identity_service()` actually issues rather than
on the setting. So a CI check is now a matter of standing the deployment up with
that value and watching the serial change while the stream stays open — which
is work, but no longer blocked.

Missing: envtest / multi-version cluster fixtures · snapshot tests for the PDF
and Markdown renderers · load tests inside the suite (throughput and chaos are
opt-in scripts, not tests) · **automated** mutation testing — every invariant
added since the audit was mutation-tested by hand, which does not survive
inattention (**partly closed**: `scripts/mutation_check.py` re-runs six of
them in CI, pairing a defect that shipped with the test that must object to it
— what remains uncovered is mutations needing a live cluster, which the
integration job's own mutation record covers by hand) · **agent certificate
renewal**, the one part of the identity
lifecycle the job does not reach (it enrols, connects and revokes, but nothing
runs long enough for renewal at 2/3 of certificate life) · cross-host
scale-out, which needs workers on separate machines.

---

## What the review got right about the existing design

Calibration matters as much as criticism. These held up under scrutiny and
should not be redesigned:

- Evidence spine with deterministic ids and status-as-data
- Fault-isolated collector DAG with budgets
- Read-only enforcement by construction, including the mixed-verb `rollout` case
- Signal → hypothesis separation with declarative rules
- Playbook-as-planner (inherits isolation, redaction, concurrency for free)
- Deterministic remediation with stated risk, rollback, and required RBAC
- Degradation as citable data rather than silence
