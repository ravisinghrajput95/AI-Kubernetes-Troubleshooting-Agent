# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Backend (run from `backend/`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend (run from `frontend/`):

```bash
npm install
npm run dev      # Vite dev server on :3000
npm run build    # tsc -b && vite build — the only type-check gate in the repo
```

Frontend tests (from `frontend/`):

```bash
npm test                                    # vitest run (hermetic)
npm test -- src/lib/analysis.test.ts        # single file
npm test -- -t "falls back to polling"      # single test

# Opt-in transport tests against a running backend:
VITE_API_INTEGRATION=1 react_PUBLIC_API_BASE_URL=http://127.0.0.1:8778 npm test
```

`http.integration.test.ts` is skipped unless `VITE_API_INTEGRATION` is set, so the default suite stays hermetic. It exists because the unit tests mock `fetch` — which proves the logic but not the wire, including FastAPI's `{"detail": ...}` error bodies.

Backend tests (from `backend/`):

```bash
pip install -r requirements-dev.txt
python -m pytest                       # whole suite
python -m pytest tests/test_collection_scheduler.py -k timeout   # single test
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. Tests use a fake `KubectlExecutor` subclass and never touch a real cluster.

```bash
python -m evals    # reasoning + grounding regression report
```

`evals/` is a golden corpus enforced by `tests/test_evals.py` and printed in CI. It exists because rules, prompts and grounding checks can all change without breaking a unit test while making the platform worse at reasoning. **The grounding corpus must keep cases that are expected to be *accepted*** — a corpus of only-rejections passes while an over-strict check has silently routed every investigation to the deterministic fallback. See `docs/EVALUATION.md`.

Docker: `docker compose up --build` builds both services. Two caveats — `docker-compose.yml` declares `env_file: ./backend/.env.example`, which is not in the repo (`backend/.gitignore` ignores it), and the backend image does not install `kubectl` or mount a kubeconfig, so investigations will fail inside the container. Local processes are the working path.

Lint and format (from `backend/`, config in `ruff.toml`):

```bash
ruff check .            # CI fails on any finding
ruff format --check .   # CI enforces formatting
```

`.github/workflows/ci.yml` runs lint, format, backend tests on Python 3.12/3.13, frontend build and tests, a dependency audit, a secret scan, and both Docker builds.

`requirements.txt` pins are kept current and audited — `pip-audit --strict` runs in CI and **fails the build**, because the original pins shipped known CVEs in PyJWT (which validates auth tokens) and Starlette.

**Security status:** there is no authentication on any endpoint. See `SECURITY.md` and `docs/PRODUCTION_READINESS.md` — do not deploy against a production cluster.

`PyYAML` is a runtime dependency: generated patches are applied to production clusters, so YAML is serialised properly rather than string-formatted.

## Runtime requirements

- `kubectl` must be on PATH of the backend process; every cluster read shells out to it.
- `OPENAI_API_KEY` (optional). Without it the LLM call fails cleanly and the deterministic fallback diagnosis is used — the app stays fully functional, just with `ai_generated: false`.
- Config is `pydantic-settings` in `app/core/config.py`, read from env or `backend/.env`: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-4o-mini`), `KUBECONFIG_PATH`, `KUBECTL_TIMEOUT_SECONDS`, `LLM_TIMEOUT_SECONDS`.
- `InvestigationHistoryService` writes to `Path("data")/"investigations"` — a **relative** path, so the backend must be started from `backend/` or history lands in the wrong directory.

## Architecture

`run_investigation()` (`app/services/investigation_runner.py`) is the single pipeline both APIs use, so the sync and async paths cannot drift. It runs, in order:

1. `InvestigationService.run()` (`app/services/investigation_service.py`) — drives the collector graph, then derives the investigation payload from the resulting evidence.
2. `RootCauseAnalyzer.analyze()` (`app/ai/root_cause_analyzer.py`) — the reasoning layer.
3. `InvestigationHistoryService.save()` (`app/services/history_service.py`) — persists PDF + JSON + Markdown reports.

Steps 2 and 3 block, so both are dispatched via `asyncio.to_thread` to keep the event loop free.

### Two entry points

| Endpoint | Behavior |
|---|---|
| `POST /investigate` | Runs to completion and returns the full result. Retained for backward compatibility. |
| `POST /investigations` | 202 + id, runs in the background. `GET /investigations/{id}` for state, `/events` for SSE, `/cancel` to abort. |

**The job id is the investigation id.** `history_service.save()` takes an optional `investigation_id`, so the finished report is stored under the id returned at submit and `/investigations/{id}/pdf|json|markdown|report` resolve without a second id space. `GET /investigations/{id}` falls back to the persisted report when the job has been evicted or the process restarted.

