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

**Do not use `EventSource`.** This endpoint is behind authentication like every
other, and `EventSource` cannot send an `Authorization` header — there is no
option for it in the API. So against any deployment with authentication
configured, which since `AUTH_MODE` lost its default is all of them, an
`EventSource` request arrives anonymous and is answered **401**. The console
shipped exactly that and silently polled for its whole life; measured in Chrome
against a token deployment, it received **zero events of every type** and an
error. Read the stream with `fetch` instead, which can carry the credential:

```js
const response = await fetch(`${apiBaseUrl}/investigations/${id}/events`, {
  headers: {
    Accept: "text/event-stream",
    Authorization: `Bearer ${token}`,
    // Optional, and the reason a reconnect is cheap. `EventSource` sets this
    // itself; with `fetch` you send it, from the last `id:` you saw.
    ...(lastEventId ? { "Last-Event-ID": String(lastEventId) } : {}),
  },
});
if (!response.ok) throw new Error(`stream refused: ${response.status}`);

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  // A frame ends at a blank line, and chunk boundaries are not frame
  // boundaries — splitting on "\n" delivers half a JSON payload.
  const blocks = buffer.split("\n\n");
  buffer = blocks.pop();
  for (const block of blocks) {
    if (block.startsWith(":")) continue;              // ": keepalive"
    const type = block.match(/^event: (.*)$/m)?.[1];
    const data = block.match(/^data: (.*)$/m)?.[1];
    if (type && data) handle(type, JSON.parse(data));
  }
}
```

**Read the event name off the frame, never `onmessage`.** Every frame here is
named, and a browser routes a named event only to
`addEventListener("<name>", ...)`; `onmessage` fires solely for an unnamed
event, which this endpoint never sends. That was the *first* reason the console
received nothing, fixed before the 401 was found underneath it — two independent
faults on one path, each sufficient to break it, and the polling fallback hid
both. `frontend/src/services/eventStream.ts` is the working implementation.

## Deployment constraints

Which of these applies is decided once at startup, by whether `DATABASE_URL`
and `REDIS_URL` are set. Setting exactly one is refused.

**Neither set — the single-process default.** Job state is held in the process:

- Jobs do not survive a restart. Completed investigations do, via their reports.
- Multiple uvicorn workers will not share jobs; a request routed to another
  worker sees a 404 until the report is persisted. **Run a single worker.**
- The store keeps at most 100 jobs, evicting the oldest terminal ones; running
  jobs are never evicted.

**Both set — the distributed deployment.** Jobs are Postgres rows and Redis
carries the queue, the cancel channel and the event fan-out, so jobs survive a
restart, any worker can answer for any job, and nothing is evicted. This is the
supported way to run more than one worker, and it is what the throughput
numbers in `docs/PERFORMANCE_ENVELOPE.md` are measured on.

`JobStore` (`backend/app/jobs/base.py`) is the seam both satisfy — no API
handler knows which it has. `InvestigationJobStore` is a retained alias for the
in-memory one.

Back-pressure is one-directional by design: a subscriber that stops reading
loses events rather than blocking the investigation that produces them.
