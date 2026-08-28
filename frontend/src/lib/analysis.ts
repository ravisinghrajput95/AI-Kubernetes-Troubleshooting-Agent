import type {
  CollectionCache,
  ConfidenceComponent,
  EvidenceEntry,
  EvidenceStatus,
  Hypothesis,
  ResourceRef,
  Severity,
  Signal,
} from "../types/investigation";

const SEVERITY_ORDER: Record<Severity, number> = {
  critical: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
};

export const SEVERITY_TONE: Record<Severity, string> = {
  critical: "border-red-800 bg-red-950/40 text-red-300",
  high: "border-amber-800 bg-amber-950/40 text-amber-300",
  medium: "border-yellow-800 bg-yellow-950/30 text-yellow-300",
  low: "border-sky-800 bg-sky-950/40 text-sky-300",
  info: "border-slate-700 bg-slate-900 text-slate-300",
};

export const STATUS_TONE: Record<EvidenceStatus, string> = {
  ok: "border-lime-800 bg-lime-950/40 text-lime-300",
  empty: "border-slate-700 bg-slate-900 text-slate-400",
  unavailable: "border-amber-800 bg-amber-950/40 text-amber-300",
  forbidden: "border-red-800 bg-red-950/40 text-red-300",
  timeout: "border-orange-800 bg-orange-950/40 text-orange-300",
  not_applicable: "border-slate-700 bg-slate-900 text-slate-400",
  failed: "border-red-800 bg-red-950/40 text-red-300",
};

export function severityRank(severity?: Severity): number {
  return severity ? (SEVERITY_ORDER[severity] ?? 0) : 0;
}

export function sortSignals(signals: Signal[]): Signal[] {
  return [...signals].sort((a, b) => {
    const bySeverity = severityRank(b.severity) - severityRank(a.severity);
    return bySeverity !== 0 ? bySeverity : a.id.localeCompare(b.id);
  });
}

export function formatTarget(target?: ResourceRef): string {
  if (!target) {
    return "cluster";
  }
  return target.namespace
    ? `${target.kind}/${target.namespace}/${target.name}`
    : `${target.kind}/${target.name}`;
}

/** Signals grouped by domain (`pod`, `network`, …), most severe domain first. */
export function groupSignalsByDomain(signals: Signal[]): Array<[string, Signal[]]> {
  const groups = new Map<string, Signal[]>();

  for (const signal of sortSignals(signals)) {
    const domain = signal.domain || signal.type.split(".")[0] || "other";
    groups.set(domain, [...(groups.get(domain) ?? []), signal]);
  }

  return [...groups.entries()].sort(
    (a, b) => severityRank(b[1][0]?.severity) - severityRank(a[1][0]?.severity),
  );
}

export interface EvidenceGroup {
  kind: string;
  entries: EvidenceEntry[];
  usable: number;
  degraded: number;
}

export function groupEvidenceByKind(evidence: EvidenceEntry[]): EvidenceGroup[] {
  const groups = new Map<string, EvidenceEntry[]>();

  for (const entry of evidence) {
    groups.set(entry.kind, [...(groups.get(entry.kind) ?? []), entry]);
  }

  return [...groups.entries()]
    .map(([kind, entries]) => {
      const usable = entries.filter((item) => isUsable(item.status)).length;
      return { kind, entries, usable, degraded: entries.length - usable };
    })
    // Surface problems first: kinds with degraded evidence lead.
    .sort((a, b) => b.degraded - a.degraded || a.kind.localeCompare(b.kind));
}

export function isUsable(status: EvidenceStatus): boolean {
  return status === "ok" || status === "empty";
}

export function filterEvidence(
  evidence: EvidenceEntry[],
  query: string,
  onlyDegraded: boolean,
): EvidenceEntry[] {
  const needle = query.trim().toLowerCase();

  return evidence.filter((entry) => {
    if (onlyDegraded && isUsable(entry.status)) {
      return false;
    }
    if (!needle) {
      return true;
    }
    return [entry.id, entry.kind, entry.command ?? "", formatTarget(entry.target)]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
}

export function rankHypotheses(hypotheses: Hypothesis[]): Hypothesis[] {
  return [...hypotheses].sort(
    (a, b) =>
      severityRank(b.severity) - severityRank(a.severity) ||
      b.confidence - a.confidence,
  );
}

export function signalsById(signals: Signal[]): Map<string, Signal> {
  return new Map(signals.map((signal) => [signal.id, signal]));
}

/**
 * Total of the weighted contributions. Shown next to the reported confidence so
 * a mismatch between the parts and the whole is visible rather than hidden.
 */
export function totalContribution(components: ConfidenceComponent[]): number {
  return components.reduce((sum, item) => sum + item.contribution, 0);
}

export function formatDuration(ms?: number | null): string {
  if (ms === undefined || ms === null) {
    return "—";
  }
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * What to tell an operator about reused cluster reads, or nothing.
 *
 * Returns `null` when every read was live, because a line saying "0 reused"
 * on every investigation trains people to stop reading it. When reads *were*
 * reused the age leads: the backend stamps each evidence record with the age
 * of the read behind it, so this is a summary of what the records already say
 * rather than a separate claim that could drift from them.
 */
export function describeCollectionCache(
  cache?: CollectionCache,
): { label: string; detail: string } | null {
  if (!cache || !cache.enabled || cache.hits <= 0) {
    return null;
  }
  const total = cache.hits + cache.misses;
  const age = cache.oldest_evidence_seconds;
  return {
    label: `${cache.hits} of ${total} reads reused`,
    detail:
      age === null || age === undefined
        ? "Some evidence was reused from a recent investigation of this cluster."
        : `Oldest evidence is ${formatAge(age)} old. Re-run with refresh for a live read.`,
  };
}

function formatAge(seconds: number): string {
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  return `${Math.round(seconds / 60)}m`;
}

export function humanizeKind(kind: string): string {
  return kind
    .replace(/^k8s\./, "")
    .split(".")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
