import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useInvestigationJob } from "./useInvestigationJob";
import * as api from "../services/api";
import { clearToken, setToken } from "../services/auth";

/**
 * Controllable EventSource stand-in that dispatches the way a browser does.
 *
 * The previous version called `onmessage` directly from `emit()`, which
 * modelled a wire the server does not produce: every frame it sends is
 * *named* (`event: progress`), and per the HTML spec `onmessage` fires only
 * for the default, unnamed type. So the fake delivered events the real
 * browser never delivered, and four tests passed against a stream that had
 * never worked in a browser once.
 *
 * `emit()` now dispatches on `payload.type`, exactly as the server names it,
 * so a handler that is not registered for that name receives nothing — here
 * and in Chrome alike.
 */
/**
 * A fake **wire**, not a fake EventSource.
 *
 * Its predecessor called `onmessage` directly, which modelled a stream the
 * server does not produce and is exactly why the hook's tests all passed while
 * the console received zero events in a browser. This one writes the bytes
 * `investigate.py` actually writes — `id:`, `event:`, `data:`, blank line —
 * into a `ReadableStream` that the real parser reads, so a frame the console
 * cannot decode fails here.
 *
 * It also asserts what a browser cannot do: the request must carry an
 * `Authorization` header, because `EventSource` could not and that is the
 * defect this replaced.
 */
class FakeStream {
  static instances: FakeStream[] = [];
  private controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  private encoder = new TextEncoder();
  closed = false;

  constructor(
    public url: string,
    public headers: Record<string, string>,
    public status = 200,
    signal?: AbortSignal,
  ) {
    FakeStream.instances.push(this);
    // The hook tears down by aborting. A fake that ignored it would let
    // "closes the stream when unmounted" pass against a leaked connection.
    signal?.addEventListener("abort", () => {
      this.closed = true;
      try {
        this.controller?.error(new Error("aborted"));
      } catch {
        /* already closed */
      }
    });
  }

  response(): Response {
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
      cancel: () => {
        this.closed = true;
      },
    });
    return { ok: this.status === 200, status: this.status, body } as unknown as Response;
  }

  /** Write one frame exactly as the server frames it. */
  emit(payload: Record<string, unknown>, seq = 1) {
    const name = String(payload.type ?? "message");
    const frame = `id: ${seq}\nevent: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
    this.controller?.enqueue(this.encoder.encode(frame));
  }

  /** A keepalive comment, which must not be delivered as an event. */
  keepalive() {
    this.controller?.enqueue(this.encoder.encode(": keepalive\n\n"));
  }

  /** Deliver a frame split across two chunks, as a network does. */
  emitSplit(payload: Record<string, unknown>, seq = 1) {
    const name = String(payload.type ?? "message");
    const frame = `id: ${seq}\nevent: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
    const cut = Math.floor(frame.length / 2);
    this.controller?.enqueue(this.encoder.encode(frame.slice(0, cut)));
    this.controller?.enqueue(this.encoder.encode(frame.slice(cut)));
  }

  end() {
    this.controller?.close();
    this.closed = true;
  }

  fail() {
    this.controller?.error(new Error("stream dropped"));
    this.closed = true;
  }

  static latest() {
    return FakeStream.instances[FakeStream.instances.length - 1];
  }
}

/** Status the next stream request answers with; 401 is the defect's shape. */
let nextStreamStatus = 200;

/**
 * Let the reader drain.
 *
 * Frames now arrive through an async generator, so a write is delivered over
 * several microtask turns rather than by a synchronous callback the way the
 * old `onmessage` fake delivered it. That is a property of real streams, not
 * of this double.
 */
async function drain(turns = 6) {
  for (let i = 0; i < turns; i += 1) {
    await Promise.resolve();
  }
}

function installFetchStream() {
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const stream = new FakeStream(
      String(url),
      (init?.headers ?? {}) as Record<string, string>,
      nextStreamStatus,
      init?.signal ?? undefined,
    );
    return Promise.resolve(stream.response());
  });
}

