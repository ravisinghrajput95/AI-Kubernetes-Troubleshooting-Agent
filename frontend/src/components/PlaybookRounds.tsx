import { EmptyState, Panel, Tag } from "./ui";
import { humanizeKind } from "../lib/analysis";
import type { InvestigationResponse } from "../types/investigation";

type InvestigationData = InvestigationResponse["investigation"];

const PLAYBOOK_TITLES: Record<string, string> = {
  crashloop: "CrashLoopBackOff",
  pending: "Pending / unschedulable",
  imagepull: "Image pull failure",
  network: "Service and connectivity",
  storage: "Volume and storage",
};

/**
 * What deep investigation ran, and what it added.
 *
 * Makes the second collection pass visible: which failure classes the platform
 * recognised, and which targeted reads it performed as a result.
 */
export function PlaybookRounds({
  investigation,
}: {
  investigation?: InvestigationData;
}) {
  const rounds = investigation?.playbook_rounds ?? [];
  const deepKinds = Object.keys(investigation?.deep_evidence ?? {});

  return (
    <Panel
      title="Deep Investigation"
      subtitle="Targeted evidence gathered after the first pass identified a failure class."
      action={
        rounds.length ? (
          <Tag
            label={`${rounds.length} round${rounds.length > 1 ? "s" : ""}`}
            className="border-fuchsia-800 bg-fuchsia-950/40 text-fuchsia-300"
          />
        ) : undefined
      }
    >
      {rounds.length === 0 ? (
        <EmptyState message="No deep investigation was needed — the baseline evidence showed no recognised failure class." />
      ) : (
        <div className="grid gap-3">
          {rounds.map((round) => (
            <div
              key={round.round}
              className="rounded-md border border-slate-800 bg-[#101722] px-4 py-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-slate-200">
                  Round {round.round}
                </span>
                {round.playbooks.map((playbook) => (
                  <Tag
                    key={playbook}
                    label={PLAYBOOK_TITLES[playbook] ?? playbook}
                    className="border-fuchsia-800 bg-fuchsia-950/30 text-fuchsia-300"
                  />
                ))}
                <span className="ml-auto text-xs text-slate-500">
                  +{round.evidence_added} evidence records
                </span>
              </div>

              <ul className="mt-2 grid gap-1">
                {round.collectors.map((collector) => (
                  <li
                    key={collector}
                    className="truncate font-mono text-[11px] text-slate-500"
                  >
                    {collector}
                  </li>
                ))}
              </ul>
            </div>
          ))}

          {deepKinds.length ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-slate-500">
                Collected
              </span>
              {deepKinds.map((kind) => (
                <Tag key={kind} label={humanizeKind(kind)} />
              ))}
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
