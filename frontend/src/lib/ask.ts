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
  signals: Array<{ type: string; summary: string; severity: string }>;
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
export function recurringFindings(corpus: CorpusEntry[]): Finding[] {
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
      if (entry.cluster && !existing.clusters.includes(entry.cluster)) {
        existing.clusters.push(entry.cluster);
      }
      if (entry.at < existing.firstSeen) existing.firstSeen = entry.at;
      if (entry.at > existing.lastSeen) existing.lastSeen = entry.at;
    }
  }

  return [...groups.values()]
    .filter((finding) => finding.occurrences.length > 1)
    .map((finding) => ({ ...finding, trend: trendOf(finding.occurrences) }))
    .sort((a, b) => {
      if (a.clusters.length !== b.clusters.length) {
        return b.clusters.length - a.clusters.length;
      }
      if (a.occurrences.length !== b.occurrences.length) {
        return b.occurrences.length - a.occurrences.length;
      }
      return b.lastSeen.localeCompare(a.lastSeen);
    });
}

/**
 * Which half of its own history a finding is concentrated in.
 *
 * Deliberately refuses to answer below three occurrences. Calling two data
 * points a trend is exactly the kind of confident overclaim this product
 * exists not to make.
 */
export function trendOf(occurrences: Occurrence[]): Trend {
  if (occurrences.length < MIN_FOR_TREND) {
    return "unknown";
  }

  const times = occurrences.map((item) => Date.parse(item.at)).filter((n) => !Number.isNaN(n));
  if (times.length < MIN_FOR_TREND) {
    return "unknown";
  }

  const earliest = Math.min(...times);
  const latest = Math.max(...times);
  if (latest === earliest) {
    return "steady";
  }

  const midpoint = earliest + (latest - earliest) / 2;
  const recent = times.filter((time) => time > midpoint).length;
  const earlier = times.length - recent;

  if (recent > earlier) return "rising";
  if (recent < earlier) return "falling";
  return "steady";
}

/** Findings that appeared on more than one cluster. */
export function sharedAcrossClusters(findings: Finding[]): Finding[] {
  return findings.filter((finding) => finding.clusters.length > 1);
}

/**
 * Findings that recurred, but only ever on one cluster.
 *
 * The complement of `sharedAcrossClusters`, so the two lists are disjoint. A
 * finding shown under both headings is the same answer printed twice, and the
 * reader has to work out that it is not two problems.
 */
export function recurredOnOneCluster(findings: Finding[]): Finding[] {
  return findings.filter((finding) => finding.clusters.length <= 1);
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
