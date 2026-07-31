import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { ClusterCard } from "../components/fleet/ClusterCard";
import { FleetGrid } from "../components/fleet/FleetGrid";
import { SignalCorrelation } from "../components/fleet/SignalCorrelation";
import { SeverityDot } from "../components/report/SeverityDot";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import {
  correlateSignals,
  fleetState,
  rollup,
  type ClusterState,
  type FleetState,
} from "../lib/fleet";
import {
  getInvestigationHistory,
  getInvestigationJobs,
  getInvestigationReport,
  getKubernetesContexts,
  startInvestigationJob,
} from "../services/api";

/** Above this many clusters the board stops being readable and the grid takes over. */
const GRID_THRESHOLD = 24;

/**
 * Correlation costs one report fetch per cluster. Bounded here rather than
 * left to grow: a fleet-wide figure is an aggregate endpoint's job.
 */
const CORRELATION_LIMIT = 12;

const ROLLUP: Array<[FleetState, string]> = [
  ["critical", "critical"],
  ["unreadable", "could not read"],
  ["warning", "degraded"],
  ["stale", "stale"],
  ["unknown", "not investigated"],
  ["healthy", "healthy"],
];

export function FleetPage() {
  useDocumentTitle("Fleet");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [view, setView] = useState<"auto" | "board" | "grid">("auto");
  const [starting, setStarting] = useState("");

  const contexts = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
  });
  const history = useQuery({
    queryKey: ["investigation-history"],
    queryFn: getInvestigationHistory,
  });
  // Only needed to attribute entries written before history recorded its
  // cluster. New entries carry it themselves.
  const jobs = useQuery({
    queryKey: ["investigation-jobs"],
    queryFn: getInvestigationJobs,
    retry: false,
  });

  const rows = useMemo(() => {
    const jobContexts = new Map<string, string>();
    for (const job of jobs.data ?? []) {
      const context = (job.request as { context?: string } | undefined)?.context;
      if (context) {
        jobContexts.set(job.id, context);
      }
    }
    return fleetState(contexts.data?.items ?? [], history.data ?? [], jobContexts);
  }, [contexts.data?.items, history.data, jobs.data]);

  const counts = useMemo(() => rollup(rows), [rows]);

  const correlationTargets = rows
    .filter((row) => row.investigationId)
    .slice(0, CORRELATION_LIMIT);

  const reports = useQueries({
    queries: correlationTargets.map((row) => ({
      queryKey: ["investigation-report", row.investigationId],
      queryFn: () => getInvestigationReport(row.investigationId),
      retry: false,
      staleTime: 60_000,
    })),
  });

  const correlated = useMemo(
    () =>
      correlateSignals(
        reports
          .map((report, position) => ({
            cluster: correlationTargets[position]?.name ?? "",
            signals: (report.data?.diagnosis?.signals ?? []).map((signal) => ({
              type: signal.type,
              summary: signal.summary,
              severity: signal.severity,
            })),
          }))
          .filter((entry) => entry.cluster),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [reports.map((report) => report.data?.incident_id).join("|")],
  );

  async function investigate(name: string) {
    setStarting(name);
    try {
      const accepted = await startInvestigationJob(name);
      queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
      navigate(`/investigations/${accepted.id}`);
    } finally {
      setStarting("");
    }
  }

  const useGrid = view === "grid" || (view === "auto" && rows.length > GRID_THRESHOLD);
  const loading = contexts.isLoading || history.isLoading;

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-display">Fleet</h1>
          <p className="mt-1 text-sm text-ink-2">
            {rows.length} {rows.length === 1 ? "cluster" : "clusters"} · state as of the
            last investigation of each, not a live reading.
          </p>
        </div>

        {rows.length > 6 ? (
          <div className="flex overflow-hidden rounded-md border border-line">
            {(["board", "grid"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setView(option)}
                aria-pressed={useGrid === (option === "grid")}
                className={`px-3 py-1.5 text-sm capitalize transition-colors duration-fast focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-info ${
                  useGrid === (option === "grid")
                    ? "bg-raised text-ink"
                    : "text-ink-3 hover:text-ink-2"
                }`}
              >
                {option}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2">
        {ROLLUP.filter(([state]) => counts[state] > 0).map(([state, label]) => (
          <SeverityDot
            key={state}
            tone={
              state === "stale" || state === "unknown"
                ? "neutral"
                : state === "unreadable"
                  ? "warning"
                  : state
            }
            label={`${counts[state]} ${label}`}
          />
        ))}
      </div>

      {loading ? (
        <ul className="mt-6 grid gap-2" aria-hidden="true">
          {[0, 1, 2].map((row) => (
            <li key={row} className="h-20 rounded-lg border border-line bg-surface" />
          ))}
        </ul>
      ) : rows.length === 0 ? (
        <div className="mt-10 rounded-lg border border-dashed border-line px-6 py-12 text-center">
          <p className="text-h2">No clusters yet</p>
          <p className="mx-auto mt-2 max-w-measure text-sm leading-6 text-ink-2">
            Clusters appear here from your kubeconfig, and gain a state once
            they have been investigated. Evidence is collected read-only;
            nothing is ever applied.
          </p>
        </div>
      ) : useGrid ? (
        <div className="mt-6">
          <FleetGrid rows={rows} onSelect={(row: ClusterState) => void investigate(row.name)} />
        </div>
      ) : (
        <ul className="mt-6 grid gap-2">
          {rows.map((row) => (
            <ClusterCard
              key={row.name}
              row={row}
              onInvestigate={(name) => void investigate(name)}
            />
          ))}
        </ul>
      )}

      {starting ? (
        <p className="mt-4 text-sm text-ink-3">Queueing an investigation for {starting}…</p>
      ) : null}

      <SignalCorrelation
        groups={correlated}
        limited={rows.length > CORRELATION_LIMIT}
      />
    </div>
  );
}