`collection_failure()` draws the line between the two failure modes: **partial** degradation (one collector down) succeeds with reduced completeness; **total** failure (zero usable evidence) fails the job, because there is nothing to reason over and reporting it as success would misrepresent it. The sync endpoint deliberately keeps its old contract — 200 with `health.status == "error"` — so the existing frontend still renders the message.

### Jobs (`app/jobs/`)

`InvestigationJobStore` is **in-process**: jobs do not survive a restart, and a multi-worker uvicorn deployment will not find a job created by another worker. Single-process is the supported topology; swapping in Redis means implementing `create/get/list/publish/subscribe` and nothing above that layer changes.

`publish()` never blocks — a subscriber that is not draining loses events rather than stalling the investigation. `subscribe()` replays the backlog before going live, so a client that connects mid-run still sees the whole timeline, and yields `None` on each `heartbeat` interval for SSE keepalives.

Progress flows through the `ProgressReporter` protocol on `CollectionContext` (default `NullProgressReporter`, so sync runs stay silent). Collectors carry a `label` used as the progress message — the generic scheduler must not gain a mapping of Kubernetes collector ids.

Two endpoints **must stay `async`**: `start_investigation_job` and `cancel_investigation`. `asyncio.create_task` and `Task.cancel()` require the event loop thread, and FastAPI runs `def` endpoints in a worker threadpool.

Testing note: `TestClient` must be used as a context manager (`with TestClient(app) as c`). Without it the event loop is torn down per request and background jobs are cancelled mid-run. `TestClient` also buffers streamed responses, so it verifies SSE framing but not progressive delivery — that needs a real server.

### Evidence layer (`app/evidence/`)

Every collected fact is an `Evidence` record with a **deterministic id** (`kind:target.key`), a `status`, and its originating command. This is the citation spine: conclusions reference evidence ids rather than copying payloads. `EvidenceStatus.usable` covers `OK` and `EMPTY`; everything else (`UNAVAILABLE`, `FORBIDDEN`, `TIMEOUT`, `NOT_APPLICABLE`, `FAILED`) records *why* a fact is missing, so a degraded backend becomes citable data instead of an exception. `EvidenceStore.coverage()` reports completeness and is the intended input for evidence-completeness confidence scoring.

### Collection (`app/collectors/`)

Collectors declare `provides` / `requires` / `optional_requires`; `CollectorRegistry.resolve()` topologically sorts them into waves. `requires` must have a registered provider (a missing one raises `CollectorGraphError` at resolve time); `optional_requires` only affects ordering when a provider exists, which is how optional backends like Prometheus stay absent without breaking the graph.

`CollectionScheduler` guarantees three things regardless of collector behavior:

- A collector that raises, hangs, or exhausts the budget degrades **only its own** evidence.
- Every declared kind lands in the store, worst case as a non-usable record explaining the gap.
- **Redaction happens here, at the collection boundary** — so reports on disk, the HTTP API, and the LLM all see the same scrubbed payload. Do not reintroduce redaction at the prompt boundary; that leaves the persistence and API paths uncovered.

The nine existing inspectors are **adapted, not rewritten** (`app/collectors/kubernetes.py`). `LegacyInspectorCollector` runs each synchronous inspector via `asyncio.to_thread` and maps its established `{"error": ...}` contract onto evidence status through `app/kubernetes/errors.py`. Everything except pod logs is independent and runs as one concurrent wave; logs form a second wave because `PodLogsCollector` declares `requires={PODS}`.

### Inspector contract (`app/kubernetes/`)

Constructor takes the shared `KubectlExecutor`; `inspect()` returns a plain dict, returning `{"error": <stderr>, ...}` on kubectl failure rather than raising. Findings-producing inspectors return a `findings` list; pods return `problematic_pods` + `pod_inventory`; deployments return `unhealthy_deployments`. Severity, health, and overview logic count exactly those keys, so **a new inspector still needs wiring into `_health_summary` and `_severity_summary`** or its findings are ignored. (Collapsing that into the evidence layer is the natural next refactor.)

All cluster access is **read-only by construction**: `KubectlExecutor.run()` calls `assert_read_only()` from `app/kubernetes/command_policy.py`, which allowlists verbs and sub-verbs. A mutating command raises `UnsafeKubectlCommand`, which the scheduler's fault boundary records as failed evidence. `executed_commands` is guarded by a lock because collectors run in worker threads.

