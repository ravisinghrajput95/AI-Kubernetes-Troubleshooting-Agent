/**
 * Questions no single investigation can answer.
 *
 * Reasoning about *one* investigation belongs inline with it and stays there.
 * Reasoning *across* investigations — has this happened before, which clusters
 * share it, is it getting worse — had no home, and the data to answer it is
 * already stored.
 *
 * Everything here is a query, not a model call. Signals carry stable type
 * prefixes, so recurrence and correlation are a group-by over reports the
 * console already fetches. That ordering is deliberate: the deterministic layer
 * is the product, and a natural-language front door is an interface to it
 * later, which is the same architecture the investigation engine already uses.
 *
 * **The grounding contract applies here unchanged, and structurally.** An
 * answer is only ever a list of occurrences that were actually found; there is
 * no path that produces a sentence without the investigations behind it. This
 * is the most obvious place in the product to accidentally build the
 * hallucination surface everything else was engineered to prevent — an
 * ungrounded answer here would speak with the authority of the whole corpus
 * rather than one run.
 */

import { severityTone, type SeverityTone } from "./report";

export interface CorpusEntry {
  investigationId: string;
  cluster: string;
  at: string;
  rootCause: string;
  signals: Array<{ type: string; summary: string; severity: string; namespace?: string }>;
  /**
   * Whether this run read the cluster at all. A run that collected nothing
   * found nothing, and that is not the same as a finding being absent.
   * Absent on older callers, and treated as true.
   */
  collected?: boolean;
  /** What the run was asked about; absent means the whole cluster. */
  scope?: { namespace?: string; resource_kind?: string; resource_name?: string };
}

/**
 * Whether a run could have raised a finding about these namespaces.
 *
 * A trend's denominator is the runs where a finding *could* have appeared. On a
 * live console every finding read "happening more often", because the early
 * half held runs whose agent could not reach its API server — nothing
 * collected, so nothing found — and runs scoped to one deployment, which never
 * read the pods the finding was about. Both were counted as the finding being
 * absent. A resource-scoped run is treated as seeing nothing it did not
 * report, which is the conservative reading: the pod and deployment reads are
 * narrowed to that resource.
 */
function couldSee(entry: CorpusEntry, namespaces: string[]): boolean {
  if (entry.collected === false) return false;
  const scope = entry.scope ?? {};
  if (scope.resource_name && scope.resource_kind && scope.resource_kind !== "cluster") {
    return false;
  }
  if (!scope.namespace || scope.namespace === "all") return true;
  return namespaces.length > 0 && namespaces.every((namespace) => namespace === scope.namespace);
}

export interface Occurrence {
  investigationId: string;
  cluster: string;
  at: string;
}

export type Trend = "rising" | "falling" | "steady" | "unknown";

export interface Finding {
  type: string;
  summary: string;
  severity: string;
  tone: SeverityTone;
  occurrences: Occurrence[];
  clusters: string[];
  /** Namespaces its signals named; empty for cluster-level findings. */
  namespaces: string[];
  /** How many clusters `clusters` names; fewer when names reach the same nodes. */
  distinct: number;
  firstSeen: string;
  lastSeen: string;
  trend: Trend;
}

/** Occurrences below this are too few to claim a direction from. */
const MIN_FOR_TREND = 3;

/**
 * Findings seen more than once, newest activity first.
 *
 * One occurrence is an incident, not a pattern, so it is not reported here —
 * it is already on its own investigation page.
 */
