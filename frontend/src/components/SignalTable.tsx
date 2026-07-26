import { EmptyState, Panel, Tag } from "./ui";
import { SEVERITY_TONE, formatTarget, groupSignalsByDomain } from "../lib/analysis";
import type { Diagnosis } from "../types/investigation";

/**
 * Every observation the platform extracted from evidence.
 *
 * Signals are deterministic — produced by rules, never by the model — and each
 * one names the evidence it came from.
 */
export function SignalTable({ diagnosis }: { diagnosis?: Diagnosis }) {
  const signals = diagnosis?.signals ?? [];
  const cited = new Set(diagnosis?.cited_signals ?? []);
  const groups = groupSignalsByDomain(signals);

  return (
    <Panel
      title="Signals"
      subtitle="Deterministic observations extracted from collected evidence."
      action={signals.length ? <Tag label={`${signals.length} total`} /> : undefined}
    >
      {signals.length === 0 ? (
        <EmptyState message="No failure signals were extracted." />
      ) : (
        <div className="grid gap-4">
          {groups.map(([domain, domainSignals]) => (
            <div key={domain}>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                {domain} · {domainSignals.length}
              </p>
              <ul className="mt-2 grid gap-2">
                {domainSignals.map((signal) => (
                  <li
                    key={signal.id}
                    className={`rounded-md border px-3 py-2 ${
                      cited.has(signal.id)
                        ? "border-cyan-800 bg-cyan-950/20"
                        : "border-slate-800 bg-[#101722]"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Tag
                        label={signal.severity}
                        className={SEVERITY_TONE[signal.severity]}
                      />
                      <span className="text-sm text-slate-200">{signal.summary}</span>
                      {cited.has(signal.id) ? (
                        <Tag
                          label="cited"
                          title="This signal was cited by the diagnosis."
                          className="border-cyan-800 bg-cyan-950/40 text-cyan-300"
                        />
                      ) : null}
                    </div>
                    <p className="mt-1 font-mono text-[11px] text-slate-500">
                      {formatTarget(signal.target)} · from{" "}
                      {signal.evidence_ids.join(", ") || "unknown evidence"}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
