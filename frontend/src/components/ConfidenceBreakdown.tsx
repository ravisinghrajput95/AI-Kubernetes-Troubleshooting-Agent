import { EmptyState, Meter, Panel, Tag } from "./ui";
import { totalContribution } from "../lib/analysis";
import type { Diagnosis } from "../types/investigation";

/**
 * How the reported confidence was composed.
 *
 * Shows each weighted component so a number can be decomposed rather than
 * taken on trust. Nothing is shown when the backend supplied no breakdown —
 * an invented one would defeat the purpose.
 */
export function ConfidenceBreakdown({ diagnosis }: { diagnosis?: Diagnosis }) {
  const components = diagnosis?.confidence_breakdown ?? [];
  const grounding = diagnosis?.grounding;
  const total = totalContribution(components);
  const reported = diagnosis?.confidence ?? 0;

  return (
    <Panel
      title="Confidence Breakdown"
      subtitle="Evidence strength, model reasoning, and evidence completeness, weighted."
      action={
        <div className="flex items-center gap-2">
          <Tag
            label={diagnosis?.ai_generated ? "AI-assisted" : "deterministic"}
            className={
              diagnosis?.ai_generated
                ? "border-fuchsia-800 bg-fuchsia-950/40 text-fuchsia-300"
                : "border-slate-700 bg-slate-900 text-slate-300"
            }
          />
          <Tag
            label={`${reported}%`}
            className="border-cyan-800 bg-cyan-950/40 text-cyan-300"
          />
        </div>
      }
    >
      {components.length === 0 ? (
        <EmptyState message="No confidence breakdown was reported for this investigation." />
      ) : (
        <div className="grid gap-3">
          {components.map((component) => (
            <div
              key={component.component}
              className="rounded-md border border-slate-800 bg-[#101722] px-4 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-slate-200">
                  {component.component}
                </span>
                <span className="font-mono text-xs text-slate-400">
                  {component.score}% × {component.weight}% ={" "}
                  <span className="text-slate-200">{component.contribution}</span>
                </span>
              </div>
              <div className="mt-2">
                <Meter value={component.score} />
              </div>
              <p className="mt-2 text-xs leading-5 text-slate-500">
                {component.detail}
              </p>
            </div>
          ))}

          {total !== reported ? (
            <p className="rounded-md border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
              Components total {total}%, but {reported}% was reported. The score was
              capped or clamped.
            </p>
          ) : null}

          {grounding && grounding.rejected_citations.length > 0 ? (
            <p className="rounded-md border border-red-900/60 bg-red-950/20 px-3 py-2 text-xs text-red-200">
              {grounding.rejected_citations.length} fabricated citation(s) were
              rejected from the model response:{" "}
              <span className="font-mono">
                {grounding.rejected_citations.join(", ")}
              </span>
            </p>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
