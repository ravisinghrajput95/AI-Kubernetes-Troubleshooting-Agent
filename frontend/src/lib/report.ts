/**
 * The composed incident report, and the rules for rendering it.
 *
 * `IncidentReportComposer` builds one composition and the PDF, Markdown and
 * JSON writers all render *that*, so the formats cannot disagree. The console
 * becomes the fourth renderer of the same thing: same sections, same order,
 * same omit-when-empty rule.
 *
 * One qualification worth stating plainly, because the design document did not.
 * The composition is **pre-flattened to strings** — that is what the PDF and
 * Markdown writers need, and it means the composition alone cannot carry
 * evidence ids. So the console takes its *spine* from the composition and
 * enriches the sections where interaction matters from the structured
 * `diagnosis` and `investigation` payloads it already has. Sections and their
 * order still cannot drift from the report; only the rendering of a few of them
 * is richer on screen than on paper.
 */

import type { Diagnosis, EvidenceEntry, InvestigationResponse } from "../types/investigation";

type Investigation = InvestigationResponse["investigation"];

export interface ReportField {
  label: string;
  value: string;
}

export interface ReportSection {
  title: string;
  body: string[];
  fields: ReportField[];
  table: string[][];
  headers: string[];
  note: string;
}

export interface IncidentComposition {
  incident_id: string;
  title: string;
  generated_at: string;
  sections: ReportSection[];
}

/** Severity tokens. Never colour alone — each pairs with a glyph and a label. */
export type SeverityTone = "critical" | "warning" | "healthy" | "neutral";

const CRITICAL = new Set(["critical", "high", "failed", "error"]);
const WARNING = new Set(["warning", "medium", "degraded", "unavailable", "timeout", "forbidden"]);
const HEALTHY = new Set(["healthy", "ok", "low", "succeeded", "resolved", "empty"]);

export function severityTone(value: string | undefined): SeverityTone {
  const key = (value ?? "").trim().toLowerCase();
  if (CRITICAL.has(key)) return "critical";
  if (WARNING.has(key)) return "warning";
  if (HEALTHY.has(key)) return "healthy";
  return "neutral";
}

/**
 * Evidence that was collected but could not be used.
 *
 * `not_applicable` is deliberately *not* a gap: it means the record did not
 * apply to this cluster, and the coverage ratio excludes it. Blurring the two
 * would make an undeployed Prometheus look like a failure to look.
 */
export function isGap(status: string): boolean {
  return !["ok", "empty", "not_applicable"].includes(status);
}

export function evidenceIndex(investigation?: Investigation): Map<string, EvidenceEntry> {
  const index = new Map<string, EvidenceEntry>();
  for (const record of investigation?.evidence ?? []) {
    index.set(record.id, record);
  }
  return index;
}

/**
 * Evidence ids the diagnosis actually rested on.
 *
 * Taken from `cited_evidence`, which the grounding validator populates after
 * stripping citations that did not resolve. A claim with nothing here renders
 * with no chip, and the absence is informative rather than an oversight.
 */
export function citationsFor(diagnosis?: Diagnosis): string[] {
  return (diagnosis?.cited_evidence ?? []).filter(Boolean);
}

/** Evidence ids behind one signal, in the order the backend reported them. */
export function citationsForSignal(
  diagnosis: Diagnosis | undefined,
  signalId: string,
): string[] {
  const signal = (diagnosis?.signals ?? []).find((item) => item.id === signalId);
  return signal?.evidence_ids ?? [];
}

/**
 * Which conclusions a given evidence record supports.
 *
 * The reverse of a citation, shown in the inspector so an operator can see what
 * rests on the record they are looking at.
 */
export function citedBy(diagnosis: Diagnosis | undefined, evidenceId: string): string[] {
  const references: string[] = [];
  if (citationsFor(diagnosis).includes(evidenceId)) {
    references.push("Root cause");
  }
  for (const signal of diagnosis?.signals ?? []) {
    if (signal.evidence_ids?.includes(evidenceId)) {
      references.push(signal.summary || signal.id);
    }
  }
  return references;
}

/**
 * Sections the console renders itself rather than as generic text.
 *
 * Everything else falls through to the generic renderer, so a section added to
 * the composer appears on screen without a frontend change — which is the
 * property that keeps the screen and the postmortem in step.
 */
export const ENRICHED_SECTIONS = new Set(["Root Cause", "Evidence", "Confidence Assessment"]);

/** Sections whose prose a model wrote, and which are labelled as such. */
export const MODEL_AUTHORED_SECTIONS = new Set([
  "Resolution",
  "Lessons Learned",
  "Preventive Actions",
]);

export function isCommandLine(line: string): boolean {
  const text = line.trim();
  return text.startsWith("kubectl ") || text.startsWith("$ ");
}
