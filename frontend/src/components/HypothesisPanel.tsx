import { useState } from "react";

import { EmptyState, Meter, Panel, Tag } from "./ui";
import {
  SEVERITY_TONE,
  formatTarget,
  rankHypotheses,
  signalsById,
} from "../lib/analysis";
import type { Diagnosis, Hypothesis, Signal } from "../types/investigation";

function SignalRefs({
  ids,
  lookup,
  tone,
  label,
}: {
  ids: string[];
  lookup: Map<string, Signal>;
  tone: string;
  label: string;
}) {
  if (ids.length === 0) {
    return null;
  }

  return (
    <div className="mt-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <ul className="mt-2 grid gap-1.5">
        {ids.map((id) => {
          const signal = lookup.get(id);
          return (
            <li key={id} className={`rounded-md border px-3 py-1.5 text-xs ${tone}`}>
              {signal ? signal.summary : id}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * Candidate root causes with the evidence for and against each.
 *
 * Refuting signals are shown as prominently as supporting ones: a hypothesis
 * that the evidence argues against is the most useful thing an operator can
 * learn early in an incident.
 */
export function HypothesisPanel({ diagnosis }: { diagnosis?: Diagnosis }) {
  const [expanded, setExpanded] = useState<string | null>(null);

  const hypotheses = rankHypotheses(diagnosis?.hypotheses ?? []);
  const lookup = signalsById(diagnosis?.signals ?? []);
  const selected = diagnosis?.selected_hypothesis ?? null;

  return (
    <Panel
      title="Candidate Root Causes"
      subtitle="Ranked hypotheses, each scored from the signals that support it."
      action={
        hypotheses.length ? (
          <Tag label={`${hypotheses.length} considered`} />
        ) : undefined
      }
    >
      {hypotheses.length === 0 ? (
        <EmptyState message="No hypotheses were generated. The evidence showed no recognised failure pattern." />
      ) : (
        <ul className="grid gap-3">
          {hypotheses.map((hypothesis: Hypothesis) => {
            const isSelected = hypothesis.id === selected;
            const isOpen = expanded === hypothesis.id;

            return (
              <li
                key={hypothesis.id}
                className={`rounded-md border px-4 py-3 ${
                  isSelected
                    ? "border-cyan-700 bg-cyan-950/20"
                    : "border-slate-800 bg-[#101722]"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-slate-100">
                        {hypothesis.title}
                      </span>
                      {isSelected ? (
                        <Tag
                          label="selected"
                          className="border-cyan-700 bg-cyan-950/40 text-cyan-300"
                        />
                      ) : null}
                      <Tag
                        label={hypothesis.severity}
                        className={SEVERITY_TONE[hypothesis.severity]}
                      />
                    </div>
                    <p className="mt-1 font-mono text-xs text-slate-500">
                      {hypothesis.id} · {formatTarget(hypothesis.target)}
                    </p>
                  </div>
                  <div className="w-32 shrink-0">
                    <div className="flex items-center justify-between text-xs text-slate-400">
                      <span>confidence</span>
                      <span className="font-semibold text-slate-200">
                        {hypothesis.confidence}%
                      </span>
                    </div>
                    <div className="mt-1">
                      <Meter
                        value={hypothesis.confidence}
                        tone={
                          hypothesis.refuting_signals.length
                            ? "bg-amber-400"
                            : "bg-cyan-400"
                        }
                      />
                    </div>
                  </div>
                </div>

                <p className="mt-2 text-sm leading-6 text-slate-300">
                  {hypothesis.rationale}
                </p>

                <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                  <Tag
                    label={`${hypothesis.supporting_signals.length} supporting`}
                    className="border-lime-800 bg-lime-950/30 text-lime-300"
                  />
                  {hypothesis.refuting_signals.length ? (
                    <Tag
                      label={`${hypothesis.refuting_signals.length} refuting`}
                      className="border-red-800 bg-red-950/30 text-red-300"
                    />
                  ) : null}
                  <button
                    type="button"
                    onClick={() => setExpanded(isOpen ? null : hypothesis.id)}
                    className="rounded-md border border-slate-700 px-2 py-0.5 font-semibold text-slate-300 hover:border-cyan-700 hover:text-cyan-300"
                  >
                    {isOpen ? "Hide evidence" : "Show evidence"}
                  </button>
                </div>

                {isOpen ? (
                  <div className="mt-1">
                    <SignalRefs
                      label="Supporting signals"
                      ids={hypothesis.supporting_signals}
                      lookup={lookup}
                      tone="border-lime-900/60 bg-lime-950/20 text-lime-200"
                    />
                    <SignalRefs
                      label="Refuting signals"
                      ids={hypothesis.refuting_signals}
                      lookup={lookup}
                      tone="border-red-900/60 bg-red-950/20 text-red-200"
                    />
                    {hypothesis.missing_evidence.length ? (
                      <div className="mt-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                          Would confirm or refute
                        </p>
                        <ul className="mt-2 grid gap-1.5">
                          {hypothesis.missing_evidence.map((item) => (
                            <li
                              key={item}
                              className="rounded-md border border-slate-800 bg-[#0d131c] px-3 py-1.5 text-xs text-slate-400"
                            >
                              {item}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    {hypothesis.remediation_hint ? (
                      <p className="mt-3 rounded-md border border-slate-800 bg-[#0d131c] px-3 py-2 text-xs text-slate-300">
                        <span className="font-semibold text-slate-200">Fix: </span>
                        {hypothesis.remediation_hint}
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