const RESULT = {
  id: "job-1",
  status: "succeeded" as const,
  investigation: { context: "test", health: { status: "issues_found" } },
  diagnosis: { root_cause: "Missing DB_HOST" },
  history_item: { id: "job-1", pdf_url: "/investigations/job-1/pdf" },
};

beforeEach(() => {
  FakeStream.instances = [];
  nextStreamStatus = 200;
  installFetchStream();
  vi.spyOn(api, "startInvestigationJob").mockResolvedValue({
    id: "job-1",
    status: "pending",
    status_url: "/investigations/job-1",
    events_url: "/investigations/job-1/events",
  });
  vi.spyOn(api, "getInvestigationJob").mockResolvedValue(
    RESULT as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>,
  );
  // The polling loop reads the status projection, which carries no
  // investigation or diagnosis — that is the whole point of it. `settle` does
  // the one full read once the job is terminal.
  vi.spyOn(api, "getInvestigationJobStatus").mockResolvedValue({
    id: "job-1",
    status: "succeeded",
    timeline: [],
  } as unknown as Awaited<ReturnType<typeof api.getInvestigationJobStatus>>);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useInvestigationJob", () => {
  it("starts idle", () => {
    const { result } = renderHook(() => useInvestigationJob());
    expect(result.current.phase).toBe("idle");
    expect(result.current.isRunning).toBe(false);
    expect(result.current.timeline).toEqual([]);
  });

  it("submits a job and opens the event stream", async () => {
    const { result } = renderHook(() => useInvestigationJob());

    await act(async () => {
      await result.current.start("test", { namespace: "prod" });
    });

    expect(api.startInvestigationJob).toHaveBeenCalledWith("test", {
      namespace: "prod",
    });
    expect(result.current.jobId).toBe("job-1");
    expect(result.current.transport).toBe("stream");
    expect(FakeStream.latest().url).toContain("/investigations/job-1/events");
  });

  it("appends streamed progress and tracks the running phase", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    await act(async () => {
      FakeStream.latest().emit({
        type: "started",
        message: "Investigation started",
        at: "t0",
        time: "10:00:00",
      });
      FakeStream.latest().emit({
        type: "progress",
        message: "Retrieved Pods",
        at: "t1",
        time: "10:00:01",
      });
      await drain();
    });

    expect(result.current.phase).toBe("running");
    expect(result.current.isRunning).toBe(true);
    expect(result.current.timeline.map((event) => event.message)).toEqual([
      "Investigation started",
      "Retrieved Pods",
    ]);
  });

  it("fetches the full result when the job completes", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    await act(async () => {
      FakeStream.latest().emit({
        type: "completed",
        message: "Investigation complete",
        at: "t2",
        time: "10:00:05",
      });
      await drain();
    });

    await waitFor(() => expect(result.current.phase).toBe("succeeded"));
    expect(result.current.diagnosis?.root_cause).toBe("Missing DB_HOST");
    expect(result.current.historyItem?.pdf_url).toBe("/investigations/job-1/pdf");
    expect(FakeStream.latest().closed).toBe(true);
  });

  it("falls back to polling when the stream fails before settling", async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useInvestigationJob());
      await act(async () => {
        await result.current.start("test");
      });

      await act(async () => {
        FakeStream.latest().fail();
        await drain();
      });

      expect(result.current.transport).toBe("poll");

      // waitFor polls on timers, which are faked here; advance explicitly instead.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
        await vi.advanceTimersByTimeAsync(0);
      });

      // Polled cheaply, then settled once with the full read. Asserting both
      // is what stops the projection quietly reverting to the full endpoint.
      expect(api.getInvestigationJobStatus).toHaveBeenCalledWith("job-1");
      expect(api.getInvestigationJob).toHaveBeenCalledWith("job-1");
      expect(result.current.phase).toBe("succeeded");
      expect(result.current.diagnosis?.root_cause).toBe("Missing DB_HOST");
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores the stream error that follows normal completion", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    await act(async () => {
      FakeStream.latest().emit({
        type: "completed",
        message: "done",
        at: "t",
        time: "10:00:05",
      });
      await drain();
    });
    await waitFor(() => expect(result.current.phase).toBe("succeeded"));

    await act(async () => {
      FakeStream.latest().fail();
      await drain();
    });

    // The server closing the stream must not look like a transport failure.
    expect(result.current.transport).toBe("stream");
    expect(result.current.phase).toBe("succeeded");
  });

  it("polls directly when streaming is unavailable", async () => {
    vi.stubGlobal("fetch", undefined);
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useInvestigationJob());
      await act(async () => {
        await result.current.start("test");
      });

      expect(result.current.transport).toBe("poll");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.phase).toBe("succeeded");
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports a failed submission without leaving the UI running", async () => {
    vi.spyOn(api, "startInvestigationJob").mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useInvestigationJob());

    await act(async () => {
      await result.current.start("test");
    });

    expect(result.current.phase).toBe("failed");
    expect(result.current.isRunning).toBe(false);
    expect(result.current.error).toContain("Unable to start");
  });

  it("surfaces a failed job's error message", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "failed",
      error: "Unable to connect to Kubernetes cluster.",
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    await act(async () => {
      FakeStream.latest().emit({
        type: "failed",
        message: "Unable to connect to Kubernetes cluster.",
        at: "t",
        time: "10:00:02",
      });
    });

    await waitFor(() => expect(result.current.phase).toBe("failed"));
    expect(result.current.error).toContain("Unable to connect");
  });

  it("clears prior results when a new run starts", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });
    await act(async () => {
      FakeStream.latest().emit({
        type: "completed",
        message: "done",
        at: "t",
        time: "10:00:05",
      });
    });
    await waitFor(() => expect(result.current.diagnosis).toBeDefined());

    await act(async () => {
      await result.current.start("test");
    });

    expect(result.current.diagnosis).toBeUndefined();
    expect(result.current.timeline).toEqual([]);
    expect(result.current.phase).toBe("pending");
  });

  it("closes the stream when unmounted", async () => {
    const { result, unmount } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    unmount();
    expect(FakeStream.latest().closed).toBe(true);
  });

  it("does not cancel a job that never started", async () => {
    const cancelSpy = vi.spyOn(api, "cancelInvestigationJob").mockResolvedValue();
    const { result } = renderHook(() => useInvestigationJob());

    await act(async () => {
      await result.current.cancel();
    });

    expect(cancelSpy).not.toHaveBeenCalled();
  });

  it("cancels a running job", async () => {
    const cancelSpy = vi.spyOn(api, "cancelInvestigationJob").mockResolvedValue();
    const { result } = renderHook(() => useInvestigationJob());

    await act(async () => {
      await result.current.start("test");
    });
    await act(async () => {
      await result.current.cancel();
    });

    expect(cancelSpy).toHaveBeenCalledWith("job-1");
  });
});