export function recurringFindings(
  corpus: CorpusEntry[],
  key: (cluster: string) => string = (cluster) => cluster,
): Finding[] {
  const groups = new Map<string, Finding>();

  for (const entry of corpus) {
    for (const signal of entry.signals) {
      if (!signal.type) {
        continue;
      }
      const existing = groups.get(signal.type);
      const occurrence: Occurrence = {
        investigationId: entry.investigationId,
        cluster: entry.cluster,
        at: entry.at,
      };

      if (!existing) {
        groups.set(signal.type, {
          type: signal.type,
          summary: signal.summary,
          severity: signal.severity,
          tone: severityTone(signal.severity),
          occurrences: [occurrence],
          clusters: entry.cluster ? [entry.cluster] : [],
          namespaces: signal.namespace ? [signal.namespace] : [],
          distinct: 0,
          firstSeen: entry.at,
          lastSeen: entry.at,
          trend: "unknown",
        });
        continue;
      }

      // One investigation contributes one occurrence, however many signals of
      // the same type it raised — otherwise a noisy run looks like a trend.
      if (!existing.occurrences.some((item) => item.investigationId === entry.investigationId)) {
        existing.occurrences.push(occurrence);
      }
      if (signal.namespace && !existing.namespaces.includes(signal.namespace)) {
        existing.namespaces.push(signal.namespace);
      }
      if (entry.cluster && !existing.clusters.includes(entry.cluster)) {
        existing.clusters.push(entry.cluster);
      }
      if (entry.at < existing.firstSeen) existing.firstSeen = entry.at;
      if (entry.at > existing.lastSeen) existing.lastSeen = entry.at;
    }
  }

  // Every run that found it, plus every run that could have and did not.
  const runsOf = (finding: Finding): Occurrence[] =>
    corpus
      .filter(
        (entry) =>
          finding.clusters.includes(entry.cluster) &&
          (finding.occurrences.some((item) => item.investigationId === entry.investigationId) ||
            couldSee(entry, finding.namespaces)),
      )
      .map((entry) => ({
        investigationId: entry.investigationId,
        cluster: entry.cluster,
        at: entry.at,
      }));

  return [...groups.values()]
    .filter((finding) => finding.occurrences.length > 1)
    .map((finding) => ({
      ...finding,
      distinct: new Set(finding.clusters.map(key)).size,
      trend: trendOf(finding.occurrences, runsOf(finding)),
    }))
    .sort((a, b) => {
      if (a.distinct !== b.distinct) {
        return b.distinct - a.distinct;
      }
      if (a.occurrences.length !== b.occurrences.length) {
        return b.occurrences.length - a.occurrences.length;
      }
      return b.lastSeen.localeCompare(a.lastSeen);
    });
}

/** Runs in each half of the window below this are too few to compare. */
const MIN_RUNS_PER_HALF = 2;

/**
 * Whether a finding shows up in a larger or smaller share of investigations
 * lately than it used to.
 *
 * This compared *when its occurrences fell* and nothing else — "which half of
 * its own history it is concentrated in" — which measures when people ran
 * investigations, not how often the finding appears in them. On a live corpus
 * of four runs, three in the first minute and one five minutes later, every
 * finding present in all four was labelled "happening less often". A trend is
 * a rate, and a rate needs the runs where the finding was absent: `runs` is
 * every investigation of the clusters the finding was seen on.
 *
 * Still refuses below three occurrences, and now also when either half of the
 * window holds too few runs to have a share at all.
 */
export function trendOf(occurrences: Occurrence[], runs: Occurrence[]): Trend {
  if (occurrences.length < MIN_FOR_TREND) {
    return "unknown";
  }

  const seen = new Set(occurrences.map((item) => item.investigationId));
  const timed = runs
    .map((run) => ({ at: Date.parse(run.at), present: seen.has(run.investigationId) }))
    .filter((run) => !Number.isNaN(run.at));
  if (timed.length < MIN_FOR_TREND) {
    return "unknown";
  }

  const earliest = Math.min(...timed.map((run) => run.at));
  const latest = Math.max(...timed.map((run) => run.at));
  if (latest === earliest) {
    return "unknown";
  }

  const midpoint = earliest + (latest - earliest) / 2;
  const earlier = timed.filter((run) => run.at <= midpoint);
  const recent = timed.filter((run) => run.at > midpoint);
  if (earlier.length < MIN_RUNS_PER_HALF || recent.length < MIN_RUNS_PER_HALF) {
    return "unknown";
  }

  const share = (half: typeof timed) => half.filter((run) => run.present).length / half.length;
  const before = share(earlier);
  const after = share(recent);

  if (after > before) return "rising";
  if (after < before) return "falling";
  return "steady";
}

/** Findings that appeared on more than one cluster. */
export function sharedAcrossClusters(findings: Finding[]): Finding[] {
  return findings.filter((finding) => finding.distinct > 1);
}

/**
 * Findings that recurred, but only ever on one cluster.
 *
 * The complement of `sharedAcrossClusters`, so the two lists are disjoint. A
 * finding shown under both headings is the same answer printed twice, and the
 * reader has to work out that it is not two problems.
 */
export function recurredOnOneCluster(findings: Finding[]): Finding[] {
  return findings.filter((finding) => finding.distinct <= 1);
}

/**
 * Narrow by a free-text query.
 *
 * Matching is over the finding's own text, not an interpretation of the
 * question. A query that matches nothing returns nothing, and the caller says
 * so — it never falls back to a plausible-looking answer.
 */
export function search(findings: Finding[], query: string): Finding[] {
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return findings;
  }
  return findings.filter(
    (finding) =>
      finding.type.toLowerCase().includes(needle) ||
      finding.summary.toLowerCase().includes(needle) ||
      finding.clusters.some((cluster) => cluster.toLowerCase().includes(needle)),
  );
}

export function trendLabel(trend: Trend): string {
  return {
    rising: "happening more often",
    falling: "happening less often",
    steady: "steady",
    unknown: "not enough history to say",
  }[trend];
}
