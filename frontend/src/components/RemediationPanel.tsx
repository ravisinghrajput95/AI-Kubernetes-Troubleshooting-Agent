import { useEffect, useState } from "react";

import {
  buildApplyPlan,
  buildPrDescription,
  buildRemediationYaml,
  downloadText,
} from "../lib/remediation";
import type { Diagnosis, InvestigationData } from "../types/investigation";
import { StatusPill } from "./StatusPill";

export function RemediationPanel({
  diagnosis,
  investigation,
}: {
  diagnosis?: Diagnosis;
  investigation?: InvestigationData;
}) {
  const [yaml, setYaml] = useState(() => buildRemediationYaml(diagnosis, investigation));
  const [actionMessage, setActionMessage] = useState("");
  const [actionDetails, setActionDetails] = useState("");
  const [actionMode, setActionMode] = useState<"idle" | "yaml" | "apply">("idle");

  useEffect(() => {
    setYaml(buildRemediationYaml(diagnosis, investigation));
    setActionMessage("");
    setActionDetails("");
    setActionMode("idle");
  }, [diagnosis, investigation]);

  async function prepareApplyCommands() {
    const content = buildApplyPlan(diagnosis, investigation);
    if (!content) {
      setActionMessage("Run an investigation first to get apply commands.");
      return;
    }

    setActionDetails(content);
    setActionMode("apply");
    try {
      await navigator.clipboard.writeText(content);
      setActionMessage(
        "Apply plan prepared and copied. Review placeholders before running these commands.",
      );
    } catch {
      setActionMessage("Apply plan prepared. Copy the commands below after reviewing them.");
    }
  }

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-semibold text-slate-100">Recommended Fixes</h2>
        <StatusPill
          label={`Remediation Risk: ${diagnosis?.remediation_risk?.level ?? "Pending"}`}
          tone={diagnosis?.remediation_risk?.level === "Medium" ? "warning" : "good"}
        />
      </div>
      <div className="mt-4 rounded-md border border-slate-800 bg-[#101722] p-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Impact
        </p>
        {diagnosis?.remediation_risk?.impact?.length ? (
          <ul className="mt-3 space-y-2 text-sm text-slate-300">
            {diagnosis.remediation_risk.impact.map((item) => (
              <li key={item}>- {item}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-slate-500">
            No impact assessment was reported.
          </p>
        )}
      </div>
      <ol className="mt-4 space-y-2 text-sm text-slate-300">
        <li>1. {diagnosis?.fix ?? "Run an investigation to generate a fix."}</li>
        <li>2. Restart or roll out the affected deployment.</li>
        <li>3. Verify pods, events, and service endpoints after the change.</li>
      </ol>
      <pre className="mt-4 overflow-x-auto rounded-md border border-slate-800 bg-[#080d14] p-4 text-xs text-slate-300">
        {yaml}
      </pre>
      {actionMessage ? (
        <div className="mt-3 rounded-md border border-cyan-900/70 bg-cyan-950/30 px-3 py-2 text-sm text-cyan-200">
          {actionMessage}
        </div>
      ) : null}
      {actionMode === "apply" && actionDetails ? (
        <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[#080d14] p-4 text-xs leading-5 text-slate-300">
          {actionDetails}
        </pre>
      ) : null}
      <div className="mt-4 flex flex-wrap gap-3">
        <button
          type="button"
          onClick={() => {
            setYaml(buildRemediationYaml(diagnosis, investigation));
            setActionMessage("YAML fix generated from the current diagnosis.");
            setActionDetails("");
            setActionMode("yaml");
          }}
          className="rounded-md border border-violet-800 bg-violet-950/30 px-4 py-2 text-sm font-semibold text-violet-200"
        >
          Generate YAML Fix
        </button>
        <button
          type="button"
          onClick={prepareApplyCommands}
          className="rounded-md border border-sky-800 bg-sky-950/30 px-4 py-2 text-sm font-semibold text-sky-200"
        >
          Apply Fix
        </button>
        <button
          type="button"
          onClick={() =>
            downloadText("kubernetes-remediation-pr.md", buildPrDescription(diagnosis), "text/markdown")
          }
          className="rounded-md border border-slate-700 bg-slate-900 px-4 py-2 text-sm font-semibold text-slate-200"
        >
          Create GitHub PR
        </button>
        <button
          type="button"
          onClick={() => downloadText("kubernetes-remediation-patch.yaml", yaml, "text/yaml")}
          className="rounded-md border border-amber-800 bg-amber-950/30 px-4 py-2 text-sm font-semibold text-amber-200"
        >
          Download Patch
        </button>
      </div>
    </section>
  );
}
