import { EmptyState, Panel, Tag } from "./ui";
import type { JobEvent, JobStatus } from "../types/investigation";
import type { JobPhase, Transport } from "../hooks/useInvestigationJob";

const EVENT_TONE: Record<JobEvent["type"], string> = {
  queued: "border-slate-700 bg-slate-900 text-slate-400",
  started: "border-sky-800 bg-sky-950/40 text-sky-300",
  progress: "border-slate-700 bg-slate-900 text-slate-300",
  completed: "border-lime-800 bg-lime-950/40 text-lime-300",
  failed: "border-red-800 bg-red-950/40 text-red-300",
  cancelled: "border-amber-800 bg-amber-950/40 text-amber-300",
};

const PHASE_TONE: Record<JobStatus, string> = {
  pending: "border-slate-700 bg-slate-900 text-slate-300",
  running: "border-sky-800 bg-sky-950/40 text-sky-300",
  succeeded: "border-lime-800 bg-lime-950/40 text-lime-300",
  failed: "border-red-800 bg-red-950/40 text-red-300",
  cancelled: "border-amber-800 bg-amber-950/40 text-amber-300",
};

/**
 * Live investigation progress.
 *
 * Every row is an event the backend actually emitted — there is no simulated
 * progress here.
 */
export function LiveTimeline({
  phase,
  transport,
  timeline,
  onCancel,
}: {
  phase: JobPhase;
  transport: Transport;
  timeline: JobEvent[];
  onCancel: () => void;
}) {
  const running = phase === "pending" || phase === "running";
  const deepIndex = timeline.findIndex((event) =>
    event.message.startsWith("Running deep investigation"),
  );

  return (
    <Panel
      title="Investigation Progress"
      subtitle={
        running
          ? "Streaming live from the backend."
          : phase === "idle"
            ? "Waiting for an investigation to start."
            : "Investigation finished."
      }
      action={
        <div className="flex items-center gap-2">
          {transport === "poll" ? (
            <Tag
              label="polling"
              title="The event stream was unavailable, so progress is being polled."
              className="border-amber-800 bg-amber-950/40 text-amber-300"
            />
          ) : null}
          <Tag
            label={phase === "idle" ? "idle" : phase}
            className={
              phase === "idle"
                ? "border-slate-700 bg-slate-900 text-slate-400"
                : PHASE_TONE[phase as JobStatus]
            }
          />
          {running ? (
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-slate-700 px-3 py-1 text-xs font-semibold text-slate-300 hover:border-red-700 hover:text-red-300"
            >
              Cancel
            </button>
          ) : null}
        </div>
      }
    >
      {timeline.length === 0 ? (
        <EmptyState message="No progress yet." />
      ) : (
        <ol className="grid gap-2">
          {timeline.map((event, index) => (
            <li
              key={`${event.at}-${index}`}
              className={`flex items-start gap-3 rounded-md border px-3 py-2 text-sm ${EVENT_TONE[event.type]}`}
            >
              <span className="font-mono text-xs text-slate-500">{event.time}</span>
              <span className="min-w-0 flex-1">
                {deepIndex >= 0 && index >= deepIndex ? (
                  <span className="mr-2 text-fuchsia-300">deep</span>
                ) : null}
                {event.message}
              </span>
              {typeof event.data?.duration_ms === "number" ? (
                <span className="font-mono text-xs text-slate-600">
                  {String(event.data.duration_ms)}ms
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
