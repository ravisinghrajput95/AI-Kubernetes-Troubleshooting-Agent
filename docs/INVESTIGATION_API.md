# Investigation API

Two ways to run an investigation. Both execute the same pipeline
(`app/services/investigation_runner.py`), so results are identical.

## Synchronous

```http
POST /investigate
{"context": "prod-east", "namespace": "payments"}
```

Blocks until the investigation completes and returns the full result. Retained
for backward compatibility and fine for scoped investigations. Deep
investigations on large clusters should use the job API instead.

## Asynchronous

```http
POST /investigations
{"context": "prod-east", "namespace": "payments"}

202 Accepted
{
  "id": "8a732d2c-...",
  "status": "pending",
  "status_url": "/investigations/8a732d2c-...",
  "events_url": "/investigations/8a732d2c-.../events"
}
```

| Endpoint | Purpose |
|---|---|
| `GET /investigations/{id}` | Current status, timeline, and result when finished |
| `GET /investigations/{id}/events` | SSE stream of progress |
| `POST /investigations/{id}/cancel` | Abort a running investigation |
| `GET /investigation-jobs` | Jobs held by this process (no payloads) |
| `GET /investigations` | Persisted report history |

### One id for the whole lifecycle

The job id **is** the investigation id. When the run completes, the report is
persisted under that same id, so these resolve without a second lookup:

```
/investigations/{id}/report
/investigations/{id}/pdf
/investigations/{id}/json
/investigations/{id}/markdown
```

`GET /investigations/{id}` resolves a live job first, then falls back to the
persisted report — so an id stays addressable after the job is evicted from
memory or the process restarts.

### Status values

| Status | Meaning |
|---|---|
| `pending` | Accepted, not yet started |
| `running` | Collecting or reasoning |
| `succeeded` | Completed with usable evidence |
| `failed` | Crashed, or collected no usable evidence at all |
| `cancelled` | Aborted by request |

**Partial versus total failure.** Losing one inspector degrades an
investigation — it still succeeds, with `evidence_coverage.completeness` below
100 and the gap named in `evidence_gaps`. Collecting *nothing* usable is a
different condition: there is nothing to reason over, so the job fails rather
than presenting a baseless diagnosis as a success.

## Event stream

```
event: progress
data: {"type":"progress","message":"Retrieved Pods","at":"...","time":"11:24:44","data":{"collector":"k8s.pods","duration_ms":41}}
```

Event types: `queued`, `started`, `progress`, `completed`, `failed`,
`cancelled`. A `: keepalive` comment is sent every 15 idle seconds.

The stream replays everything that already happened before going live, so a
client connecting mid-run still receives the full timeline. Verified against a
real server: events arrive as work completes, not batched at the end.

**`Last-Event-ID` resumes a broken stream, measured rather than asserted.**
Disconnected mid-run after frame 31, reconnected with `Last-Event-ID: 31`, and
the stream resumed at 32 and ran contiguously to 70 — **no duplicates, no gap,
monotonic ids, terminal event delivered**. That is the whole point of the id
being the event sequence: a client that drops does not have to choose between
replaying the timeline it already has and missing what it did not.

**Register a listener per event name, not `onmessage`.** Every frame here is
named, and a browser routes a named event only to
`addEventListener("<name>", ...)`; `onmessage` fires solely for an unnamed
event, which this endpoint never sends. The console got this wrong for its
whole life — the stream opened, delivered nothing and fell back to polling —
so the example below is the contract, not an illustration.

```js
const events = new EventSource(`/investigations/${id}/events`);
events.addEventListener("progress", (e) => appendStep(JSON.parse(e.data)));
events.addEventListener("completed", () => { events.close(); loadResult(id); });
```

## Deployment constraints

Job state is held **in the process**:

- Jobs do not survive a restart. Completed investigations do, via their reports.
- Multiple uvicorn workers will not share jobs; a request routed to another
  worker sees a 404 until the report is persisted. **Run a single worker**, or
  replace the store.

`InvestigationJobStore` is the seam: implement `create/get/list/publish/
subscribe` against Redis or a database and nothing above it changes. The store
keeps at most 100 jobs, evicting the oldest terminal ones; running jobs are
never evicted.

Back-pressure is one-directional by design: a subscriber that stops reading
loses events rather than blocking the investigation that produces them.
