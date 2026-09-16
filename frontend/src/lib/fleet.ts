/**
 * Fleet state, derived from investigations that have already been stored.
 *
 * There is no background watcher and no polling of clusters. What a fleet view
 * shows is the aggregate of what has been investigated, timestamped, with
 * staleness visible — putting a live-looking number on screen that nothing
 * refreshes would be worse than showing nothing.
 */

import type {
  AgentStatus,
  ClusterConnection,
  InvestigationHistoryItem,
  KubernetesContext,
} from "../types/investigation";
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
  /** How the cluster is reached. Independent of its investigation state. */
  connection: ClusterConnection;
  /** Present only when an agent is connected for this cluster. */
  agent: AgentStatus | null;
  /**
   * What the run behind this row was asked about, when it was not the whole
   * cluster — "" otherwise. Only set when no whole-cluster run exists.
   */
  scope: string;
  /** Other names whose runs read the same nodes as this one. */
  sameAs: string[];
}

/**
 * Whether a run read the whole cluster.
 *
 * An entry written before scope was recorded counts as whole — that is how
 * every view treated every entry before, and it cannot be known otherwise.
 */
export function isWholeCluster(item: Pick<InvestigationHistoryItem, "scope">): boolean {
  const scope = item.scope;
  if (!scope) return true;
  const namespaced = Boolean(scope.namespace) && scope.namespace !== "all";
  const resource = Boolean(scope.resource_name) && scope.resource_kind !== "cluster";
  return !namespaced && !resource;
}

/** "deployment payments/checkout", "namespace payments", or "" for the whole cluster. */
export function describeScope(item: Pick<InvestigationHistoryItem, "scope">): string {
  if (isWholeCluster(item)) return "";
  const scope = item.scope ?? {};
  const namespace = scope.namespace && scope.namespace !== "all" ? scope.namespace : "";
  if (scope.resource_name && scope.resource_kind && scope.resource_kind !== "cluster") {
    return `${scope.resource_kind} ${namespace ? `${namespace}/` : ""}${scope.resource_name}`;
  }
  return `namespace ${namespace}`;
}

/**
 * The run a view of the whole cluster should speak from: the newest one that
 * read the whole cluster, else the newest of any scope.
 *
 * Taking simply the newest made an investigation of one deployment the
 * cluster's headline on the fleet page and its seven pods the cluster's
 * capacity on the cluster page.
 */
export function representativeRun<T extends InvestigationHistoryItem>(runs: T[]): T | undefined {
  const newestFirst = [...runs].sort((a, b) => (b.timestamp ?? "").localeCompare(a.timestamp ?? ""));
  return newestFirst.find(isWholeCluster) ?? newestFirst[0];
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
  const byContext = new Map<string, InvestigationHistoryItem[]>();

  for (const item of history) {
    const context = item.context || jobContexts.get(item.id) || "";
    if (!context) {
      // Not attributable to a cluster. Counted separately rather than
      // assigned to an arbitrary one.
      continue;
    }
    byContext.set(context, [...(byContext.get(context) ?? []), item]);
  }

  const newest = new Map<string, InvestigationHistoryItem>();
  for (const [context, runs] of byContext) {
    const run = representativeRun(runs);
    if (run) newest.set(context, run);
  }

  const names = new Set<string>([
    ...contexts.map((context) => context.name),
    ...newest.keys(),
  ]);

  const key = clusterKeys(history);
  const rows: ClusterState[] = [];
  for (const name of names) {
    const sameAs = sameClusterAs(name, [...names], key);
    const item = newest.get(name);
    const context = contexts.find((entry) => entry.name === name);
    const cluster = context?.cluster ?? "";
    // Connection is *not* a fleet state: an agent-connected cluster can be
    // critical, and a healthy one can be reached by kubeconfig. Keeping them
    // separate is what stops "connected" from reading as "fine".
    const connection: ClusterConnection = context?.connection ?? "kubeconfig";
    const agent = context?.agent ?? null;

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
        connection,
        agent,
        scope: "",
        sameAs,
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
      connection,
      agent,
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
      scope: describeScope(item),
      sameAs,
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

/**
 * Which cluster each name reaches, by the nodes its runs read.
 *
 * A cluster is named by whatever reaches it: a kubeconfig context, an agent's
 * enrolment id. Enrolling an agent for a cluster already read through a
 * kubeconfig is the documented way onto agents, and a sweep of the console
 * found one kind cluster, reached three ways, reported as "the same failure on
 * 3 clusters, counted as one incident". Node UIDs are assigned by the API
 * server, so names whose runs share one are the same cluster. Returns each
 * name's group key — the alphabetically first name in its group — and a name
 * with no recorded nodes is its own group, which is the direction that was
 * already assumed.
 */
export function clusterKeys(history: InvestigationHistoryItem[]): (name: string) => string {
  const parent = new Map<string, string>();
  const find = (name: string): string => {
    let root = name;
    while (parent.has(root) && parent.get(root) !== root) root = parent.get(root) as string;
    return root;
  };
  const union = (a: string, b: string) => {
    const [ra, rb] = [find(a), find(b)];
    if (ra === rb) return;
    const [keep, fold] = ra < rb ? [ra, rb] : [rb, ra];
    parent.set(keep, keep);
    parent.set(fold, keep);
  };

  const owner = new Map<string, string>();
  for (const item of history) {
    if (!item.context) continue;
    for (const uid of item.node_uids ?? []) {
      const seen = owner.get(uid);
      if (seen === undefined) owner.set(uid, item.context);
      else union(seen, item.context);
    }
  }
  return (name: string) => find(name);
}

/**
 * "3 clusters", or "3 names for 1 cluster" when some of them read the same
 * nodes.
 *
 * The cards already say "Reads the same nodes as …", and the header above them
 * still counted names: one kind cluster reached through its kubeconfig and two
 * agents read "3 clusters" over three cards each saying it was the other two.
 * A name with no recorded nodes counts as its own cluster, the same direction
 * `clusterKeys` takes.
 */
export function describeClusterCount(names: string[], key: (name: string) => string): string {
  const clusters = new Set(names.map(key)).size;
  const noun = (count: number) => (count === 1 ? "cluster" : "clusters");
  if (clusters === names.length) return `${names.length} ${noun(names.length)}`;
  return `${names.length} names for ${clusters} ${noun(clusters)}`;
}

/** The other names that reach the same nodes as `name`. */
export function sameClusterAs(
  name: string,
  names: string[],
  key: (name: string) => string,
): string[] {
  return names.filter((other) => other !== name && key(other) === key(name)).sort();
}

export interface SignalCluster {
  type: string;
  summary: string;
  severity: string;
  clusters: string[];
  /** How many clusters those names are; fewer when names share nodes. */
  distinct: number;
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
  key: (name: string) => string = (name) => name,
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
        distinct: 0,
      };
      if (!group.clusters.includes(cluster)) {
        group.clusters.push(cluster);
      }
      groups.set(signal.type, group);
    }
  }

  return [...groups.values()]
    .map((group) => ({ ...group, distinct: new Set(group.clusters.map(key)).size }))
    .filter((group) => group.distinct > 1)
    .sort((a, b) => {
      if (a.distinct !== b.distinct) {
        return b.distinct - a.distinct;
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
