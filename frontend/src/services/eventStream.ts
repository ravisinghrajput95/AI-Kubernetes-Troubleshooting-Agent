/**
 * Server-sent events over `fetch`, because `EventSource` cannot authenticate.
 *
 * `EventSource` sends no `Authorization` header and offers no way to add one.
 * Every endpoint here is behind `require_principal`, so the console's stream
 * request arrived unauthenticated and the platform correctly answered **401**
 * — for every deployment with authentication configured, which since
 * `AUTH_MODE` lost its default is every deployment there is. The stream opened,
 * delivered nothing, errored, and the hook fell back to polling.
 *
 * That is the second time this transport has been dead end to end while
 * everything around it passed, and both times the fallback is what hid it: the
 * console still worked, only slower, and the sole symptom was a `polling` tag
 * on a healthy run. The first cause was the browser's event-dispatch rule
 * (fixed in `99b9d27`); this is the other half, and the earlier fix can only
 * have been verified with authentication switched off.
 *
 * `fetch` can set headers, so the same `Authorization` every other call uses
 * works here too — no token in a query string, where it would land in access
 * logs and browser history, and no second credential type to mint and expire.
 * The cost is parsing the wire format ourselves, which is the small function
 * below.
 */

/** One decoded frame. `type` is the SSE event name, defaulting per the spec. */
export interface StreamFrame {
  id: string;
  type: string;
  data: string;
}

export interface EventStreamOptions {
  headers?: Record<string, string>;
  /** Resume position; the backend reads this exact header. */
  lastEventId?: string;
  signal?: AbortSignal;
  /** Injected in tests. */
  fetchImpl?: typeof fetch;
}

/**
 * Parse an SSE byte stream into frames.
 *
 * Deliberately a generator over decoded text rather than a callback soup: the
 * chunk boundaries a network puts in are not frame boundaries, so the buffer
 * has to outlive a chunk, and that is the whole subtlety here.
 */
export function decodeFrames(buffer: string): { frames: StreamFrame[]; rest: string } {
  const frames: StreamFrame[] = [];
  // A frame ends at a blank line. Anything after the last one is a partial
  // frame and must stay in the buffer — splitting on "\n" instead would
  // deliver half a JSON payload the moment a frame straddles two packets.
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";

  for (const block of parts) {
    let id = "";
    let type = "";
    const data: string[] = [];

    for (const rawLine of block.split("\n")) {
      const line = rawLine.replace(/\r$/, "");
      // A line beginning with a colon is a comment. The platform sends
      // ": keepalive" to hold the connection open through proxies that drop
      // idle ones, and treating it as data would deliver an unparseable frame.
      if (line === "" || line.startsWith(":")) {
        continue;
      }
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      // Exactly one optional space after the colon is stripped, per the spec.
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }

      if (field === "id") id = value;
      else if (field === "event") type = value;
      else if (field === "data") data.push(value);
    }

    if (data.length === 0 && id === "" && type === "") {
      continue;
    }
    // "message" is the default type for an event with no `event:` field, which
    // is the rule that made `onmessage` alone never fire against this server.
    frames.push({ id, type: type || "message", data: data.join("\n") });
  }

  return { frames, rest };
}

/**
 * Open an authenticated SSE stream and yield frames until it ends.
 *
 * Throws if the response is not a 200 — a 401 here is the defect this module
 * exists for, and it must reach the caller as a failure to fall back on rather
 * than as an empty stream that looks like a quiet investigation.
 */
export async function* readEventStream(
  url: string,
  options: EventStreamOptions = {},
): AsyncGenerator<StreamFrame> {
  const doFetch = options.fetchImpl ?? fetch;
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    ...(options.headers ?? {}),
  };
  if (options.lastEventId) {
    headers["Last-Event-ID"] = options.lastEventId;
  }

  const response = await doFetch(url, {
    headers,
    signal: options.signal,
    // No `cache: "no-store"`: the platform sends the headers that say so, and
    // overriding here would mask a proxy that strips them.
  });

  if (!response.ok || !response.body) {
    throw new Error(`stream failed with HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = decodeFrames(buffer);
      buffer = rest;
      for (const frame of frames) {
        yield frame;
      }
    }
  } finally {
    // Releasing matters: an abandoned reader holds the connection open, and
    // this stream can be abandoned on every navigation away from the page.
    reader.releaseLock();
  }
}