`InvestigationService` derives the rest from the store: `metrics` (parses `kubectl top` lines), `security`, `topology`, `timeline`, plus the additive `evidence` (citation index) and `evidence_coverage` keys.

Scoping: `resource_kind` + `resource_name` narrow the pod and deployment collectors only; the rest still run namespace- or cluster-wide.

### Playbooks (`app/playbooks/`)

Investigation is **iterative**: baseline collection → analysis → playbook selection → targeted collection → re-analysis. `InvestigationOrchestrator` drives the loop and `InvestigationService._build_view` is passed in as the view builder, so the analysis engine always sees the latest evidence.

**A playbook is a planner, not an executor.** It emits targeted collectors from `app/collectors/targeted.py`; the existing scheduler runs them. That is why playbooks inherit fault isolation, redaction, concurrency, and budgets for free — do not let a playbook call `kubectl` directly.

Each hypothesis's `missing_evidence` is the collection plan: playbooks collect exactly what the deterministic layer already knew it lacked.

Two conventions in targeted collectors:

- **Structured summaries, not raw dumps.** One `get pod -o json` yields probes, exit codes, restart counts, limits, and config references; `PodSpecCollector` extracts them. Never parse `kubectl describe` text — it is not stable across versions.
- **Secret values are never read.** Secrets go through `describe` (prints key names, never values); ConfigMaps use `get -o json` but only key names are emitted. `test_secret_values_are_never_requested` asserts no command issues `get secret`.

Bounded on three axes: `max_targets` (5) per playbook, `max_rounds` (1), and the shared `CollectionBudget`. Collectors already executed are never repeated, so raising `max_rounds` converges.

Deep payloads land in `investigation["deep_evidence"]` keyed by kind (baseline kinds excluded — they are already in named sections); `AnalysisInput.deep()` reads them. `playbook_rounds` records what ran.

`CollectorRegistry.resolve(available)` is what lets round 2 depend on baseline evidence without re-registering the collectors that produced it.

### Analysis layer (`app/analysis/`)

Evidence → **signals** → **hypotheses**, all deterministic, all before any model call.

A `Signal` is an evidence-backed observation extracted by a rule in `signal_rules.py`. `evidence_ids` is mandatory — `Signal.__post_init__` raises if empty, because a signal that cannot name its provenance is a bug. Ids are deterministic (`pod.crash_loop:pod/prod/web-0`), which makes duplicate observations idempotent.

A `Hypothesis` is a candidate root cause. Rules in `hypothesis_rules.py` are **declarative data**, not branching logic: each `SignalPatternRule` states its `triggers`, `supporting`, `refuting`, and `missing_evidence`. Adding a failure mode means appending a rule to `DEFAULT_HYPOTHESIS_RULES` — no dispatcher to edit. Confidence is `base + 10/supporting type − 20/refuting type`, and `missing_evidence` drives what playbooks collect.

`signal_rules.py` runs over baseline evidence; `deep_signal_rules.py` runs over targeted evidence and produces findings baseline cannot reach (exit code 137 confirming an OOM, a referenced ConfigMap key that does not exist). Rules that could fire broadly are gated on evidence of the specific failure — `image.no_pull_secret` only fires when a container is actually failing to pull.

Both rule loops are individually fault-isolated: one broken rule is logged and skipped, not fatal.

### Remediation (`app/remediation/`)

Plans are keyed on the **hypothesis**, not on investigation heuristics, so they can name the actual container, its limits, and the owning workload. `RemediationPlanner` maps `hypothesis.id` → rule; an unmatched or failing rule degrades to a read-only *diagnostic* plan built from the hypothesis's `missing_evidence` rather than inventing a fix.

**The platform can never apply what it generates.** Mutating commands are rejected by `assert_read_only()`, and `tests/test_remediation_safety.py` asserts that for every registered rule — plus the inverse, that preconditions and verification steps *pass* the policy. Those tests are parameterised over `DEFAULT_REMEDIATION_RULES`, so a new rule is automatically held to the same guarantees.

Two correctness details that are easy to regress:

- **Target the controller, not the pod.** `PodSpecCollector` captures `ownerReferences`; the Deployment name is derived by dropping the ReplicaSet's pod-template-hash. The derivation is flagged as a caveat, never presented as fact.
- **Bare pods are not rollable.** `rollout restart/undo/status` fail against a Pod. `_rollable()` gates this; unmanaged workloads get pod-appropriate verification and a manual rollback step.

The platform deliberately refuses to generate secret values, a memory limit when no current limit was observed, and NetworkPolicy selectors — each emits a caveat saying why instead.

