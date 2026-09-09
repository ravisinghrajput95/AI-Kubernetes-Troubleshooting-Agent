import { useCallback, useEffect, useRef, useState } from "react";

import { authHeaders } from "../services/auth";
import { readEventStream } from "../services/eventStream";
import {
  cancelInvestigationJob,
  eventStreamUrl,
  getInvestigationJob,
  getInvestigationJobStatus,
  startInvestigationJob,
  type InvestigationScope,
} from "../services/api";
import type {
  Diagnosis,
  InvestigationHistoryItem,
  InvestigationJobState,
  InvestigationResponse,
  JobEvent,
  JobStatus,
} from "../types/investigation";

type InvestigationData = InvestigationResponse["investigation"];

export type JobPhase = "idle" | JobStatus;

/** How progress is being received; surfaced so operators can see a degraded path. */
export type Transport = "stream" | "poll" | null;

const POLL_INTERVAL_MS = 1500;

/**
 * The event names the server sends, from `docs/INVESTIGATION_API.md`.
 *
 * Exported and held against the documented set by
 * `backend/tests/test_documentation.py`, because a name added on the server and
 * not here stops being delivered *silently* — the console falls back to polling
 * and the only symptom is a tag nobody reads. That is exactly how the whole
 * stream went unnoticed.
 */
export const STREAM_EVENT_TYPES = [
  "queued",
  "started",
  "progress",
  "completed",
  "failed",
  "cancelled",
] as const;
const TERMINAL: JobStatus[] = ["succeeded", "failed", "cancelled"];

function isTerminal(status: JobPhase): boolean {
  return TERMINAL.includes(status as JobStatus);
}

export interface InvestigationJobHandle {
  phase: JobPhase;
  isRunning: boolean;
  transport: Transport;
  jobId: string | null;
  timeline: JobEvent[];
  investigation?: InvestigationData;
  diagnosis?: Diagnosis;
  historyItem?: InvestigationHistoryItem;
  error: string;
  start: (context?: string, scope?: InvestigationScope) => Promise<string | null>;
  attach: (id: string) => Promise<void>;
  cancel: () => Promise<void>;
  reset: () => void;
}

/**
 * Drives one investigation job.
 *
 * Progress arrives over SSE, read with `fetch` rather than `EventSource` —
 * see `services/eventStream.ts`, which exists because EventSource cannot send
 * the `Authorization` header every endpoint here requires, so the stream 401'd
 * in every authenticated deployment and this hook silently polled instead.
 *
 * The fallback remains for the reason it was written: corporate proxies block
 * or buffer streams, and a stalled screen during an incident is worse than a
 * slower one. Both paths converge on the same terminal fetch for the full
 * result, and `transport` is surfaced in the UI because a degraded path that
 * nothing reports is a path nobody notices is degraded.
 */
