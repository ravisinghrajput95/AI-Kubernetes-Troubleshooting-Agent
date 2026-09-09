# Changelog

Notable changes, newest first. This project follows [semantic
versioning](https://semver.org/); while the major version is `0`, a minor bump
may carry a breaking change and each one says so under **Breaking**.

Entries record *why* a change was made and, where it matters, what it cost —
which is the same standard the rest of this repository's documentation is held
to. A change that fixed a defect names the defect.

## [Unreleased]

### Fixed

- **Three diagnostic defects, found by a corpus of failures written before
  reading the rules.** The shipped corpus is 20 investigations authored by the
  same person who wrote the signal and hypothesis rules, scoring 100% — a
  number that cannot fail. A held-out set was written first, then induced in a
  real cluster so the evidence came from the platform's own collectors: 14
  faults, one namespace each. **10/14 exact on the first run.**

  *A failed liveness probe was diagnosed as an out-of-memory kill.*
  `deep_signal_rules.py` read `exit_code == 137 or reason == "OOMKilled"`, and
  137 is 128 + SIGKILL — which a liveness kill also produces. The platform
  reported "Container terminated for exceeding its memory limit" for a
  container whose spec carried `resources: {}`, with `container.no_memory_limit`
  and `event.probe_failure` firing in the same investigation. The OOM signal
  now requires the termination *reason*; a bare SIGKILL gets its own signal,
  because a container that was killed and one that exited are different
  findings. The existing test was named `test_exit_code_137_confirms_an_oom_kill`
  while its fixture always supplied the reason as well, so it passed either way.

  *Contradicting evidence could not change any answer.* A hypothesis takes the
  severity of its triggering signal and severity was the primary sort key, so
  `REFUTE_PENALTY` moved `confidence` and nothing else whenever the refuted
  hypothesis had the more severe trigger — the normal case, since a symptom is
  more alarming than the marker of its cause. Refutation now sorts first.

  *And severity outranking confidence preferred symptoms to causes.* With
  refutation fixed the liveness case moved from one wrong answer to another:
  `rollout.stalled` (high, 70) over `probe.failing` (medium, 75). Confidence
  now outranks severity; severity still decides ties. **This changes what every
  investigation reports as its root cause**, and it was measured on both
  corpora before being made — 20/20 golden, 11/11 grounding, whole suite green.

  Fixing the ranking then exposed a fourth, pre-existing: `storage.claim_blocking_pod`
  listed `EVENT_SCHEDULING_FAILURE` as *refuting*, and a pod mounting an
  unbound claim is unschedulable — the scheduler says `pod has unbound
  immediate PersistentVolumeClaims`. The signal fired because the claim was
  blocking the pod and argued against the hypothesis saying so. Inert while
  severity dominated; visible the moment refutation mattered.

  **12/14 after, from 10/14.** Four mutations added to `scripts/mutation_check.py`,
  one of which survived its first run and needed the test strengthening.

  Still open and not fixed: a ResourceQuota that blocks pod *creation* produces
  no pod, so the pod-centric playbooks never collect `deep.quotas` and
  `scheduling.quota_exhausted` never fires. That is a design question about
  where the pipeline starts, not a rule to correct.

- **The collection cache never said how much it was holding.**
  `CollectionCache.stats()` has carried bytes, entries and evictions since F18
  and no caller read them — not the investigation payload, which reports what
  *that* investigation took from the cache, and not the soak, which records the
  hit rate. So the question a resident-memory trend raises — is this the cache
  filling toward its 64 MB bound, or a leak? — had no answer but inference from
  RSS, and the envelope had to say an hour cannot separate them. **An eviction
  count still at zero says the bound has never bound**, and the cache knew that
  the whole time.

  Three series, sampled once per collection wave rather than per read:
  `k8sagent_collection_cache_bytes`, `_entries` and `_evictions_total`. All
  unlabelled — they are properties of the worker, and the rule that no series
  carries a cluster, tenant, namespace, user or investigation id is untouched.
  It also answers a question an operator could not previously ask at all:
  whether `COLLECTION_CACHE_MAX_BYTES` is anywhere near right for their fleet.

  **The recorder's first version could raise into the collection wave that
  called it** — it coerced `stats["evictions"]` to `int` outside `_safe`, so a
  probe handed `None` propagated, which is the one thing this module forbids.
  Its own test caught that. The obvious repair then made the guard untestable:
  with `or 0` absorbing None inside the recorder, removing the `_safe` wrapper
  passed every test, and the mutation survived until the test was given inputs
  only the wrapper can absorb. Three mutations now fail, one in
  `scripts/mutation_check.py`.

- **A published claim that impersonation costs ~10% of p50 is retracted.** It
  came from one hour either side of the change — 0.41s without, 0.45s with. A
  fourth hour in the *same* impersonating configuration returned **0.52s**, so
  two runs differing in nothing disagree by 0.07s while the difference
  attributed to the change was 0.04s. Run-to-run variance is larger than the
  effect. Recorded in `docs/PERFORMANCE_ENVELOPE.md` rather than quietly
  edited, because it is the error the throughput figure already made twice.

  What three hours do support is that throughput is unchanged at 19.4/min and
  p95 sits at 0.62–0.68s. The fourth hour was also the first undisturbed one,
  giving the first trustworthy memory trend with impersonation on: +2.5 and
  +0.6 MB/h, monotonic, 14.2 MB and 6.4 MB of total movement against a 64 MB
  cache ceiling — **which an hour cannot distinguish from a slow leak**, and
  the envelope now says so.

- **The soak credited a worker with memory growth for ending 41 MB lower.** It
  publishes resident memory as start / peak / end plus a trend over the second
  half, and `docs/PERFORMANCE_ENVELOPE.md` quotes those trends as evidence of
  no leak. A 60-minute run reported `worker-2 start 118.5 MB, end 77.2 MB,
  trend(2nd half) +8.4 MB/h`. Both numbers were computed correctly; together
  they describe a run that did not happen.

  Both workers had stepped down together at minute 15 — worker-2 as low as
  **34.9 MB against a 123.8 MB peak** — wandered for eight minutes and settled
  on a new baseline. Two independent processes do not release memory at the
  same instant for a reason of their own, so that is the host reclaiming pages,
  and the refault that follows reads as growth to anything fitting a slope. The
  trough appeared nowhere in the report, because only the peak was printed.

  `ps rss` is not the noisy part, which was checked rather than assumed:
  sampled every two seconds for a minute under the same workload it did not
  move by a single kilobyte.

  So the report prints the **low** as well as the peak, and a run with a
  simultaneous fall gets **no trend at all**. Fitting one after the last
  disturbance was the obvious fix and is worse — the refault climbs back toward
  the old baseline for the rest of the run, taking worker-2 from +8.4 to
  **+13.1 MB/h** on a better-founded window. There is no window that makes such
  a run answer the question, so it says so: `trend n/a (host disturbance)`,
  naming the minutes it fell. Memory is the one claim that hour cannot make;
  every other number in it stands.

  Found by running the first 60-minute soak with the agent impersonating. Five
  hermetic tests driven by the real series, three mutations watched fail —
  including the over-strict one, where a single worker falling alone would be
  called a host event and a bounded cache evicting would be discarded as noise.

## [0.2.3] — 2026-09-08

Six changes, no breaking change. **Every one is in the checking apparatus
rather than the platform**, which is the whole character of this release: one
defect in a shipped guarantee, and five in the things that were supposed to
notice.

The shipped defect is F27 — an agent-collected evidence record did not say
whose RBAC produced it. The reads *were* impersonated on both paths; the record
was not, so the two transports disagreed about whose permissions produced the
same fact, and an audit of an agent-served investigation could not answer it.

The rest are harnesses that were wrong about themselves. The differential
comparison bracketed one provider and so could not see kubectl's own
nondeterminism. The soak refused to start without a caller RBAC grant and then
routed 100% of collection through an agent that ignored it. The console's
overflow check passed identically whether or not there was anything long enough
to overflow. The SSE check punished the platform for being fast and failed a
required job by three tenths of a percentage point. And `--all-containers` was
recorded as having a stable container order it does not have.

Two of them were caught by measuring before building something, and one by
checking a claim I had just written and found to be an over-claim. None came
from a test suite: 1,584 backend and 256 frontend tests are green with all six
present.

### Fixed

- **The SSE incremental-delivery check punished the platform for being fast,
  and failed the required CI job by three tenths of a percentage point.** It
  compared the client's whole arrival span against the platform's whole
  emission span. But the investigation is submitted *before* the stream is
  opened, and `subscribe()` replays the backlog before going live — so every
  event emitted before the connection existed arrives in one burst, by design.
  That shortens the arrival span and leaves the emission span untouched, so the
  more of the investigation that finishes before the client connects, the more
  incremental delivery reads as a buffered blob.

  Two consecutive CI runs of the same code: `0.470s / 0.830s = 57%` passed,
  `0.379s / 0.760s = 49.87%` failed against a 50% threshold. The constant's own
  comment asserted that "a machine being fast or slow moves both sides
  together", which is the false premise the whole check rested on; it is
  corrected in place with both measurements beside it.

  `_live_portion` now drops the backlog so the two spans describe the same
  frames, aligning the server and client clocks on the last frame — which is
  also what makes the all-backlog case fall out as a refusal instead of needing
  its own branch. That refusal replaces a failure that used to arrive with the
  wrong message: a run where nothing was delivered live cannot tell streamed
  from buffered, and now says so.

  `verify_deployment.py` had no unit tests at all, which is how a required job
  came to rest on an unmeasured constant. It has eight now, driven by both real
  CI runs' numbers, plus three mutations watched fail — not excluding the
  backlog, aligning on the first frame, and widening the slack enough to
  swallow the live tail. Verified live: 49 checks pass, 26 of 29 frames live,
  100% against the 50% threshold.

- **`kubectl logs --all-containers` has no stable container order, and this
  repository recorded that it did.** F24's account said the agent reproduces
  "kubectl's container order, init containers first, established against a live
  cluster rather than assumed". Measured properly: kubectl issues one request
  per container concurrently and writes each as it arrives, so an unchanging
  three-container pod came back **22 init-first, 7 sidecar-first and 1
  app-first over 30 reads**, and 17/2/1 over 20 once it was crash-looping. The
  original claim was one reading of the common case — established against a
  live cluster, and wrong anyway.

  The agent's behaviour does not change: init, regular, ephemeral, each in spec
  order, is deterministic, and determinism is the right side to err on when the
  evidence spine wants a payload reproducible from the same cluster state. What
  changes is the claim, in the agent's own comments, its test, `CLAUDE.md` and
  the F24 backlog entry — and the consequence is now stated instead of denied:
  the two providers can order a multi-container log differently, and that is
  kubectl's nondeterminism rather than a divergence.

- **An agent-collected evidence record did not say whose RBAC produced it.**
  `equivalent_command` is the evidence spine's answer to "how was this fact
  obtained" — every record carries the invocation that would produce the same
  bytes. The kubeconfig path records `--as <caller>`, because impersonation is
  how F13's "the platform cannot see more than you can" is delivered. The agent
  path applies the same identity as `Impersonate-User` headers and recorded
  **no identity at all**.

  Three costs, none of them wrong data. The same read through the two
  transports disagreed about whose RBAC produced it, so the two investigations
  were not comparable on the one field a differential harness uses to find
  behavioural divergence. A human running the recorded command read as
  themselves, which on a cluster-admin kubeconfig returns *more* than the
  investigation saw. And an audit of an agent-served investigation could not
  answer who a fact was collected for.

  Fixed in the agent, gated on what actually happened rather than on what was
  asked: `impersonating()` is now one condition asked by both the headers and
  the record, because two copies drift and the drift is silent in both
  directions. Deliberately **not** rendered on the platform side, which knows
  the actor it sent but cannot know whether the agent applied it — an agent
  enrolled before impersonation shipped discards the actor, and a command
  claiming `--as` there would be the same false record pointing the other way.

  Found by `scripts/provider_diff.py` against a live 57-record namespace:
  status clean, content clean, **33 command differences of exactly one shape**.
  After the fix the same comparison reports **0 differences across all three
  nets**, and the control still reports 33 when the agent is not impersonating
  — which is a *true* divergence, because the two paths then really do read as
  two different identities. Four Go mutations watched fail, including the one
  that would render the identity unconditionally.

### Changed

- **`console_check.mjs` reports when its overflow check had nothing to
  detect.** The check finds a page that scrolls sideways, and the defect it was
  written for — a grid item left at `min-width: auto` whose content is
  `truncate` — only exists when some text is long enough to force it. That is
  already recorded as a hazard: reverting `min-w-0` once reported clean because
  a fresh investigation had replaced the long health message. The output looked
  the same either way, so the hazard was advice rather than a signal.

  Each run now reports the widest run of text that cannot wrap. If that is
  narrower than the viewport, no single item could have scrolled the page.
  Verified across the full 2×2 rather than the diagonal: with the defect
  present and a 241-character root cause the page scrolls to 2,010px and the
  culprit is named; with the defect present and an 89-character one it passes
  clean at 618px and prints `NO TRIGGER` — the run that is otherwise
  indistinguishable from a working check. Reported and not enforced, because a
  console with no long content is a legitimate state.

- **The differential comparison brackets both providers, not just the agent.**
  F26 reads one provider either side of the other so a value that moved was the
  cluster moving. That sees the cluster; it cannot see a provider that is
  nondeterministic *in itself*, because that provider is read once — and
  kubectl's concurrent log fetch is exactly that. Churn is now what moved
  between either provider's own two reads, and the two brackets span the whole
  window.

  **It is not reachable from the suite today, which was checked rather than
  assumed.** No projection in `TestEveryCollectorAgrees` compares log text:
  `logs` is a named volatile field and the fan-out projection takes only entry
  names. Against a deliberately racy crash-looping sidecar pod the one-bracket
  version passed twice. The fourth read costs about 4s across 40 tests and buys
  the guarantee that adding such a value to a projection later cannot quietly
  reintroduce it — the exclusion is currently the only thing holding it.

- **The soak's impersonation guard was inert in the configuration it runs in.**
  `grant_caller_rbac` exists so the caller's own RBAC is on the path — without
  it every read is FORBIDDEN and an hour measures a locked door — and the run
  refuses to start without it. But `--agent` routes 100% of collection through
  the agent, and the soak never passed `--impersonate`, so those reads ran as
  the agent's own broad ServiceAccount and the grant was protecting a path the
  headline run does not take. F13's guarantee had never been exercised by a
  soak.

  Proved both ways with the grant mutated away: the pre-fix agent publishes
  **40/40 usable, exit 0**; the impersonating agent gives **0/40 usable,
  REFUSED, exit 1**. A healthy 3-minute run with the flag on still gives 60/60
  usable, all 60 through the agent.

## [0.2.2] — 2026-09-06

Four defects, no breaking change. None was found by a test suite — 1,574
backend and 256 frontend tests stayed green with all four present — and **two
of them were defects in the checking apparatus itself**, which is the part
worth reading twice.

The differential suite that exists to catch a provider divergence could not
tell one from a cluster that moved, and said "differs" for both; it failed the
required CI job on a Deployment replacing a pod between two reads. The
console's progress panel claimed to be "streaming live from the backend"
while the `polling` tag beside it said otherwise — and the tag was right,
because the SSE transport had never delivered a single event to a browser in
the project's history. The fallback worked, so the only symptom was an
indicator firing on the healthy path, which teaches you to ignore it.

Each was found by running something. The SSE defect took opening Chrome and
counting frames — the hook's own tests passed because their fake called
`onmessage` directly, modelling a wire the server does not produce, while
`docs/INVESTIGATION_API.md` had documented the correct usage all along. The
churn defect took a required CI job going red. And the fourth, a race that
only appears under the shipped topology, took the soak: two workers starting
together each generated their own certificate authority and both wrote,
leaving an agent handed that file unable to verify the gateway it dialled.
It is intermittent, which is how it survived earlier soaks.

### Fixed

- **The differential suite could not tell a provider that disagreed from a
  cluster that moved, and reported both in the words of the first.**
  `TestEveryCollectorAgrees` runs the baseline collector graph twice against one
  cluster — once through an agent, once through a kubeconfig — and compares the
  values each derived. Comparing values is what makes it worth running; it is
  how the agent's `statusFor` was caught mapping every 404 to `EMPTY`. It is
  also why two reads seconds apart see two different clusters.

  That reached CI. The required `integration-verify` job failed on `da5de44`
  with `k8s.deployments.unhealthy_deployments differs` — `unavailable_replicas`
  1 against 0 — while all 48 of its deployment checks passed beside it. The
  platform's own Deployment was replacing a pod between the two reads. Named
  volatile fields could never have covered this: `unhealthy_deployments` is a
  finding, not a clock, and what was wrong with it was that it happened to be
  moving right then.

  Each comparison is now **bracketed** — agent, kubeconfig, agent again — and a
  value that moved between the two reads of one provider was moving in the
  cluster rather than disagreeing between providers. One bracket catches every
  *one-time* change, which is the arithmetic worth stating: with samples at
  t1 < t2 < t3 and a single change at T, either T falls inside [t1, t3] and the
  bracketing reads disagree, or all three agree. Only a value that changes and
  changes back inside that window survives — the same residue
  `scripts/provider_diff.py` handles by running twice and asking whether the
  difference swaps sides.

  **Excluding churn must not become excluding everything**, which is the quieter
  half and the one that would have shipped unnoticed: discount every difference
  and the suite passes forever while proving nothing. So the exclusion is per
  *value*, not per section — a moving `phase` on one pod does not excuse a
  divergence on the pod beside it — and `Comparison.refusal()` refuses a
  comparison the cluster churned away rather than reporting it as agreement.
  `test_the_baseline_graph_produces_the_same_evidence` also now requires both
  paths to have collected something usable, because every other assertion in
  that class is satisfied by two providers that failed identically, which is
  what a dead Docker daemon produces.

  The discriminator lives in `tests/differential.py` and is tested by
  `tests/test_differential_control.py`, which is hermetic — the suite that uses
  it skips unless `K8S_AGENT_CLUSTER_INTEGRATION=1` and a cluster are present,
  so a regression there would be invisible in the default suite and in most of
  CI. Two entries in `scripts/mutation_check.py` hold both directions.

  Verified by reproducing the trigger rather than by reasoning about it: a
  deployment flapping its replica count, and the same suite run three ways
  against it. Without the fix, **the CI failure reproduces exactly** — the same
  test, the same kind. With it, 12/12 pass. With F25 reverted on the agent path
  to inject a genuine divergence *while the churn continues*, **5 tests fail**,
  so the exclusion did not disarm the suite. The refusal guard did not fire at
  4↔30 replicas flapping and is proved by unit test only.

- **Two workers starting together each generated their own CA.**
  `CertificateAuthority.load_or_create` was check-then-act, and N replicas boot
  together by design. Both saw no CA, both generated a *different* one, and both
  wrote — so each gateway served a certificate signed by its own in-memory key
  while the file held whichever finished last, and an agent given that file
  could not verify the gateway it dialled (`x509: certificate signed by unknown
  authority`). Key was written before certificate, so an interleave could also
  leave one process's key beside the other's certificate: a pair matching
  nothing that every later start would load happily.

  `O_CREAT | O_EXCL` on the key is now the claim; losers wait for the
  certificate and load it, with a timeout so a winner that died mid-write cannot
  hang the fleet. The "exactly one half of the CA" check is narrowed to a
  certificate with no key — the mirror case is what a concurrent winner looks
  like mid-write, and rejecting it turned the race into a startup failure for
  every worker but one.

  Found by the soak, which runs two workers because that is the shipped
  topology: the agent never checked in, and both worker logs carried "Generated
  a DEVELOPMENT certificate authority" at the same timestamp. Intermittent,
  which is how it survived earlier soaks. The Helm path is unaffected — the
  chart mints a CA secret — so this bites local multi-worker runs and the soak.

- **The console's SSE transport never worked in a browser, and the fallback hid
  it completely.** The server names every frame (`event: progress`), and per the
  HTML spec `onmessage` fires only for the *default, unnamed* type — a named
  event reaches `addEventListener("<name>", …)` or nothing. `useInvestigationJob`
  registered `onmessage` alone, so the stream opened, delivered **zero** events,
  errored after ~400ms, and fell back to polling. Every investigation the console
  has ever displayed was polled. Measured in Chrome before and after:
  `messages: 0, error at 397ms` against `queued 1, started 1, progress 64,
  completed 1, error: null`.

  `docs/INVESTIGATION_API.md` documented the correct usage all along. The hook's
  tests passed because `FakeEventSource.emit()` called `onmessage` directly,
  modelling a wire the server does not produce; the fake now dispatches on
  `payload.type` as a browser does, and six of those tests fail with the defect
  present where all 256 passed before. `STREAM_EVENT_TYPES` is now held against
  the documented list, because a type added on the server and not in the console
  goes back to being dropped in silence.

- **The progress panel contradicted its own transport tag.** The subtitle read
  "Streaming live from the backend" whenever running, regardless of transport,
  while the `polling` tag beside it said otherwise — and that tag is the only
  place the degraded path is visible. It also appeared on *healthy* runs, because
  the spurious fallback above tripped it. The subtitle now names the transport
  actually carrying progress, and with SSE working the tag no longer appears.

## [0.2.1] — 2026-09-05

Five defects, and the thing they have in common is how they were found: **every
one by running the platform, none by its tests**, which stayed green with all
five present — 1,539 backend and 256 frontend.

Three came from exercising surfaces that had never been run live at all. MCP
announced a version this project has never released. The console scrolled
sideways on three of its six routes, because jsdom has no layout engine and a
test that queries by role passes against a page that looks wrong. And a
remediation plan asserted a ConfigMap that no evidence had identified, then
generated an unappliable manifest for it — found by rendering a report to PDF
and reading it, which is where an operator meets it.

The other two came from comparing the two cluster providers against each other,
which has now produced four defects across three sessions. F25 turned up after
the *status* diff came back clean across four scopes: the next thing to compare
was what each provider recorded it had run, and the kubeconfig reads carried a
paging flag while the agent's carried no limit at all.

Each fix ships with the check that would have caught it — including two new
harnesses, `scripts/console_check.mjs` and `scripts/provider_diff.py`, both of
which refuse to report a clean run they cannot vouch for.

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