describe("attach", () => {
  it("adopts a finished investigation without opening a stream", async () => {
    // Opening a finished run must not start an EventSource that would
    // immediately close, nor fetch the payload twice.
    const { result } = renderHook(() => useInvestigationJob());

    await act(async () => {
      await result.current.attach("job-1");
    });

    await waitFor(() => expect(result.current.phase).toBe("succeeded"));
    expect(result.current.diagnosis?.root_cause).toBe("Missing DB_HOST");
    expect(FakeStream.instances).toHaveLength(0);
    expect(api.getInvestigationJob).toHaveBeenCalledTimes(1);
  });

  it("follows a run that is still collecting", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValueOnce({
      id: "job-1",
      status: "running",
      timeline: [{ type: "started", message: "Investigation started" }],
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.attach("job-1");
    });

    await waitFor(() => expect(result.current.phase).toBe("running"));
    expect(FakeStream.latest()).toBeDefined();
    expect(result.current.timeline).toHaveLength(1);
  });

  it("streams progress for a run it did not start", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValueOnce({
      id: "job-1",
      status: "running",
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.attach("job-1");
    });

    await act(async () => {
      FakeStream.latest().emit({
        type: "progress",
        message: "Retrieved Pods",
        at: new Date().toISOString(),
        time: "00:00:01",
        seq: 3,
      });
    });

    expect(result.current.timeline.map((event) => event.message)).toContain("Retrieved Pods");
  });

  it("reports an id that does not resolve", async () => {
    vi.spyOn(api, "getInvestigationJob").mockRejectedValueOnce(new Error("404"));

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.attach("nope");
    });

    await waitFor(() => expect(result.current.phase).toBe("failed"));
    expect(result.current.error).toMatch(/could not load/i);
  });

  it("carries the evidence a failed run did collect", async () => {
    // A total collection failure still has degraded evidence behind it, and
    // the page has to be able to show it.
    vi.spyOn(api, "getInvestigationJob").mockResolvedValueOnce({
      id: "job-1",
      status: "failed",
      error: "Kubernetes investigation failed.",
      investigation: { evidence_coverage: { total: 11, usable: 0 } },
      diagnosis: {},
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.attach("job-1");
    });

    await waitFor(() => expect(result.current.phase).toBe("failed"));
    expect(result.current.investigation?.evidence_coverage?.total).toBe(11);
    expect(result.current.error).toMatch(/investigation failed/i);
  });
});

