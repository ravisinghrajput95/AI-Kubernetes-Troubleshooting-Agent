/**
 * The SSE grammar, tested against the bytes the platform actually writes.
 *
 * `investigate.py` frames every event as `id: N\nevent: <type>\ndata: {...}`
 * and sends `: keepalive` between them. These are the cases a network creates
 * and a callback-shaped test double cannot: a frame split across packets, two
 * frames in one packet, and a comment that must never become an event.
 */
import { describe, expect, it, vi } from "vitest";

import { decodeFrames, readEventStream } from "./eventStream";

function frame(type: string, payload: unknown, seq: number): string {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`;
}

describe("decodeFrames", () => {
  it("decodes the frame shape the platform writes", () => {
    const { frames, rest } = decodeFrames(frame("progress", { message: "Retrieved Pods" }, 7));

    expect(rest).toBe("");
    expect(frames).toEqual([
      { id: "7", type: "progress", data: JSON.stringify({ message: "Retrieved Pods" }) },
    ]);
  });

  it("holds a partial frame back rather than delivering half of it", () => {
    const whole = frame("progress", { message: "half a payload" }, 1);
    const cut = whole.length - 10;

    const first = decodeFrames(whole.slice(0, cut));
    expect(first.frames).toEqual([]);

    // The remainder must carry the buffer forward, or the JSON never parses.
    const second = decodeFrames(first.rest + whole.slice(cut));
    expect(second.frames).toHaveLength(1);
    expect(JSON.parse(second.frames[0].data)).toEqual({ message: "half a payload" });
  });

  it("decodes several frames arriving in one packet", () => {
    const { frames } = decodeFrames(
      frame("started", { message: "a" }, 1) + frame("progress", { message: "b" }, 2),
    );

    expect(frames.map((f) => f.type)).toEqual(["started", "progress"]);
    expect(frames.map((f) => f.id)).toEqual(["1", "2"]);
  });

  it("ignores a keepalive comment", () => {
    const { frames } = decodeFrames(": keepalive\n\n");
    expect(frames).toEqual([]);
  });

  it("defaults to the message type when the server names no event", () => {
    // The rule that made `onmessage` alone never fire against this server:
    // named events go only to matching listeners, and unnamed ones are
    // "message". Kept correct so an unnamed frame is not silently dropped.
    const { frames } = decodeFrames('data: {"message":"unnamed"}\n\n');
    expect(frames[0].type).toBe("message");
  });

  it("strips exactly one space after the colon, per the grammar", () => {
    const { frames } = decodeFrames("event: progress\ndata:  two spaces\n\n");
    expect(frames[0].data).toBe(" two spaces");
  });
});

describe("readEventStream", () => {
  function streamOf(chunks: string[], status = 200): typeof fetch {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder();
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    return vi.fn().mockResolvedValue({
      ok: status === 200,
      status,
      body,
    }) as unknown as typeof fetch;
  }

  it("carries the credential and the resume position as headers", async () => {
    const fetchImpl = streamOf([]);
    const iterator = readEventStream("/investigations/job-1/events", {
      headers: { Authorization: "Bearer t" },
      lastEventId: "12",
      fetchImpl,
    });
    await iterator.next();

    // Both of these are the reason this is `fetch` and not `EventSource`:
    // one it cannot send at all, and the other it sends only on its own terms.
    expect(fetchImpl).toHaveBeenCalledWith(
      "/investigations/job-1/events",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer t",
          "Last-Event-ID": "12",
          Accept: "text/event-stream",
        }),
      }),
    );
  });

  it("throws on a refused stream rather than looking like a quiet one", async () => {
    // A 401 is what the platform answered before this module existed. Returned
    // as an empty stream it is indistinguishable from an investigation that
    // has not reported yet, and the console would wait forever instead of
    // falling back to polling.
    const iterator = readEventStream("/events", { fetchImpl: streamOf([], 401), });
    await expect(iterator.next()).rejects.toThrow("401");
  });

  it("yields every frame across chunk boundaries", async () => {
    const whole = frame("started", { message: "a" }, 1) + frame("progress", { message: "b" }, 2);
    const fetchImpl = streamOf([whole.slice(0, 25), whole.slice(25, 60), whole.slice(60)]);

    const seen: string[] = [];
    for await (const f of readEventStream("/events", { fetchImpl })) {
      seen.push(f.type);
    }

    expect(seen).toEqual(["started", "progress"]);
  });
});
