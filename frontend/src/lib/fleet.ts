/**
 * Fleet state, derived from investigations that have already been stored.
 *
 * There is no background watcher and no polling of clusters. What a fleet view
 * shows is the aggregate of what has been investigated, timestamped, with
 * staleness visible — putting a live-looking number on screen that nothing
 * refreshes would be worse than showing nothing.
 */

import type { InvestigationHistoryItem, KubernetesContext } from "../types/investigation";
import { severityTone, type SeverityTone } from "./report";

/** Beyond this, a cluster's last investigation is too old to speak for it. */
export const STALE_AFTER_MS = 24 * 60 * 60 * 1000;

export type FleetState =
  | SeverityTone
  | "stale"
  /** Investigated, but the cluster could not be read. */
  | "unreadable"
  /** Never investigated at all. */
  | "unknown";

export interface ClusterState {
  name: string;
  /** The kubeconfig cluster this context points at, when known. */
  cluster: string;
  state: FleetState;
  severity: string;
  rootCause: string;
  namespace: string;
  confidence: number;
  investigationId: string;
  at: string;
  ageMs: number | null;
}

const ORDER: Record<FleetState, number> = {
  critical: 0,
  // A cluster nobody could read outranks a degraded one: the finding count is
  // not just bad, it is not trustworthy. It must never sort below healthy.
  unreadable: 1,
  warning: 2,
  stale: 3,
  unknown: 4,
  healthy: 5,
  neutral: 6,
};

/**
 * Fold history into one row per cluster.
 *
 * Attribution comes from the history entry's own `context`. Older entries
 * written before that field existed fall back to the job store, which is why
 * `jobContexts` is threaded through rather than assumed.
 */
export function fleetState(
  contexts: KubernetesContext[],
  history: InvestigationHistoryItem[],
  jobContexts: Map<string, string> = new Map(),
  now: number = Date.now(),
): ClusterState[] {
  const newest = new Map<string, InvestigationHistoryItem>();

  for (const item of history) {
    const context = item.context || jobContexts.get(item.id) || "";
    if (!context) {
      // Not attributable to a cluster. Counted separately rather than
      // assigned to an arbitrary one.
      continue;
    }
    const existing = newest.get(context);
    if (!existing || (item.timestamp ?? "") > (existing.timestamp ?? "")) {
      newest.set(context, item);
    }
  }

  const names = new Set<string>([
    ...contexts.map((context) => context.name),
    ...newest.keys(),
  ]);

  const rows: ClusterState[] = [];
  for (const name of names) {
    const item = newest.get(name);
    const cluster = contexts.find((context) => context.name === name)?.cluster ?? "";

    if (!item) {
      rows.push({
        name,
        cluster,
        state: "unknown",
        severity: "",
        rootCause: "Never investigated",
        namespace: "",
        confidence: 0,
        investigationId: "",
        at: "",
        ageMs: null,
      });
      continue;
    }

    const at = Date.parse(item.timestamp ?? "");
    const ageMs = Number.isNaN(at) ? null : Math.max(0, now - at);
    // "Unknown" is what the backend reports when the collectors that produce
    // findings did not run — distinct from a cluster never investigated.
    const tone: FleetState =
      (item.severity ?? "").toLowerCase() === "unknown"
        ? "unreadable"
        : severityTone(item.severity);

    rows.push({
      name,
      cluster,
      // Staleness outranks a healthy verdict: a cluster investigated six days
      // ago is unknown, not healthy, and rendering unknown as green is lying
      // by omission.
      state: ageMs !== null && ageMs > STALE_AFTER_MS ? "stale" : tone,
      severity: item.severity ?? "",
      rootCause: item.root_cause ?? "",
      namespace: item.namespace ?? "",
      confidence: item.confidence ?? 0,
      investigationId: item.id,
      at: item.timestamp ?? "",
      ageMs,
    });
  }

  return rows.sort(compare);
}

/**
 * Worst first, then stalest, then by name.
 *
 * Never alphabetical by default. The default sort of an operations surface is
 * a statement about what the product thinks matters.
 */
function compare(a: ClusterState, b: ClusterState): number {
  if (ORDER[a.state] !== ORDER[b.state]) {
    return ORDER[a.state] - ORDER[b.state];
  }
  if (a.ageMs !== b.ageMs) {
    if (a.ageMs === null) return -1;
    if (b.ageMs === null) return 1;
    return b.ageMs - a.ageMs;
  }
  return a.name.localeCompare(b.name);
}

export function rollup(rows: ClusterState[]): Record<FleetState, number> {
  const counts: Record<FleetState, number> = {
    critical: 0,
    unreadable: 0,
    warning: 0,
    healthy: 0,
    neutral: 0,
    stale: 0,
    unknown: 0,
  };
  for (const row of rows) {
    counts[row.state] += 1;
  }
  return counts;
}

export interface SignalCluster {
  type: string;
  summary: string;
  severity: string;
  clusters: string[];
}

/**
 * The same failure, seen on more than one cluster.
 *
 * The highest-value question at fleet scale is not "how is cluster X" but
 * "what is wrong across many at once" — a bad node image, an expiring registry
 * credential. Signals carry stable type prefixes, so this is a group-by over
 * stored reports rather than a model call, and no single investigation can see
 * it.
 */
export function correlateSignals(
  perCluster: Array<{ cluster: string; signals: Array<{ type: string; summary: string; severity: string }> }>,
): SignalCluster[] {
  const groups = new Map<string, SignalCluster>();

  for (const { cluster, signals } of perCluster) {
    for (const signal of signals) {
      if (!signal.type) {
        continue;
      }
      const group = groups.get(signal.type) ?? {
        type: signal.type,
        summary: signal.summary,
        severity: signal.severity,
        clusters: [],
      };
      if (!group.clusters.includes(cluster)) {
        group.clusters.push(cluster);
      }
      groups.set(signal.type, group);
    }
  }

  return [...groups.values()]
    .filter((group) => group.clusters.length > 1)
    .sort((a, b) => {
      if (a.clusters.length !== b.clusters.length) {
        return b.clusters.length - a.clusters.length;
      }
      return ORDER[severityTone(a.severity)] - ORDER[severityTone(b.severity)];
    });
}

/** "4h ago", "6d ago" — short enough for a dense row. */
export function relativeAge(ageMs: number | null): string {
  if (ageMs === null) {
    return "";
  }
  const minutes = Math.floor(ageMs / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
