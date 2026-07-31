import { Citations } from "./CitationChip";
import { SeverityDot } from "./SeverityDot";
import {
  citationsFor,
  citationsForSignal,
  evidenceIndex,
  isCommandLine,
  isGap,
  MODEL_AUTHORED_SECTIONS,
  severityTone,
  type IncidentComposition,
  type ReportSection,
} from "../../lib/report";
import type { Diagnosis, InvestigationResponse } from "../../types/investigation";

type Investigation = InvestigationResponse["investigation"];

/**
 * An investigation, rendered as the report it is.
 *
 * The section list and its order come from the composition the PDF and
 * Markdown writers use, so a section added to the composer appears here without
 * a frontend change and the screen cannot drift from the postmortem. Sections
 * where interaction matters are rendered from the structured payload instead of
 * the composition's flattened lines — see `src/lib/report.ts` for why.
 */
export function ReportDocument({
  composition,
  diagnosis,
  investigation,
  selectedEvidence,
  onSelectEvidence,
}: {
  composition?: IncidentComposition;
  diagnosis?: Diagnosis;
  investigation?: Investigation;
  selectedEvidence: string;
  onSelectEvidence: (id: string) => void;
}) {
  if (!composition) {
    return null;
  }

  const index = evidenceIndex(investigation);
  const shared = { diagnosis, investigation, index, selectedEvidence, onSelectEvidence };

  return (
    <article className="grid gap-8">
      {composition.sections.map((section) => (
        <section key={section.title} aria-labelledby={sectionId(section.title)}>
          <h2 id={sectionId(section.title)} className="text-h2">
            {section.title}
          </h2>
          <div className="mt-3">
            {section.title === "Root Cause" ? (
              <RootCause section={section} {...shared} />
            ) : section.title === "Evidence" ? (
              <EvidenceSection section={section} {...shared} />
            ) : section.title === "Confidence Assessment" ? (
              <Confidence section={section} diagnosis={diagnosis} />
            ) : (
              <GenericSection section={section} />
            )}
          </div>
        </section>
      ))}
    </article>
  );
}

