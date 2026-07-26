import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useInvestigationJob } from "./useInvestigationJob";
import * as api from "../services/api";

/** Minimal controllable EventSource stand-in. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(payload: Record<string, unknown>) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }

  fail() {
    this.onerror?.();
  }

  static latest() {
    return FakeEventSource.instances[FakeEventSource.instances.length - 1];
  }
}

const RESULT = {
  id: "job-1",
  status: "succeeded" as const,
  investigation: { context: "test", health: { status: "issues_found" } },
  diagnosis: { root_cause: "Missing DB_HOST" },
  history_item: { id: "job-1", pdf_url: "/investigations/job-1/pdf" },
};

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.spyOn(api, "startInvestigationJob").mockResolvedValue({
    id: "job-1",
    status: "pending",
    status_url: "/investigations/job-1",
    events_url: "/investigations/job-1/events",
  });
  vi.spyOn(api, "getInvestigationJob").mockResolvedValue(
    RESULT as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>,
  );
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
    expect(FakeEventSource.latest().url).toContain("/investigations/job-1/events");
  });

  it("appends streamed progress and tracks the running phase", async () => {
    const { result } = renderHook(() => useInvestigationJob());
    await act(async () => {
      await result.current.start("test");
    });

    act(() => {
      FakeEventSource.latest().emit({
        type: "started",
        message: "Investigation started",
        at: "t0",
        time: "10:00:00",
      });
      FakeEventSource.latest().emit({
        type: "progress",
        message: "Retrieved Pods",
        at: "t1",
        time: "10:00:01",
      });
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
      FakeEventSource.latest().emit({
        type: "completed",
        message: "Investigation complete",
        at: "t2",
        time: "10:00:05",
      });
    });

    await waitFor(() => expect(result.current.phase).toBe("succeeded"));
    expect(result.current.diagnosis?.root_cause).toBe("Missing DB_HOST");
    expect(result.current.historyItem?.pdf_url).toBe("/investigations/job-1/pdf");
    expect(FakeEventSource.latest().closed).toBe(true);
  });

  it("falls back to polling when the stream fails before settling", async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useInvestigationJob());
      await act(async () => {
        await result.current.start("test");
      });

      act(() => {
        FakeEventSource.latest().fail();
      });

      expect(result.current.transport).toBe("poll");

      // waitFor polls on timers, which are faked here; advance explicitly instead.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
        await vi.advanceTimersByTimeAsync(0);
      });

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
      FakeEventSource.latest().emit({
        type: "completed",
        message: "done",
        at: "t",
        time: "10:00:05",
      });
    });
    await waitFor(() => expect(result.current.phase).toBe("succeeded"));

    act(() => {
      FakeEventSource.latest().fail();
    });

    // The server closing the stream must not look like a transport failure.
    expect(result.current.transport).toBe("stream");
    expect(result.current.phase).toBe("succeeded");
  });

  it("polls directly when EventSource is unavailable", async () => {
    vi.stubGlobal("EventSource", undefined);
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
      FakeEventSource.latest().emit({
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
      FakeEventSource.latest().emit({
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
    expect(FakeEventSource.latest().closed).toBe(true);
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
