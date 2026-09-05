import { Link } from "react-router";

import { AgentDot } from "./AgentDot";
import { SeverityDot } from "../report/SeverityDot";
import { relativeAge, type ClusterState, type FleetState } from "../../lib/fleet";
import type { SeverityTone } from "../../lib/report";

const LABEL: Record<FleetState, string> = {
  critical: "Critical",
  unreadable: "Could not read",
  warning: "Degraded",
  healthy: "Healthy",
  neutral: "Unknown",
  stale: "Stale",
  unknown: "Not investigated",
};

const STRIPE: Record<FleetState, string> = {
  critical: "bg-critical",
  unreadable: "bg-warning",
  warning: "bg-warning",
  healthy: "bg-healthy",
  neutral: "bg-ink-3",
  stale: "bg-ink-3",
  unknown: "bg-line",
};

/** Staleness and never-investigated are not severities; they read as neutral. */
function tone(state: FleetState): SeverityTone {
  if (state === "stale" || state === "unknown") return "neutral";
  if (state === "unreadable") return "warning";
  return state;
}

/**
 * One cluster, leading with the finding rather than the inventory.
 *
 * At 02:41 an SRE needs the sentence, not the pod count.
 */
export function ClusterCard({
  row,
  onInvestigate,
}: {
  row: ClusterState;
  onInvestigate: (name: string) => void;
}) {
  const body = (
    <div className="flex min-w-0 flex-1 items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="flex min-w-0 items-center gap-2">
          <span className="truncate font-semibold text-ink">{row.name}</span>
          {/* Reachability, not health. A connected agent says nothing about
              whether the cluster is well — the severity dot below does. */}
          <AgentDot agent={row.agent} label={false} />
        </p>
        <p className="mt-1 truncate text-sm text-ink-2">
          {/* The state chip already says "Not investigated"; repeating it here
              wastes the line. Name the cluster behind the context instead. */}
          {row.state === "unknown"
            ? row.cluster || "No investigation on record"
            : row.rootCause || "No finding recorded"}
          {row.state !== "unknown" && row.namespace && row.namespace !== "unknown" ? (
            <span className="text-ink-3"> · {row.namespace}</span>
          ) : null}
        </p>
        {row.state === "stale" ? (
          <p className="mt-1 text-sm text-warning">
            Last investigated {relativeAge(row.ageMs)} — this is what was true then.
          </p>
        ) : null}
      </div>

      <div className="shrink-0 text-right">
        <SeverityDot tone={tone(row.state)} label={LABEL[row.state]} />
        <p className="mt-1 font-mono text-sm text-ink-3">
          {row.confidence ? `${row.confidence}% · ` : ""}
          {relativeAge(row.ageMs)}
        </p>
      </div>
    </div>
  );

  return (
    // `min-w-0` is load-bearing: a grid item defaults to `min-width: auto`,
    // meaning "at least min-content", and this card's summary line is
    // `truncate` (`white-space: nowrap`) — so min-content is the *whole*
    // unwrapped sentence. A cluster whose last investigation produced a long
    // health message ("No cluster read succeeded... Grant 'x' get/list on...")
    // stretched this item to 2,511px inside a 1,032px track and scrolled the
    // entire page, sidebar included. Every element inside already had it; the
    // grid item that actually needed it did not.
    <li className="flex min-w-0 items-stretch gap-3 rounded-lg border border-line bg-surface p-4">
      <span aria-hidden="true" className={`w-0.5 shrink-0 rounded-full ${STRIPE[row.state]}`} />
      {row.investigationId ? (
        <Link
          to={`/clusters/${encodeURIComponent(row.name)}`}
          className="flex min-w-0 flex-1 rounded transition-colors duration-fast hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
        >
          {body}
        </Link>
      ) : (
        <div className="flex min-w-0 flex-1 items-center justify-between gap-4">
          {body}
          <button
            type="button"
            onClick={() => onInvestigate(row.name)}
            className="shrink-0 rounded-md border border-line bg-raised px-3 py-1.5 text-sm transition-colors duration-fast hover:border-info hover:text-info focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
          >
            Investigate
          </button>
        </div>
      )}
    </li>
  );
}
