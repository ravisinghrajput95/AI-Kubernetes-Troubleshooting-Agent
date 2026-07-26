import { useState } from "react";

import { EmptyState, Panel, Tag } from "./ui";
import { formatTarget } from "../lib/analysis";
import type {
  Diagnosis,
  Patch,
  RemediationPlan,
  RemediationStep,
} from "../types/investigation";

const RISK_TONE: Record<string, string> = {
  Low: "border-lime-800 bg-lime-950/40 text-lime-300",
  Medium: "border-amber-800 bg-amber-950/40 text-amber-300",
  High: "border-orange-800 bg-orange-950/40 text-orange-300",
  Critical: "border-red-800 bg-red-950/40 text-red-300",
};

const SECTIONS: Array<{ key: keyof RemediationPlan; title: string; hint: string }> = [
  { key: "preconditions", title: "Before you change anything", hint: "Read-only" },
  { key: "remediation", title: "The change", hint: "Requires approval" },
  { key: "verification", title: "Confirm it worked", hint: "Read-only" },
  { key: "rollback", title: "If it goes wrong", hint: "Undo" },
];

function Steps({ steps }: { steps: RemediationStep[] }) {
  if (steps.length === 0) {
    return <p className="text-xs text-slate-500">None.</p>;
  }

  return (
    <ol className="grid gap-2">
      {steps.map((step, index) => (
        <li
          key={`${step.description}-${index}`}
          className="rounded-md border border-slate-800 bg-[#0d131c] px-3 py-2"
        >
          <div className="flex items-start gap-2">
            <span className="mt-0.5 font-mono text-[11px] text-slate-600">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1 text-sm text-slate-300">
              {step.description}
            </span>
            {step.manual ? (
              <Tag
                label="manual"
                title="This step needs a human decision; it is not a command."
                className="border-violet-800 bg-violet-950/40 text-violet-300"
              />
            ) : null}
          </div>
          {step.command ? (
            <pre className="mt-2 overflow-x-auto rounded bg-[#080d14] px-3 py-2 font-mono text-[11px] text-slate-400">
              {step.command}
            </pre>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function PatchCard({ patch }: { patch: Patch }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(patch.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="rounded-md border border-slate-800 bg-[#0d131c] px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Tag
          label={patch.format}
          className="border-cyan-800 bg-cyan-950/40 text-cyan-300"
        />
        <span className="font-mono text-xs text-slate-400">{patch.filename}</span>
        <button
          type="button"
          onClick={copy}
          className="ml-auto rounded-md border border-slate-700 px-2 py-0.5 text-xs font-semibold text-slate-300 hover:border-cyan-700 hover:text-cyan-300"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="mt-2 text-xs text-slate-500">{patch.description}</p>
      <pre className="mt-2 max-h-64 overflow-auto rounded bg-[#080d14] px-3 py-2 font-mono text-[11px] text-slate-400">
        {patch.content}
      </pre>
      {patch.apply_command ? (
        <div className="mt-2">
          <p className="text-[11px] uppercase tracking-wide text-slate-600">
            Apply (never run by this platform)
          </p>
          <pre className="mt-1 overflow-x-auto rounded bg-[#080d14] px-3 py-2 font-mono text-[11px] text-amber-200/80">
            {patch.apply_command}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The remediation plan for the leading hypothesis.
 *
 * Ordered the way an operator works: verify, change, confirm, undo. Risk and
 * blast radius are shown before the commands, and generated artifacts are
 * copyable but never applied from here.
 */
export function RemediationPlanPanel({ diagnosis }: { diagnosis?: Diagnosis }) {
  const plan = diagnosis?.remediation ?? null;

  if (!plan) {
    return (
      <Panel
        title="Remediation Plan"
        subtitle="Derived from the leading hypothesis."
      >
        <EmptyState message="No remediation plan was produced for this investigation." />
      </Panel>
    );
  }

  return (
    <Panel
      title="Remediation Plan"
      subtitle={plan.title}
      action={
        <div className="flex flex-wrap items-center gap-2">
          <Tag
            label={`${plan.risk.level} risk`}
            className={RISK_TONE[plan.risk.level] ?? RISK_TONE.Low}
          />
          {plan.requires_approval ? (
            <Tag
              label="approval required"
              className="border-amber-800 bg-amber-950/40 text-amber-300"
            />
          ) : null}
        </div>
      }
    >
      <div className="grid gap-4">
        <p className="text-sm leading-6 text-slate-300">{plan.summary}</p>

        <dl className="grid gap-2 rounded-md border border-slate-800 bg-[#101722] px-4 py-3 text-xs sm:grid-cols-2">
          {[
            ["Target", formatTarget(plan.target)],
            ["Change type", plan.risk.change_kind],
            ["Restart required", plan.risk.restart_required ? "Yes" : "No"],
            ["Estimated downtime", plan.risk.estimated_downtime],
            ["Blast radius", plan.risk.blast_radius],
            ["Reversible", plan.risk.reversible ? "Yes" : "Not automatically"],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="uppercase tracking-wide text-slate-500">{label}</dt>
              <dd className="mt-0.5 text-slate-300">{value}</dd>
            </div>
          ))}
        </dl>

        {plan.risk.notes.length ? (
          <ul className="grid gap-1.5">
            {plan.risk.notes.map((note) => (
              <li
                key={note}
                className="rounded-md border border-slate-800 bg-[#101722] px-3 py-2 text-xs text-slate-400"
              >
                {note}
              </li>
            ))}
          </ul>
        ) : null}

        {plan.caveats.length ? (
          <ul className="grid gap-1.5">
            {plan.caveats.map((caveat) => (
              <li
                key={caveat}
                className="rounded-md border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200"
              >
                {caveat}
              </li>
            ))}
          </ul>
        ) : null}

        {SECTIONS.map(({ key, title, hint }) => (
          <div key={key}>
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                {title}
              </p>
              <Tag label={hint} />
            </div>
            <div className="mt-2">
              <Steps steps={plan[key] as RemediationStep[]} />
            </div>
          </div>
        ))}

        {plan.required_permissions.length ? (
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Access needed — check before starting
            </p>
            <ul className="mt-2 grid gap-1.5">
              {plan.required_permissions.map((permission) => (
                <li key={permission.check_command}>
                  <pre className="overflow-x-auto rounded bg-[#080d14] px-3 py-2 font-mono text-[11px] text-slate-400">
                    {permission.check_command}
                  </pre>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {plan.patches.length ? (
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Generated artifacts
            </p>
            <div className="mt-2 grid gap-2">
              {plan.patches.map((patch) => (
                <PatchCard key={`${patch.format}-${patch.filename}`} patch={patch} />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