/**
 * The defect this transport was rewritten for.
 *
 * `EventSource` cannot send an `Authorization` header, so against any backend
 * with authentication configured the stream request arrived anonymous and was
 * answered 401 — measured in Chrome as zero events of every type and an error,
 * with the console silently polling for the whole of every investigation. None
 * of the hook's other tests can see it: they assert what happens once frames
 * arrive, and the defect is that the request is refused before any do.
 */
describe("the stream request is authenticated", () => {
  it("sends the Authorization header a browser EventSource could not", async () => {
    setToken("console-test-token");
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    const sent = FakeStream.latest().headers;
    expect(sent.Authorization).toBe("Bearer console-test-token");
    expect(sent.Accept).toBe("text/event-stream");
    clearToken();
  });

  it("does not open an EventSource, which could not carry the credential", async () => {
    // Stated directly, because the header assertion above passes for any
    // transport that happens to send one and this is the specific mechanism
    // that cannot: `EventSource` has no way to add a header, so reaching for
    // it again reintroduces the 401 exactly.
    const constructed: string[] = [];
    vi.stubGlobal(
      "EventSource",
      class {
        constructor(url: string) {
          constructed.push(url);
        }
        addEventListener() {}
        close() {}
      },
    );

    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    expect(constructed).toEqual([]);
  });

  it("falls back to polling when the stream is refused", async () => {
    // What the platform actually answered before this change.
    nextStreamStatus = 401;
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useInvestigationJob());
      await act(async () => {
        await result.current.start("test");
      });
      await act(async () => {
        await drain();
      });

      expect(result.current.transport).toBe("poll");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.phase).toBe("succeeded");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the wire, not a model of it", () => {
  it("decodes a frame that arrives split across two chunks", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    // A network splits wherever it likes. The old fake called `onmessage`
    // with a whole payload, so a parser that broke on a split frame would
    // have passed every test and dropped events in production.
    await act(async () => {
      FakeStream.latest().emitSplit({
        type: "progress",
        message: "Retrieved Pods",
        at: "t1",
        time: "10:00:01",
      });
      await drain();
    });

    expect(result.current.timeline.map((event) => event.message)).toEqual([
      "Retrieved Pods",
    ]);
  });

  it("does not deliver a keepalive comment as an event", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    await act(async () => {
      FakeStream.latest().keepalive();
      await drain();
    });

    // The platform sends ": keepalive" to hold the connection open through
    // proxies. Delivered as data it would be an unparseable timeline row.
    expect(result.current.timeline).toEqual([]);
  });
});
