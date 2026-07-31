import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useQueries, useQuery } from "@tanstack/react-query";

import { SeverityDot } from "../components/report/SeverityDot";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import {
  recurredOnOneCluster,
  recurringFindings,
  search,
  sharedAcrossClusters,
  trendLabel,
  type CorpusEntry,
  type Finding,
} from "../lib/ask";
import { getInvestigationHistory, getInvestigationReport } from "../services/api";

/** One report fetch per investigation, so the corpus is bounded on purpose. */
const CORPUS_LIMIT = 20;

/**
 * Ask — the cross-investigation workspace.
 *
 * Not a chatbot, and not where the AI lives: reasoning about a single
 * investigation stays inline with that investigation. This answers the
 * questions that span several, which had no home at all.
 *
 * Everything on this page is a query over stored reports. No model is called,
 * which is why it is useful on day one — and every claim is a list of the
 * investigations it was counted from, so there is no path here that produces a
 * sentence without its evidence.
 */
export function AskPage() {
  useDocumentTitle("Ask");
  const [query, setQuery] = useState("");
  const [openFinding, setOpenFinding] = useState("");

  const history = useQuery({
    queryKey: ["investigation-history"],
    queryFn: getInvestigationHistory,
  });

  const recent = (history.data ?? []).slice(0, CORPUS_LIMIT);
  const reports = useQueries({
    queries: recent.map((item) => ({
      queryKey: ["investigation-report", item.id],
      queryFn: () => getInvestigationReport(item.id),
      retry: false,
      staleTime: 60_000,
    })),
  });

  const corpus = useMemo<CorpusEntry[]>(
    () =>
      reports
        .map((report, position): CorpusEntry | null => {
          const item = recent[position];
          if (!item || !report.data) {
            return null;
          }
          return {
            investigationId: item.id,
            cluster: item.context ?? "",
            at: item.timestamp ?? "",
            rootCause: item.root_cause ?? "",
            signals: (report.data.diagnosis?.signals ?? []).map((signal) => ({
              type: signal.type,
              summary: signal.summary,
              severity: String(signal.severity),
            })),
          };
        })
        .filter((entry): entry is CorpusEntry => entry !== null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [reports.map((report) => report.data?.incident_id ?? "").join("|"), recent.length],
  );

  const findings = useMemo(() => recurringFindings(corpus), [corpus]);
  const matches = useMemo(() => search(findings, query), [findings, query]);
  const shared = useMemo(() => sharedAcrossClusters(matches), [matches]);
  // Disjoint from `shared`, so one finding never appears under two headings.
  const local = useMemo(() => recurredOnOneCluster(matches), [matches]);
  const loading = history.isLoading || reports.some((report) => report.isLoading);

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-display">Ask</h1>
      {/* Permanent, and not in a tooltip. Without it an operator reasonably
          assumes live cluster access and receives confident answers about a
          cluster nobody looked at. */}
      <p className="mt-1 max-w-measure text-sm leading-6 text-ink-2">
        Answers come from {corpus.length} stored{" "}
        {corpus.length === 1 ? "investigation" : "investigations"}. No cluster is
        queried, and nothing here is generated — every claim is a count of runs
        you can open.
      </p>

      <label htmlFor="ask" className="sr-only">
        Filter findings
      </label>
      <input
        id="ask"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Filter by finding or cluster — for example, image or prod-eu-west"
        className="mt-6 w-full rounded-md border border-line bg-raised px-3 py-2 text-body text-ink outline-none transition-colors duration-fast placeholder:text-ink-3 focus:border-info focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
      />

      {loading ? (
        <div className="mt-8 grid gap-2" aria-hidden="true">
          {[0, 1, 2].map((row) => (
            <div key={row} className="h-16 rounded-lg border border-line bg-surface" />
          ))}
        </div>
      ) : findings.length === 0 ? (
        <p className="mt-8 max-w-measure text-sm leading-6 text-ink-2">
          Nothing has recurred yet. A finding appears here once it has been seen
          in more than one investigation — a single occurrence is an incident,
          not a pattern, and it is already on its own page.
        </p>
      ) : matches.length === 0 ? (
        <p className="mt-8 text-sm text-ink-2">
          Nothing on record matches “{query}”.
        </p>
      ) : (
        <>
          {shared.length > 0 ? (
            <section className="mt-8">
              <h2 className="text-h2">Seen on more than one cluster</h2>
              <p className="mt-1 max-w-measure text-sm text-ink-2">
                The same failure across several clusters is one incident, and no
                single investigation can see it.
              </p>
              <FindingList
                findings={shared}
                open={openFinding}
                onToggle={setOpenFinding}
              />
            </section>
          ) : null}

          {local.length > 0 ? (
            <section className="mt-8">
              <h2 className="text-h2">Has this happened before, on one cluster?</h2>
              <p className="mt-1 max-w-measure text-sm text-ink-2">
                Findings seen in more than one investigation of the same cluster,
                with every run they appeared in.
              </p>
              <FindingList findings={local} open={openFinding} onToggle={setOpenFinding} />
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}

function FindingList({
  findings,
  open,
  onToggle,
}: {
  findings: Finding[];
  open: string;
  onToggle: (type: string) => void;
}) {
  return (
    <ul className="mt-4 grid gap-2">
      {findings.map((finding) => {
        const expanded = open === finding.type;
        return (
          <li key={finding.type} className="rounded-lg border border-line bg-surface">
            <button
              type="button"
              onClick={() => onToggle(expanded ? "" : finding.type)}
              aria-expanded={expanded}
              className="flex w-full flex-wrap items-baseline justify-between gap-x-4 gap-y-1 p-4 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-info"
            >
              <span className="flex min-w-0 flex-1 items-baseline gap-2">
                <SeverityDot tone={finding.tone} />
                <span className="min-w-0">
                  <span className="block font-mono text-sm text-ink">{finding.type}</span>
                  <span className="mt-1 block truncate text-sm text-ink-2">
                    {finding.summary}
                  </span>
                </span>
              </span>
              <span className="shrink-0 text-right">
                <span className="block font-mono text-sm text-ink-2">
                  {finding.occurrences.length} runs
                  {finding.clusters.length > 1
                    ? ` · ${finding.clusters.length} clusters`
                    : ""}
                </span>
                <span className="mt-1 block text-sm text-ink-3">
                  {trendLabel(finding.trend)}
                </span>
              </span>
            </button>

            {expanded ? (
              <div className="border-t border-line-muted p-4">
                <p className="text-label uppercase text-ink-3">
                  Counted from these investigations
                </p>
                <ul className="mt-2 grid gap-1">
                  {finding.occurrences.map((occurrence) => (
                    <li key={occurrence.investigationId}>
                      <Link
                        to={`/investigations/${occurrence.investigationId}`}
                        className="flex flex-wrap items-baseline justify-between gap-x-4 rounded px-2 py-1.5 text-sm transition-colors duration-fast hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
                      >
                        <span className="text-ink-2">{occurrence.cluster || "unattributed"}</span>
                        <span className="font-mono text-sm text-ink-3">
                          {occurrence.at.slice(0, 16).replace("T", " ")}
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
