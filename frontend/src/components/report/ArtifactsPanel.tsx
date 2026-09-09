import { useState } from "react";

import { downloadReport } from "../../services/api";

/**
 * The three rendered formats of one investigation.
 *
 * These were `<a href>` links straight at the API, which cannot carry an
 * `Authorization` header — so in every deployment with authentication
 * configured, clicking one was answered 401 and saved a JSON error body under
 * the name of a report. They are buttons that fetch with the credential now.
 * Same root cause as the progress stream, which was an `EventSource` for the
 * same reason and refused for the same one.
 */
export function ArtifactsPanel({
  historyItem,
}: {
  historyItem?: { pdf_url: string; json_url?: string; markdown_url?: string };
}) {
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState("");

  const items: Array<[string, string | undefined, string]> = [
    ["PDF Report", historyItem?.pdf_url, "investigation.pdf"],
    ["JSON Report", historyItem?.json_url, "investigation.json"],
    ["Markdown Report", historyItem?.markdown_url, "investigation.md"],
  ];

  async function save(label: string, url: string, fallback: string) {
    setFailed("");
    setBusy(label);
    try {
      await downloadReport(url, fallback);
    } catch (error) {
      // Said out loud rather than swallowed: a download that silently does
      // nothing is what a 401 already looked like from the outside.
      setFailed(error instanceof Error ? error.message : "Could not download the report.");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Downloadable Artifacts</h2>
      <div className="mt-4 flex flex-wrap gap-3">
        {items.map(([label, url, fallback]) =>
          url ? (
            <button
              key={label}
              type="button"
              disabled={busy === label}
              onClick={() => void save(label, url, fallback)}
              className="rounded-md border border-cyan-800 bg-cyan-950/30 px-4 py-2 text-sm font-semibold text-cyan-200 disabled:opacity-60"
            >
              {busy === label ? `${label}\u2026` : label}
            </button>
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
      {failed ? (
        <p role="alert" className="mt-3 text-sm text-rose-300">
          {failed}
        </p>
      ) : null}
    </section>
  );
}