`remediation_risk` and `remediation_plan` keep their original shapes via `to_legacy()`/`to_legacy_plan()`; `remediation` and `patches` are additive. All are computed deterministically and overwrite model output.

`command_policy` treats `rollout` as a mixed verb: `status`/`history` are allowed, `undo`/`restart`/`pause` are not.

### AI layer (`app/ai/`)

**The model never diagnoses from raw JSON.** `PromptBuilder` sends only signals, hypotheses, scope, health, and coverage — then asks the model to *select and explain*, citing signal ids. This shrinks the prompt and, more importantly, narrows what the model can assert.

**Commands are never taken from the model.** `_normalize()` uses the deterministic command set regardless of what the model returns, and every surfaced command passes `classify_command()` — unrecognised strings are dropped, mutating ones labelled. This closes a verified injection chain: hostile pod-log text reached the prompt and produced `kubectl delete ns kube-system` as an operator-facing recommendation. The read-only executor does *not* mitigate that, because the human is the execution path. Regression tests: `tests/test_prompt_injection.py`.

`GroundingValidator` (`app/analysis/grounding.py`) enforces **citation integrity** *and* **semantic consistency**. Citation checks:

- A fabricated hypothesis id **rejects** the response.
- Fabricated signal ids are **stripped** and recorded in `grounding.rejected_citations`; if signals existed and none of the model's citations survive, the response is **rejected**.
- An empty root cause **rejects** the response.
- With no signals at all (healthy cluster), citations are not required.

A rejected response falls back to the deterministic ranking. Do not weaken this to "log a warning and use it anyway" — the tests in `tests/test_grounding.py` and the two `*_is_discarded` tests in `tests/test_root_cause_analyzer.py` exist to catch that.

Semantic checks then reject prose that misrepresents what it cites — citation integrity alone let a response cite a genuine CrashLoopBackOff while concluding "resolved, no action needed":

- **Contradiction**: reassurance language ("no action needed", "appears healthy") over CRITICAL/HIGH signals. The same wording passes on a genuinely healthy cluster, where there are no severe signals to contradict.
- **Citation relevance**: at least one cited signal must be one the selected hypothesis actually rests on. Citing real but unrelated signals explains nothing.
- **Invented resources**: `namespace/name` references appearing in no evidence. The regex is case-sensitive and excludes paths, so `512Mi/1Gi` and `/healthz` are not mistaken for resources.

These are deterministic — a second model call would add latency and cost per investigation and would itself need grounding. Matching is deliberately lenient: **an over-strict check does not fail loudly, it silently routes every investigation to the fallback**, so `TestGenuineDiagnosesStillPass` and the false-positive cases in `tests/test_semantic_grounding.py` guard the fallback rate and must not be weakened.

`fix` and `prevention` remain model-authored prose. Commands never are.

Confidence is composed, not asserted (`app/analysis/confidence.py`): evidence strength / AI confidence / evidence completeness at 0.5·0.3·0.2 on the grounded path, 0.7·0.3 (capped 95) on the deterministic one. `confidence_breakdown` exposes each component's weight and contribution.

The dual-path design still holds: `RootCauseAnalyzer._fallback()` builds a complete diagnosis from `FixRecommendationEngine` and `ConfidenceEngine`, and `_normalize()` spreads that fallback as the per-field default for the model response. **Any new diagnosis field must be produced by `_fallback()` first.** `remediation_risk` and `remediation_plan` are always computed deterministically and overwrite whatever the model said.

`LLMClient` posts to the OpenAI chat completions endpoint directly via `httpx` (no SDK), 3 attempts with linear backoff, `response_format: json_object`.

### Observability backends (`app/integrations/`, `app/collectors/observability.py`)

Prometheus and Loki are **optional**: unset `PROMETHEUS_URL`/`LOKI_URL` means the collectors record `not_applicable` evidence naming the variable to set. Clients never raise for an operational problem — they return a result carrying a status, so `empty` ("we looked, nothing there") stays distinguishable from `unavailable` ("we could not look").

**Absent backends produce no signals, including no negative ones.** Missing metrics must never read as healthy metrics.

`EvidenceStore.coverage()` excludes `not_applicable` from the completeness ratio (reporting it separately). Completeness feeds confidence, so counting an undeployed Prometheus as a gap would permanently lower confidence in diagnoses that never needed metrics.

Queries use only cAdvisor/kube-state-metrics names. **Application-level metrics are deliberately not queried** — their names are per-application, and a guessed name returns an empty result indistinguishable from a healthy one.

Both are emitted by playbooks (CrashLoop → pod metrics + historical logs; Pending → node metrics), not baseline collection. Baseline usage still comes from `kubectl top`.

