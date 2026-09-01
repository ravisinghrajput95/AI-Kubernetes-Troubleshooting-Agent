# SRE Console

The frontend surface for the investigation platform. Its job is to make the
backend's reasoning inspectable — what was collected, what was concluded, and
how strongly.

## Structure

`src/App.tsx` is **98 lines**: the authenticated routing table and the sign-in
gate, and nothing else. Everything it used to hold lives in a dedicated module.

| Path | Contains |
|---|---|
| `src/routes/` | One page per address, including `InvestigationPage` and `ReportsPage` |
| `src/hooks/useInvestigationJob.ts` | Job submission, SSE, polling fallback, cancellation |
| `src/lib/analysis.ts` | Grouping, filtering, ordering, formatting — no React |
| `src/lib/remediation.ts` | The YAML, PR description and apply plan the remediation panel offers |
| `src/services/http.ts` | JSON transport over `fetch` |
| `src/components/` | Presentational panels |

Pure logic is kept out of components so it can be tested without rendering, and
that is not a style preference: `buildRemediationYaml` writes a manifest a
person is invited to apply to a production cluster, and while it lived in
`App.tsx` reaching it from a test meant rendering a panel, clicking a tab and
reading a `<pre>` — so it had no tests at all. It has fifteen now.

## Bundle

Measured per package before changing anything:

| Package | gzip | Verdict |
|---|---|---|
| react + react-dom + scheduler | 60.6 KB | Irreducible |
| **axios** | **16.7 KB** | **Removed** |
| @tanstack/query | 12.3 KB | Earns it — caching and invalidation |
| Console's own code | 15.6 KB | Already lean |

axios cost more gzipped than the entire console's own code, for eight JSON
requests. `src/services/http.ts` replaces it with a `fetch` wrapper keeping only
what was used: a base URL, a 120s timeout via `AbortController`, JSON
encode/decode, and throwing on non-2xx. It adds a typed `ApiError` carrying
`kind` (`network` / `timeout` / `http`) and `status`, which is more than axios
gave us for distinguishing "backend unreachable" from "investigation not found".

Result: **104 KB → 88 KB gzipped**, and one fewer runtime dependency — which for
a platform running against production clusters is a supply-chain reduction as
much as a byte one.

Vendor chunks are split so a deploy only invalidates the ~16 KB app chunk rather
than all 88 KB. Two things worth knowing:

- The `manualChunks` **function** form is required. The object form does not
  capture subpath imports such as `react-dom/client`, which silently leaves
  react-dom (60 KB gzipped) in the app chunk — the split appears to work while
  achieving nothing.
- The console's own panels are deliberately **not** split. At 15.6 KB total,
  each additional chunk would cost a round trip worth more than the bytes saved.
  Revisit only if something genuinely heavy lands, such as a graph library for
  the topology view.

## Panels

| Panel | Answers |
|---|---|
| `LiveTimeline` | What is the investigation doing *right now* |
| `HypothesisPanel` | What might be wrong, ranked, with evidence for and against |
| `SignalTable` | What was actually observed, grouped by domain |
| `ConfidenceBreakdown` | Why the confidence number is what it is |
| `EvidenceExplorer` | Every fact collected, and the command that produced it |
| `PlaybookRounds` | What deep investigation ran, and what it added |

`HypothesisPanel` shows **refuting** signals as prominently as supporting ones.
A hypothesis the evidence argues against is often the most useful thing to learn
early in an incident, and hiding it would misrepresent the analysis.

`EvidenceExplorer` sorts kinds with degraded evidence first and offers an "only
gaps" filter — during an incident, what the platform *could not* see usually
matters more than what it could.

## Live progress

Investigations are submitted as background jobs. Progress streams over SSE:

```
POST /investigations  →  202 { id }
EventSource /investigations/{id}/events
GET /investigations/{id}          (once terminal, for the full result)
```

**The transport degrades on purpose.** `EventSource` cannot send custom headers
and is frequently blocked by corporate proxies, so the hook falls back to
polling the job endpoint every 1.5s. A stalled progress screen during an
incident is worse than a slower one. The active transport is shown in the UI, so
an operator can tell when they are on the degraded path.

One subtlety worth knowing: the server closes the stream when the job ends, and
the browser reports that as an `error`. The hook tracks whether the job already
settled, so a normal close is not mistaken for a transport failure.

## Two rules this console holds to

**Never display evidence the backend did not report.** `ConfidenceEvidence`
previously fell back to a hardcoded list of plausible-looking evidence sources
when none were reported. In a platform whose entire premise is that no claim is
made without evidence, that is a correctness bug rather than a cosmetic one.
Panels now render an explicit empty state.

**Progress must be real.** The previous implementation advanced a six-step
"agent workflow" on a 900ms timer, unrelated to what the backend was doing.
Every row in `LiveTimeline` is now an event the backend emitted, with the
duration each collector took.

`ConfidenceBreakdown` follows the same principle in reverse: when the weighted
components do not sum to the reported confidence, it says so rather than hiding
the discrepancy, and it surfaces any fabricated citations the grounding
validator rejected.

## Testing

```bash
npm test          # vitest run
npm run build     # tsc -b && vite build — the type gate
```

`src/lib/analysis.test.ts` covers pure logic. `useInvestigationJob.test.ts`
drives the hook against a fake `EventSource`, covering the state machine: stream
success, polling fallback, `EventSource` absent, submission failure, job
failure, cancellation, reset between runs, and unmount cleanup.
`http.test.ts` covers the transport with `fetch` mocked.

Mocked `fetch` proves the logic but not the wire, so `http.integration.test.ts`
runs the same calls against a real backend — real HTTP, real FastAPI
`{"detail": ...}` error bodies, real JSON shapes. It is skipped unless opted in,
keeping the default suite hermetic:

```bash
VITE_API_INTEGRATION=1 react_PUBLIC_API_BASE_URL=http://127.0.0.1:8778 npm test
```

Note the flag is `VITE_`-prefixed rather than read from `process.env`: this is a
browser tsconfig with no Node types, and Vite only exposes prefixed variables.

Two harness constraints:

- `IS_REACT_ACT_ENVIRONMENT` must be set (`src/test/setup.ts`).
- Do not use Testing Library's `waitFor` in a fake-timer test — it polls on
  timers and will hang until the test times out, leaking fake timers into the
  next test. Advance timers explicitly instead.

## Contract drift

The backend returns `dict[str, Any]` for `investigation` and `diagnosis`, so the
TypeScript types are the only contract and Pydantic will not catch a mismatch.
Verify by running the backend against a fake cluster and asserting every field
the console reads is present — field names, nested shapes, and the enumerated
`severity` and evidence `status` values the colour palettes key on.