export function useInvestigationJob(): InvestigationJobHandle {
  const [phase, setPhase] = useState<JobPhase>("idle");
  const [transport, setTransport] = useState<Transport>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<JobEvent[]>([]);
  const [investigation, setInvestigation] = useState<InvestigationData>();
  const [diagnosis, setDiagnosis] = useState<Diagnosis>();
  const [historyItem, setHistoryItem] = useState<InvestigationHistoryItem>();
  const [error, setError] = useState("");

  const streamRef = useRef<AbortController | null>(null);
  const pollRef = useRef<number | null>(null);
  const receivedRef = useRef(false);
  const settledRef = useRef(false);
  const mountedRef = useRef(true);

  const teardown = useCallback(() => {
    streamRef.current?.abort();
    streamRef.current = null;
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      teardown();
    };
  }, [teardown]);

  /** Adopt a terminal state that has already been fetched. */
  const applyResult = useCallback(
    (state: InvestigationJobState, fallback: JobStatus) => {
      settledRef.current = true;
      teardown();
      setInvestigation(state.investigation);
      setDiagnosis(state.diagnosis);
      setHistoryItem(state.history_item);
      setPhase(state.status ?? fallback);
      if (state.timeline?.length) {
        setTimeline(state.timeline);
      }
      if (state.error) {
        setError(state.error);
      }
    },
    [teardown],
  );

  const settle = useCallback(
    async (id: string, status: JobStatus) => {
      if (settledRef.current) {
        return;
      }
      settledRef.current = true;
      teardown();

      try {
        const state = await getInvestigationJob(id);
        if (!mountedRef.current) {
          return;
        }
        applyResult(state, status);
      } catch {
        if (mountedRef.current) {
          setPhase(status);
          setError((current) => current || "Could not load the investigation result.");
        }
      }
    },
    [applyResult, teardown],
  );

  const poll = useCallback(
    (id: string) => {
      if (pollRef.current !== null) {
        return;
      }
      setTransport("poll");

      pollRef.current = window.setInterval(async () => {
        try {
          // The status projection, not the full job. This runs every 1.5s and
          // reads exactly two fields; the full endpoint would re-serialise a
          // finished investigation on every tick. `settle` below does the one
          // full read, when there is actually a result to render.
          const state = await getInvestigationJobStatus(id);
          if (!mountedRef.current) {
            return;
          }
          if (state.timeline?.length) {
            setTimeline(state.timeline);
          }
          setPhase(state.status);
          if (isTerminal(state.status)) {
            await settle(id, state.status);
          }
        } catch {
          // Transient failures are expected while the backend is busy; the
          // next tick retries. A hard failure surfaces via the job status.
        }
      }, POLL_INTERVAL_MS);
    },
    [settle],
  );

  const stream = useCallback(
    (id: string) => {
      // `fetch` streaming, not `EventSource`. EventSource cannot send an
      // `Authorization` header and there is no way to give it one, so against
      // any deployment with authentication configured — since `AUTH_MODE` lost
      // its default, every deployment — the stream request arrived anonymous
      // and the platform answered 401. Measured in Chrome against a token
      // deployment before this change: `{}` events of every type and an error,
      // the identical signature to the dispatch defect fixed in 99b9d27.
      if (typeof window === "undefined" || typeof fetch !== "function") {
        poll(id);
        return;
      }

      const controller = new AbortController();
      streamRef.current = controller;
      setTransport("stream");

      const consume = async () => {
        try {
          for await (const frame of readEventStream(eventStreamUrl(id), {
            headers: authHeaders(),
            signal: controller.signal,
          })) {
            // The server names every frame. A browser routes named events only
            // to matching listeners, which is why `onmessage` alone never
            // fired; reading the name off the frame keeps that impossible to
            // get wrong, and an unrecognised name is ignored rather than
            // parsed as a job event.
            if (!STREAM_EVENT_TYPES.includes(frame.type as never)) {
              continue;
            }
            receivedRef.current = true;

            let payload: JobEvent;
            try {
              payload = JSON.parse(frame.data) as JobEvent;
            } catch {
              continue;
            }

            if (!mountedRef.current) {
              return;
            }

            setTimeline((current) => [...current, payload]);

            if (payload.type === "started") {
              setPhase("running");
            } else if (payload.type === "completed") {
              void settle(id, "succeeded");
              return;
            } else if (payload.type === "failed") {
              setError(payload.message);
              void settle(id, "failed");
              return;
            } else if (payload.type === "cancelled") {
              void settle(id, "cancelled");
              return;
            }
          }

          // The stream ended without a terminal frame: the job may have
          // finished between frames, or a proxy dropped a connection it
          // considered idle. Polling converges either way.
          if (!settledRef.current && mountedRef.current) {
            poll(id);
          }
        } catch {
          // A 401, a blocked stream, a proxy that buffers to death. The
          // fallback is what keeps an operator from watching a stalled screen
          // during an incident — and it is also what hid this transport being
          // dead, twice, which is why `transport` is surfaced in the UI.
          if (!settledRef.current && mountedRef.current) {
            streamRef.current = null;
            poll(id);
          }
        }
      };

      void consume();
    },
    [poll, settle],
  );

  const reset = useCallback(() => {
    teardown();
    receivedRef.current = false;
    settledRef.current = false;
    setPhase("idle");
    setTransport(null);
    setJobId(null);
    setTimeline([]);
    setInvestigation(undefined);
    setDiagnosis(undefined);
    setHistoryItem(undefined);
    setError("");
  }, [teardown]);

  const start = useCallback(
    async (context?: string, scope?: InvestigationScope) => {
      reset();
      setPhase("pending");

      try {
        const accepted = await startInvestigationJob(context, scope);
        if (!mountedRef.current) {
          return null;
        }
        setJobId(accepted.id);
        stream(accepted.id);
        return accepted.id;
      } catch {
        if (mountedRef.current) {
          setPhase("failed");
          setError(
            "Unable to start the investigation. Confirm the backend API is reachable.",
          );
        }
        return null;
      }
    },
    [reset, stream],
  );

  /**
   * Follow an investigation this hook did not start.
   *
   * What makes a result addressable: opening `/investigations/:id` joins a run
   * already in flight, or renders one that finished — including one that has
   * been evicted from the job store and is served from its persisted report.
   */
  const attach = useCallback(
    async (id: string) => {
      reset();
      setJobId(id);
      setPhase("pending");

      try {
        const state = await getInvestigationJob(id);
        if (!mountedRef.current) {
          return;
        }
        if (state.timeline?.length) {
          setTimeline(state.timeline);
        }
        if (isTerminal(state.status)) {
          // Already finished: adopt what was just fetched rather than opening
          // a stream that would immediately close, or fetching it twice.
          applyResult(state, state.status);
          return;
        }
        setPhase(state.status);
        stream(id);
      } catch {
        if (mountedRef.current) {
          setPhase("failed");
          setError("Could not load this investigation.");
        }
      }
    },
    [applyResult, reset, stream],
  );

  const cancel = useCallback(async () => {
    if (!jobId || isTerminal(phase)) {
      return;
    }
    try {
      await cancelInvestigationJob(jobId);
    } catch {
      setError("Could not cancel the investigation.");
    }
  }, [jobId, phase]);

  return {
    phase,
    isRunning: phase === "pending" || phase === "running",
    transport,
    jobId,
    timeline,
    investigation,
    diagnosis,
    historyItem,
    error,
    start,
    attach,
    cancel,
    reset,
  };
}