### Reports (`app/reports/`)

`IncidentReportComposer` builds a structured `IncidentReport`; the PDF, Markdown and JSON writers all render **that one composition**, so the formats cannot disagree and a new section is one change rather than three. The JSON report carries the composition under its `report` key.

Sections with nothing behind them are **omitted, not padded** — same rule as the console.

`history_service.py` writes the three formats. The PDF is hand-rolled object emission (`_write_pdf_objects`, base-14 fonts, no PDF dependency), so section bodies are flattened via `ReportSection.as_lines()` and text must be pre-wrapped and non-ASCII escaped. `history.json` is capped at 25 entries; report files under `data/investigations/reports/` are keyed by UUID and not pruned. `POST /investigations/{id}/regenerate` re-renders all three from stored JSON without re-querying the cluster — so improving the composer improves historical reports too.

### Frontend

`src/App.tsx` still holds the original panels plus the `Dashboard` composition. **New work goes in `src/components/`, `src/hooks/`, and `src/lib/`** — do not grow `App.tsx` further.

Investigations run through `useInvestigationJob` (`src/hooks/`), not a React Query mutation. It posts to `/investigations`, streams progress over SSE, and **falls back to polling** when `EventSource` fails — corporate proxies commonly block it, and a stalled screen during an incident is worse than a slower one. Both paths converge on one terminal `GET /investigations/{id}` for the full result. `transport` is surfaced in the UI so a degraded path is visible.

Pure logic lives in `src/lib/analysis.ts` (grouping, filtering, severity ordering, formatting) so it is testable without rendering. Panels stay presentational.

HTTP goes through `src/services/http.ts`, a small `fetch` wrapper — **there is no axios**. It cost 16.7 KB gzipped, more than the console's entire own code, for eight JSON calls. The wrapper keeps what was actually used (base URL, 120s timeout via `AbortController`, JSON encode/decode, throw on non-2xx) and adds a typed `ApiError` carrying `kind` (`network` / `timeout` / `http`) and `status`. Do not reintroduce an HTTP client library without a reason beyond convenience.

`vite.config.ts` splits `react` and `query` into separate chunks. Total bytes are unchanged; the point is that a deploy only invalidates the ~16 KB app chunk instead of all ~88 KB. Note the `manualChunks` **function** form is required — the object form does not capture subpath imports such as `react-dom/client`, which silently leaves react-dom in the app chunk. Do not code-split the app's own panels: they are a small fraction of the bundle and each extra chunk costs a round trip.

Two properties are load-bearing and must not regress:

- **Never display evidence the backend did not report.** `ConfidenceEvidence` previously fell back to a hardcoded `["Events", "Pod Logs", …]`; panels now render an empty state instead. In a product whose premise is that nothing is asserted without evidence, placeholder content is a correctness bug.
- **Progress is real.** The old `progressSteps` array advanced on a 900ms timer with no relationship to backend work. Every row in `LiveTimeline` is an event the backend actually emitted.

The env prefix is `react_PUBLIC_` (not `VITE_`), registered in `vite.config.ts`'s `envPrefix`; `react_PUBLIC_API_BASE_URL` sets the backend base URL and is also used to build the absolute `EventSource` URL. Backend CORS defaults to `http://localhost:3000` only (`settings.cors_origins`).

Response shapes are typed in `src/types/investigation.ts`, but the backend returns `dict[str, Any]` for `investigation` and `diagnosis` — **the TS types are the only contract and Pydantic will not catch drift.** `scratchpad/contract_check.py` style verification (run the backend against a fake cluster, assert every field the console reads is present) is the way to check it.

`vite.config.ts` imports `defineConfig` from `vitest/config`, not `vite` — vitest owns the merged config type. Tests need `IS_REACT_ACT_ENVIRONMENT` from `src/test/setup.ts`. Avoid Testing Library's `waitFor` in fake-timer tests: it polls on timers and will hang; advance timers explicitly instead.

## Notes

- `prompts/` is documented as the home for prompt templates, but no code loads from it today; prompts are inline in `app/ai/prompt_builder.py`. The README's references to `docs/PROJECT_OVERVIEW.md` and the topology/multi-agent features are roadmap, not present.
- Dead code with no importers: `app/ai/client.py`, `app/kubernetes/inspector.py` (its `inspect_nodes()` is a hardcoded stub, unrelated to the real `node_inspector.py`), and `start_investigation()` at the bottom of `investigation_service.py`. The live entry points are `LLMClient`, the per-resource inspectors, and `InvestigationService.run()`.
