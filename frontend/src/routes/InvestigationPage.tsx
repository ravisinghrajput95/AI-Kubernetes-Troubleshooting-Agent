import { useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router";

import { ArtifactsPanel } from "../components/report/ArtifactsPanel";
import { EvidenceInspector } from "../components/report/EvidenceInspector";
import { LiveTimeline } from "../components/LiveTimeline";
import { RemediationPanel } from "../components/RemediationPanel";
import { ReportDocument } from "../components/report/ReportDocument";
import { SeverityDot } from "../components/report/SeverityDot";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useInvestigationJob } from "../hooks/useInvestigationJob";
import { evidenceIndex, severityTone } from "../lib/report";
import { getInvestigationReport } from "../services/api";

/**
 * One investigation, at its own address.
 *
 * Renders a run that is still collecting, one that has finished, and one that
 * has been evicted from the job store and is served from its persisted report
 * — the backend answers all three from the same id, so this page does too.
 */
export function InvestigationPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const job = useInvestigationJob();
  const { attach } = job;

  useEffect(() => {
    if (id) {
      void attach(id);
    }
  }, [attach, id]);

  const terminal =
    job.phase === "succeeded" || job.phase === "failed" || job.phase === "cancelled";

  // The composition arrives with the persisted report, which is written before
  // the job reaches a terminal state — including a failed one.
  const report = useQuery({
    queryKey: ["investigation-report", id],
    queryFn: () => getInvestigationReport(id),
    enabled: Boolean(id) && terminal,
    retry: false,
  });

  const selectedEvidence = params.get("ev") ?? "";
  const selectEvidence = useCallback(
    (evidenceId: string) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (evidenceId && evidenceId !== next.get("ev")) {
            next.set("ev", evidenceId);
          } else {
            next.delete("ev");
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const investigation = job.investigation;
  const diagnosis = job.diagnosis;
  const evidence = evidenceIndex(investigation).get(selectedEvidence);

  useDocumentTitle(
    investigation?.context
      ? `${investigation.context} · ${job.isRunning ? "running" : job.phase}`
      : "Investigation",
  );

  // Severity is derived from findings, so a run that collected nothing has no
  // findings and reports "Healthy". Showing that next to a failure notice
  // would have the header contradict the body — the same misrepresentation the
  // grounding checks exist to prevent, moved into the UI. The outcome wins.
  const outcome =
    job.phase === "failed"
      ? { tone: "critical" as const, label: "Failed" }
      : job.phase === "cancelled"
        ? { tone: "neutral" as const, label: "Cancelled" }
        : investigation?.severity?.severity
          ? {
              tone: severityTone(investigation.severity.severity),
              label: investigation.severity.severity,
            }
          : null;

  return (
    <div className="flex min-h-full">
      <div className="min-w-0 flex-1">
        <div className="mx-auto max-w-document px-6 py-8">
          <Link
            to="/"
            className="text-sm text-ink-3 transition-colors duration-fast hover:text-ink-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
          >
            ← Fleet
          </Link>

          <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="truncate text-display">
                {investigation?.context || "Investigation"}
              </h1>
              <p className="mt-1 font-mono text-sm text-ink-3">{id}</p>
            </div>
            <div className="flex items-center gap-3">
              {outcome ? <SeverityDot tone={outcome.tone} label={outcome.label} /> : null}
              {job.isRunning ? (
                <button
                  type="button"
                  onClick={() => void job.cancel()}
                  className="rounded-md border border-line bg-raised px-3 py-1.5 text-sm transition-colors duration-fast hover:border-critical hover:text-critical focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
                >
                  Cancel
                </button>
              ) : null}
            </div>
          </div>

          {job.error ? (
            <p role="alert" className="mt-5 rounded-md border border-critical/40 bg-critical/5 px-4 py-3 text-sm text-critical">
              {job.error}
            </p>
          ) : null}

          {!terminal ? (
            <div className="mt-8">
              <LiveTimeline
                phase={job.phase}
                transport={job.transport}
                timeline={job.timeline}
                onCancel={() => void job.cancel()}
              />
            </div>
          ) : null}

          {terminal ? (
            <div className="mt-8">
              {report.isLoading ? (
                <ReportSkeleton />
              ) : (
                <ReportDocument
                  composition={report.data?.report}
                  diagnosis={diagnosis}
                  investigation={investigation}
                  selectedEvidence={selectedEvidence}
                  onSelectEvidence={selectEvidence}
                />
              )}
            </div>
          ) : null}

          {terminal && diagnosis ? (
            <div className="mt-8">
              <RemediationPanel diagnosis={diagnosis} investigation={investigation} />
            </div>
          ) : null}

          {terminal && job.historyItem ? (
            <div className="mt-8">
              <ArtifactsPanel historyItem={job.historyItem} />
            </div>
          ) : null}

        </div>
      </div>

      {selectedEvidence ? (
        <aside className="hidden w-[400px] shrink-0 border-l border-line-muted bg-surface xl:block">
          <div className="sticky top-14 max-h-[calc(100vh-3.5rem)] overflow-y-auto">
            <EvidenceInspector
              evidence={evidence}
              diagnosis={diagnosis}
              onClose={() => selectEvidence("")}
            />
          </div>
        </aside>
      ) : null}

      {/* Below the three-column breakpoint the inspector is an overlay, so it
          never squeezes the document narrower than it can be read at. */}
      {selectedEvidence ? (
        <div className="fixed inset-0 z-40 bg-black/60 xl:hidden" onClick={() => selectEvidence("")}>
          <div
            className="absolute inset-y-0 right-0 w-full max-w-md overflow-y-auto bg-surface"
            onClick={(event) => event.stopPropagation()}
          >
            <EvidenceInspector
              evidence={evidence}
              diagnosis={diagnosis}
              onClose={() => selectEvidence("")}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Matches the document's box model, so nothing reflows on arrival. */
function ReportSkeleton() {
  return (
    <div className="grid gap-8" aria-hidden="true">
      {[0, 1, 2].map((block) => (
        <div key={block} className="grid gap-3">
          <div className="h-4 w-40 rounded bg-raised" />
          <div className="h-3 w-full max-w-measure rounded bg-line-muted" />
          <div className="h-3 w-3/4 max-w-measure rounded bg-line-muted" />
        </div>
      ))}
    </div>
  );
}