function sectionId(title: string): string {
  return `section-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
}

type Shared = {
  diagnosis?: Diagnosis;
  investigation?: Investigation;
  index: ReturnType<typeof evidenceIndex>;
  selectedEvidence: string;
  onSelectEvidence: (id: string) => void;
};

/** The claim, with the evidence it rests on attached to it. */
function RootCause({
  section,
  diagnosis,
  index,
  selectedEvidence,
  onSelectEvidence,
}: { section: ReportSection } & Shared) {
  const cited = citationsFor(diagnosis);

  return (
    <>
      {section.body.map((line, position) => (
        <p key={line} className="max-w-measure text-body text-ink">
          {line}
          {position === 0 ? (
            <Citations
              ids={cited}
              index={index}
              selected={selectedEvidence}
              onSelect={onSelectEvidence}
            />
          ) : null}
        </p>
      ))}

      {cited.length === 0 ? (
        <p className="mt-3 text-sm leading-6 text-ink-3">
          No evidence was cited for this conclusion.
        </p>
      ) : null}

      {section.table.length > 0 ? (
        <Table section={section} className="mt-4" />
      ) : null}
      <Note section={section} />
    </>
  );
}

/** Coverage, gaps, and the signals behind the diagnosis — each citable. */
function EvidenceSection({
  section,
  diagnosis,
  investigation,
  index,
  selectedEvidence,
  onSelectEvidence,
}: { section: ReportSection } & Shared) {
  const coverage = investigation?.evidence_coverage;
  const signals = diagnosis?.signals ?? [];
  const records = investigation?.evidence ?? [];
  const gaps = records.filter((record) => isGap(record.status));

  return (
    <>
      {coverage ? (
        <p className="max-w-measure text-body text-ink-2">
          {coverage.usable} of {coverage.total} records were usable
          {coverage.completeness ? `, ${coverage.completeness}% coverage` : ""}.
        </p>
      ) : null}

      {signals.length > 0 ? (
        <ul className="mt-4 grid gap-2">
          {signals.map((signal) => (
            <li key={signal.id} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <SeverityDot tone={severityTone(signal.severity)} />
              <span className="text-sm text-ink">{signal.summary}</span>
              <Citations
                ids={citationsForSignal(diagnosis, signal.id)}
                index={index}
                selected={selectedEvidence}
                onSelect={onSelectEvidence}
              />
            </li>
          ))}
        </ul>
      ) : null}

      {gaps.length > 0 ? (
        <>
          <p className="mt-5 text-label uppercase text-ink-3">
            Gaps — what could not be seen
          </p>
          <ul className="mt-2 grid gap-2">
            {gaps.map((record) => (
              <li key={record.id} className="flex flex-wrap items-baseline gap-x-2">
                <SeverityDot tone={severityTone(record.status)} label={record.status} />
                <button
                  type="button"
                  onClick={() => onSelectEvidence(record.id)}
                  className="text-left font-mono text-sm text-ink-2 underline decoration-line underline-offset-4 transition-colors duration-fast hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
                >
                  {record.kind}
                </button>
                <span className="text-sm text-ink-3">{record.detail}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {signals.length === 0 && gaps.length === 0 ? (
        <GenericSection section={section} />
      ) : null}
    </>
  );
}

/** Confidence as a composition, not a number asserted on its own. */
function Confidence({
  section,
  diagnosis,
}: {
  section: ReportSection;
  diagnosis?: Diagnosis;
}) {
  const components = diagnosis?.confidence_breakdown ?? [];
  const total = diagnosis?.confidence ?? 0;
  const tones = ["bg-healthy", "bg-info", "bg-warning", "bg-ai"];

  if (components.length === 0) {
    return <GenericSection section={section} />;
  }

  return (
    <>
      <p className="font-mono text-h1 text-ink">{total}%</p>

      <div className="mt-3 flex h-1.5 max-w-measure overflow-hidden rounded-full bg-line">
        {components.map((item, position) => (
          <span
            key={item.component}
            className={tones[position % tones.length]}
            style={{ width: `${Math.max(0, item.contribution)}%` }}
          />
        ))}
      </div>

      <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1">
        {components.map((item, position) => (
          <li key={item.component} className="flex items-center gap-2 text-sm text-ink-3">
            <span
              aria-hidden="true"
              className={`size-2 rounded-sm ${tones[position % tones.length]}`}
            />
            {item.component} {item.score}% × weight {item.weight}%
          </li>
        ))}
      </ul>
      <Note section={section} />
    </>
  );
}

/**
 * Every other section, rendered from the composition as-is.
 *
 * A new section in the composer arrives here for free. Prose a model wrote is
 * labelled — `fix`, `prevention` and `next_steps` are the only fields the
 * backend does not compute deterministically, and commands never are.
 */
function GenericSection({ section }: { section: ReportSection }) {
  const modelAuthored = MODEL_AUTHORED_SECTIONS.has(section.title);

  return (
    <div
      className={
        modelAuthored ? "rounded-lg border border-ai/30 bg-ai/[0.04] p-4" : undefined
      }
    >
      {section.fields.length > 0 ? (
        <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {section.fields.map((field) => (
            <div key={field.label} className="flex items-baseline gap-3">
              <dt className="w-40 shrink-0 text-sm text-ink-3">{field.label}</dt>
              <dd className="min-w-0 text-sm text-ink-2">{field.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {section.body.length > 0 ? (
        <div className={section.fields.length > 0 ? "mt-4 grid gap-2" : "grid gap-2"}>
          {section.body.map((line) =>
            isCommandLine(line) ? (
              <pre
                key={line}
                className="overflow-x-auto rounded-md border border-line bg-canvas p-3 font-mono text-sm text-ink-2"
              >
                {line.replace(/^\$ /, "")}
              </pre>
            ) : (
              <p key={line} className="max-w-measure text-body text-ink-2">
                {line}
              </p>
            ),
          )}
        </div>
      ) : null}

      {section.table.length > 0 ? <Table section={section} className="mt-4" /> : null}
      <Note section={section} />

      {modelAuthored ? (
        <p className="mt-4 font-mono text-sm text-ai">
          ◆ Model-authored · not evidence-derived
        </p>
      ) : null}
    </div>
  );
}

function Table({ section, className = "" }: { section: ReportSection; className?: string }) {
  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full min-w-[420px] border-collapse text-left">
        {section.headers.length > 0 ? (
          <thead>
            <tr>
              {section.headers.map((header, position) => (
                <th
                  key={`${header}-${position}`}
                  scope="col"
                  className="border-b border-line pb-2 pr-4 text-label uppercase text-ink-3"
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
        ) : null}
        <tbody>
          {section.table.map((row, rowIndex) => (
            <tr key={`${row.join("|")}-${rowIndex}`} className="border-b border-line-muted">
              {row.map((cell, cellIndex) => (
                <td
                  key={`${cell}-${cellIndex}`}
                  className={`py-2 pr-4 text-sm ${
                    cellIndex === 0 ? "whitespace-nowrap text-ink-2" : "text-ink-2"
                  }`}
                >
                  {cellIndex === 0 && severityTone(cell) !== "neutral" ? (
                    <SeverityDot tone={severityTone(cell)} label={cell} />
                  ) : (
                    cell
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Note({ section }: { section: ReportSection }) {
  if (!section.note) {
    return null;
  }
  return (
    <p className="mt-4 max-w-measure border-l-2 border-line pl-4 text-sm leading-6 text-ink-3">
      {section.note}
    </p>
  );
}
