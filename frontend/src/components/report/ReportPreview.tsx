import type { InvestigationReport } from "../../types/investigation";

export function ReportPreview({
  report,
  onClose,
}: {
  report: InvestigationReport;
  onClose: () => void;
}) {
  const metadata = report.report_metadata;
  const timeline = report.investigation.timeline ?? [];
  const impact = metadata?.business_impact ?? [];
  const confidence = metadata?.confidence_breakdown ?? [];
  const evidence = metadata?.evidence_matrix ?? [];

  return (
    <section className="rounded-lg border border-cyan-900/70 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-cyan-300">
            Report Preview
          </p>
          <h2 className="mt-2 text-xl font-semibold text-slate-100">
            {report.incident_id ?? "Investigation Report"}
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            {metadata?.cluster ?? "Current Context"}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-slate-700 px-3 py-2 text-sm font-semibold text-slate-300 hover:border-slate-500"
        >
          Close
        </button>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-4">
        {[
          ["Severity", metadata?.severity ?? "Unknown"],
          ["Status", metadata?.incident_status ?? report.status],
          ["Environment", metadata?.environment ?? "Unknown"],
          ["Confidence", `${report.diagnosis.confidence}%`],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border border-slate-800 bg-[#101722] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {label}
            </p>
            <p className="mt-2 text-sm font-semibold text-slate-100">{value}</p>
          </div>
        ))}
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">Root Cause</h3>
          <p className="mt-3 text-sm leading-6 text-slate-300">
            {report.diagnosis.root_cause}
          </p>
          <h3 className="mt-5 text-sm font-semibold text-slate-100">
            Business Impact
          </h3>
          <div className="mt-3 grid gap-2">
            {(impact.length ? impact : ["No business impact recorded."]).map((item) => (
              <p key={item} className="text-sm leading-6 text-slate-400">
                {item}
              </p>
            ))}
          </div>
        </div>

        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            AI Confidence Breakdown
          </h3>
          <div className="mt-3 grid gap-2">
            {confidence.map((item) => (
              <div key={item.source} className="grid grid-cols-[1fr_auto] gap-3 text-sm">
                <span className="text-slate-400">{item.source}</span>
                <span className="font-semibold text-cyan-200">
                  {item.contribution}%
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-2">
        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">Evidence Matrix</h3>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[420px] text-left text-sm">
              <tbody>
                {evidence.map((item) => (
                  <tr key={item.source} className="border-t border-slate-900">
                    <td className="py-2 pr-4 text-slate-400">{item.source}</td>
                    <td className="py-2 font-semibold text-slate-100">{item.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            Investigation Timeline
          </h3>
          <div className="mt-3 grid gap-2">
            {timeline.map((item) => (
              <p key={`${item.time}-${item.message}`} className="text-sm text-slate-400">
                <span className="mr-3 font-mono text-cyan-200">{item.time}</span>
                {item.message}
              </p>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
