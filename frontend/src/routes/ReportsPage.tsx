import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ReportPreview } from "../components/report/ReportPreview";
import {
  getInvestigationHistory,
  getInvestigationReport,
  regenerateInvestigationReport,
  reportUrl,
} from "../services/api";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

/**
 * Saved investigation reports.
 *
 * The first route split: this table lived at the bottom of the single scrolling
 * page, below the investigation form that produced it. The table itself now
 * lives here too, which is the move that comment anticipated: this is its
 * only caller, and importing it from `App.tsx` pointed the dependency
 * backwards — a route reaching into the shell that mounts it.
 */
export function ReportsPage() {
  useDocumentTitle("Reports");

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-h1">Reports</h1>
      <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
        Completed investigations, saved as incident reports.
      </p>
      <div className="mt-6">
        <HistoryTable />
      </div>
    </div>
  );
}

export function HistoryTable() {
  const { data = [], isLoading } = useQuery({
    queryKey: ["investigation-history"],
    queryFn: getInvestigationHistory,
  });
  const queryClient = useQueryClient();
  const [selectedReportId, setSelectedReportId] = useState("");

  const report = useQuery({
    queryKey: ["investigation-report", selectedReportId],
    queryFn: () => getInvestigationReport(selectedReportId),
    enabled: Boolean(selectedReportId),
  });

  const regenerate = useMutation({
    mutationFn: regenerateInvestigationReport,
    onSuccess: (updatedReport) => {
      queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
      queryClient.setQueryData(["investigation-report", selectedReportId], updatedReport);
    },
  });

  return (
    <div className="grid gap-5">
      {report.data ? (
        <ReportPreview report={report.data} onClose={() => setSelectedReportId("")} />
      ) : null}

      <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
        <h2 className="font-semibold text-slate-100">Recent Investigations</h2>
        <p className="mt-1 text-sm text-slate-400">
          Completed investigations are saved as SRE incident reports.
        </p>

        <div className="mt-5 overflow-x-auto">
          <table className="w-full min-w-[1080px] border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-xs uppercase tracking-wide text-slate-500">
              <th className="py-3 pr-4 font-semibold">Incident</th>
              <th className="py-3 pr-4 font-semibold">Timestamp</th>
              <th className="py-3 pr-4 font-semibold">Root Cause</th>
              <th className="py-3 pr-4 font-semibold">Namespace</th>
              <th className="py-3 pr-4 font-semibold">Severity</th>
              <th className="py-3 pr-4 font-semibold">Environment</th>
              <th className="py-3 pr-4 font-semibold">Confidence</th>
              <th className="py-3 pr-4 font-semibold">Status</th>
              <th className="py-3 pr-4 font-semibold">Actions</th>
              <th className="py-3 font-semibold">Report</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td className="py-4 text-slate-400" colSpan={10}>
                  Loading history...
                </td>
              </tr>
            ) : null}
            {!isLoading && data.length === 0 ? (
              <tr>
                <td className="py-4 text-slate-400" colSpan={10}>
                  No investigations yet.
                </td>
              </tr>
            ) : null}
            {data.map((item) => (
              <tr key={item.id} className="border-b border-slate-900">
                <td className="py-3 pr-4 font-mono text-xs text-cyan-200">
                  {item.incident_id ?? item.id.slice(0, 8)}
                </td>
                <td className="py-3 pr-4 text-slate-400">
                  {new Date(item.timestamp).toLocaleString()}
                </td>
                <td className="max-w-md py-3 pr-4 font-medium text-slate-100">
                  {item.root_cause}
                </td>
                <td className="py-3 pr-4 text-slate-400">{item.namespace}</td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.severity ?? "Unknown"}
                </td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.environment ?? "Unknown"}
                </td>
                <td className="py-3 pr-4 text-slate-400">{item.confidence}%</td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.incident_status ?? item.status}
                </td>
                <td className="py-3 pr-4">
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setSelectedReportId(item.id)}
                      className="font-medium text-cyan-300 underline underline-offset-4"
                    >
                      Preview
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedReportId(item.id);
                        regenerate.mutate(item.id);
                      }}
                      className="font-medium text-lime-300 underline underline-offset-4 disabled:text-slate-600"
                      disabled={regenerate.isPending}
                    >
                      Regenerate
                    </button>
                  </div>
                </td>
                <td className="py-3">
                  <div className="flex gap-2">
                    <a
                      href={reportUrl(item.pdf_url)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium text-cyan-300 underline underline-offset-4"
                    >
                      PDF
                    </a>
                    {item.json_url ? (
                      <a
                        href={reportUrl(item.json_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium text-violet-300 underline underline-offset-4"
                      >
                        JSON
                      </a>
                    ) : null}
                    {item.markdown_url ? (
                      <a
                        href={reportUrl(item.markdown_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium text-amber-300 underline underline-offset-4"
                      >
                        MD
                      </a>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      </section>
    </div>
  );
}
