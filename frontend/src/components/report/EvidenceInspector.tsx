import { X } from "lucide-react";

import { SeverityDot } from "./SeverityDot";
import { citedBy, evidenceTone, isGap } from "../../lib/report";
import type { Diagnosis, EvidenceEntry } from "../../types/investigation";

/**
 * One evidence record, in full.
 *
 * A panel and never a route: inspecting evidence must not cost the reader their
 * place in the document. Its selection lives in a query parameter, so the view
 * is still shareable.
 */
export function EvidenceInspector({
  evidence,
  diagnosis,
  onClose,
}: {
  evidence?: EvidenceEntry;
  diagnosis?: Diagnosis;
  onClose: () => void;
}) {
  if (!evidence) {
    return (
      <div className="p-4">
        <p className="text-label uppercase text-ink-3">Evidence</p>
        <p className="mt-3 text-sm leading-6 text-ink-3">
          Select a citation to inspect the record it refers to.
        </p>
      </div>
    );
  }

  const gap = isGap(evidence.status);
  const supports = citedBy(diagnosis, evidence.id);

  return (
    <div className="flex h-full flex-col">
      <div className="sticky top-0 flex items-start justify-between gap-3 border-b border-line-muted bg-surface/95 p-4 backdrop-blur">
        <div className="min-w-0">
          <p className="text-label uppercase text-ink-3">Evidence</p>
          <p className="mt-1 break-all font-mono text-sm text-ink">{evidence.id}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close evidence"
          className="shrink-0 rounded p-1 text-ink-3 transition-colors duration-fast hover:bg-raised hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <dl className="grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-2 text-sm">
          <dt className="font-mono text-sm text-ink-3">status</dt>
          <dd>
            <SeverityDot tone={evidenceTone(evidence.status)} label={evidence.status} />
          </dd>

          <dt className="font-mono text-sm text-ink-3">kind</dt>
          <dd className="min-w-0 break-words text-ink-2">{evidence.kind}</dd>

          <dt className="font-mono text-sm text-ink-3">source</dt>
          <dd className="text-ink-2">{evidence.source}</dd>

          {evidence.collected_at ? (
            <>
              <dt className="font-mono text-sm text-ink-3">at</dt>
              <dd className="text-ink-2">{evidence.collected_at}</dd>
            </>
          ) : null}

          {evidence.duration_ms ? (
            <>
              <dt className="font-mono text-sm text-ink-3">took</dt>
              <dd className="text-ink-2">{evidence.duration_ms}ms</dd>
            </>
          ) : null}
        </dl>

        {evidence.detail ? (
          <>
            <p className="mt-5 text-label uppercase text-ink-3">
              {gap ? "Why this is missing" : "Detail"}
            </p>
            <p className="mt-2 text-sm leading-6 text-ink-2">{evidence.detail}</p>
          </>
        ) : null}

        {evidence.command ? (
          <>
            <p className="mt-5 text-label uppercase text-ink-3">Originating command</p>
            <pre className="mt-2 overflow-x-auto rounded-md border border-line bg-canvas p-3 font-mono text-sm text-ink-2">
              {evidence.command}
            </pre>
          </>
        ) : null}

        {evidence.redacted ? (
          <p className="mt-4 text-sm leading-6 text-warning">
            Secrets were scrubbed from this record at collection, before it
            reached storage, the API or the model.
          </p>
        ) : null}

        {supports.length > 0 ? (
          <>
            <p className="mt-5 text-label uppercase text-ink-3">Cited by</p>
            <ul className="mt-2 grid gap-1">
              {supports.map((reference) => (
                <li key={reference} className="text-sm leading-6 text-ink-2">
                  · {reference}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </div>
  );
}
