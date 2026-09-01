import { reportUrl } from "../../services/api";

export function ArtifactsPanel({
  historyItem,
}: {
  historyItem?: { pdf_url: string; json_url?: string; markdown_url?: string };
}) {
  const items = [
    ["PDF Report", historyItem?.pdf_url],
    ["JSON Report", historyItem?.json_url],
    ["Markdown Report", historyItem?.markdown_url],
  ];

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Downloadable Artifacts</h2>
      <div className="mt-4 flex flex-wrap gap-3">
        {items.map(([label, url]) =>
          url ? (
            <a
              key={label}
              href={reportUrl(url)}
              target="_blank"
              rel="noreferrer"
              className="rounded-md border border-cyan-800 bg-cyan-950/30 px-4 py-2 text-sm font-semibold text-cyan-200"
            >
              {label}
            </a>
          ) : (
            <button
              key={label}
              type="button"
              disabled
              className="rounded-md border border-slate-800 bg-slate-900 px-4 py-2 text-sm text-slate-500"
            >
              {label}
            </button>
          ),
        )}
      </div>
    </section>
  );
}
