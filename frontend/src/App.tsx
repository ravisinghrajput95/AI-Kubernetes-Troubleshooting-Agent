import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router";

import {
  getHealth,
  getInvestigationReport,
  getInvestigationHistory,
  getKubernetesContexts,
  startInvestigationJob,
  regenerateInvestigationReport,
  reportUrl,
} from "./services/api";
import type {
  Diagnosis,
  InvestigationReport,
  InvestigationResponse,
  KubernetesContext,
} from "./types/investigation";
import {
  acknowledgeInsecure,
  getToken,
  isInsecureAcknowledged,
  onTokenChange,
} from "./services/auth";
import { useScope } from "./hooks/useScope";
import { useDocumentTitle } from "./hooks/useDocumentTitle";
import { AppShell } from "./components/shell/AppShell";
import { AskPage } from "./routes/AskPage";
import { ConnectClusterPage } from "./routes/ConnectClusterPage";
import { ClusterPage } from "./routes/ClusterPage";
import { FleetPage } from "./routes/FleetPage";
import { InvestigatePage } from "./routes/InvestigatePage";
import { ReportsPage } from "./routes/ReportsPage";
import { SettingsPage } from "./routes/SettingsPage";
import { useInvestigationJob } from "./hooks/useInvestigationJob";
import { SignIn } from "./components/SignIn";
import { EvidenceInspector } from "./components/report/EvidenceInspector";
import { ReportDocument } from "./components/report/ReportDocument";
import { SeverityDot } from "./components/report/SeverityDot";
import { evidenceIndex, severityTone } from "./lib/report";
import { ConfidenceBreakdown } from "./components/ConfidenceBreakdown";
import { EvidenceExplorer } from "./components/EvidenceExplorer";
import { HypothesisPanel } from "./components/HypothesisPanel";
import { LiveTimeline } from "./components/LiveTimeline";
import { PlaybookRounds } from "./components/PlaybookRounds";
import { RemediationPlanPanel } from "./components/RemediationPlanPanel";
import { SignalTable } from "./components/SignalTable";

type InvestigationData = InvestigationResponse["investigation"];



function StatusPill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "good" | "warning" | "info";
}) {
  const classes = {
    neutral: "border-slate-700 bg-slate-900 text-slate-300",
    good: "border-lime-800 bg-lime-950/40 text-lime-300",
    warning: "border-amber-800 bg-amber-950/40 text-amber-300",
    info: "border-sky-800 bg-sky-950/40 text-sky-300",
  };

  return (
    <span
      className={`inline-flex items-center rounded-md border px-3 py-1.5 text-xs font-semibold ${classes[tone]}`}
    >
      {label}
    </span>
  );
}

function ArtifactsPanel({
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

function MultiClusterPanel({
  selectedContext,
  clusterStatuses,
  onSelectContext,
  onInvestigateAll,
  isInvestigating,
}: {
  selectedContext: string;
  clusterStatuses: Record<string, string>;
  onSelectContext: (context: string) => void;
  onInvestigateAll: (contexts: string[]) => void;
  isInvestigating: boolean;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
  });

  function statusFor(context: KubernetesContext) {
    const knownStatus = clusterStatuses[context.name];
    if (knownStatus) {
      return knownStatus;
    }
    if (context.name === selectedContext) {
      return "Selected";
    }
    if (context.current) {
      return "Healthy";
    }
    return "Ready";
  }

  function toneFor(status: string) {
    if (status === "Selected" || status === "Healthy") {
      return "text-lime-300";
    }
    if (status === "Critical") {
      return "text-red-300";
    }
    if (status === "Warning" || status === "Running") {
      return "text-amber-300";
    }
    return "text-sky-300";
  }

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">Multi-Cluster Dashboard</h2>
          <p className="mt-1 text-sm text-slate-400">
            Kubeconfig contexts as an AI Operations Center view.
          </p>
        </div>
        <button
          type="button"
          onClick={() => onInvestigateAll(data?.items?.map((item) => item.name) ?? [])}
          disabled={!data?.items.length || isInvestigating}
          className="rounded-md border border-cyan-800 bg-cyan-950/30 px-4 py-2 text-sm font-semibold text-cyan-200 disabled:cursor-not-allowed disabled:border-slate-800 disabled:bg-slate-900 disabled:text-slate-500"
        >
          Investigate All Clusters
        </button>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {isLoading ? (
          <p className="text-sm text-slate-400">Loading clusters...</p>
        ) : null}
        {data?.items?.map((context) => {
          const status = statusFor(context);
          return (
            <button
              key={context.name}
              type="button"
              onClick={() => onSelectContext(context.name)}
              className="rounded-md border border-slate-800 bg-[#101722] p-4 text-left text-sm hover:border-slate-600"
            >
              <p className="truncate font-semibold text-slate-100">{context.name}</p>
              <p className="mt-2 truncate text-xs text-slate-500">{context.cluster}</p>
              <p className={`mt-3 text-xs font-semibold uppercase tracking-wide ${toneFor(status)}`}>
                {status}
              </p>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function downloadText(filename: string, content: string, type = "text/plain") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function firstAffectedWorkload(investigation?: InvestigationData) {
  const pods = investigation?.pods as
    | { problematic_pods?: Array<{ name?: string; namespace?: string }> }
    | undefined;
  const deployments = investigation?.deployments as
    | { unhealthy_deployments?: Array<{ name?: string; namespace?: string }> }
    | undefined;
  const pod = pods?.problematic_pods?.[0];
  const deployment = deployments?.unhealthy_deployments?.[0];

  return {
    name: deployment?.name ?? pod?.name ?? "<deployment-name>",
    namespace: deployment?.namespace ?? pod?.namespace ?? "<namespace>",
    kind: deployment ? "Deployment" : pod ? "Pod" : "Deployment",
  };
}

function buildRemediationYaml(
  diagnosis?: Diagnosis,
  investigation?: InvestigationData,
) {
  const workload = firstAffectedWorkload(investigation);
  // `a?.b.c()` guards `a` and not `b`: a diagnosis without a root cause used
  // to crash the whole page here. The backend types this dict as
  // `dict[str, Any]`, so the interface saying it is required proves nothing.
  const rootCause = diagnosis?.root_cause?.toLowerCase() ?? "";
  const imageValue = rootCause.includes("image")
    ? "<replace-with-valid-image-tag>"
    : "<validated-image-tag>";

  if (workload.kind === "Pod") {
    return `apiVersion: v1
kind: Pod
metadata:
  name: ${workload.name}
  namespace: ${workload.namespace}
spec:
  containers:
    - name: <container-name>
      image: ${imageValue}
      resources:
        requests:
          cpu: "100m"
          memory: "128Mi"
        limits:
          cpu: "500m"
          memory: "512Mi"`;
  }

  return `apiVersion: apps/v1
kind: ${workload.kind}
metadata:
  name: ${workload.name}
  namespace: ${workload.namespace}
spec:
  template:
    spec:
      containers:
        - name: <container-name>
          image: ${imageValue}
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"`;
}

function buildPrDescription(diagnosis?: Diagnosis) {
  return `# Kubernetes Remediation

## Root Cause
${diagnosis?.root_cause ?? "Run an investigation to generate a root cause."}

## Fix
${diagnosis?.fix ?? "Run an investigation to generate a suggested fix."}

## Validation Commands
${(diagnosis?.kubectl_commands ?? ["kubectl get pods -A"])
  .map((command) => `- \`${command}\``)
  .join("\n")}

## Risk
${diagnosis?.remediation_risk?.level ?? "Pending"}
`;
}

function buildApplyPlan(diagnosis?: Diagnosis, investigation?: InvestigationData) {
  const workload = firstAffectedWorkload(investigation);
  const commands = diagnosis?.kubectl_commands?.length
    ? diagnosis.kubectl_commands
    : ["kubectl get pods -A", "kubectl get events -A --sort-by=.lastTimestamp"];

  return [
    `Target: ${workload.kind} ${workload.namespace}/${workload.name}`,
    "",
    "Review the generated YAML and replace any placeholder values before applying.",
    "",
    "Recommended commands:",
    ...commands.map((command) => `- ${command}`),
    "",
    "Validation:",
    `- kubectl get pods -n ${workload.namespace}`,
    `- kubectl get events -n ${workload.namespace} --sort-by=.lastTimestamp`,
  ].join("\n");
}

function RemediationPanel({
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

function investigationEvidence(investigation?: InvestigationData) {
  const pods = investigation?.pods as
    | { problematic_pods?: Array<{ name?: string; namespace?: string; status?: string }> }
    | undefined;
  const events = investigation?.events as
    | { findings?: Array<{ reason?: string; message?: string; object?: string }> }
    | undefined;
  const logs = investigation?.logs as
    | {
        logs?: Array<{
          name?: string;
          namespace?: string;
          relevant_lines?: string[];
          error?: string;
        }>;
      }
    | undefined;
  const deployments = investigation?.deployments as
    | {
        unhealthy_deployments?: Array<{
          name?: string;
          namespace?: string;
          unavailable_replicas?: number;
        }>;
      }
    | undefined;
  const securityWarnings =
    investigation?.security?.findings?.filter((finding) => finding.status === "warning") ?? [];
  const firstPod = pods?.problematic_pods?.[0];
  const firstEvent = events?.findings?.[0];
  const firstLog = logs?.logs?.find((item) => item.relevant_lines?.length);
  const firstDeployment = deployments?.unhealthy_deployments?.[0];

  return {
    firstDeployment,
    firstEvent,
    firstLog,
    firstPod,
    securityWarnings,
  };
}

function ReportPreview({
  report,
  onClose,
}: {
  report: InvestigationReport;
  onClose: () => void;
}) {
  const metadata = report.report_metadata;
  const timeline = report.investigation.timeline ?? [];
  const impact = metadata?.business_impact ?? [];
  const confidence = metadata?.confidence_breakdown ?? [];
  const evidence = metadata?.evidence_matrix ?? [];

  return (
    <section className="rounded-lg border border-cyan-900/70 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-cyan-300">
            Report Preview
          </p>
          <h2 className="mt-2 text-xl font-semibold text-slate-100">
            {report.incident_id ?? "Investigation Report"}
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            {metadata?.cluster ?? "Current Context"}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-slate-700 px-3 py-2 text-sm font-semibold text-slate-300 hover:border-slate-500"
        >
          Close
        </button>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-4">
        {[
          ["Severity", metadata?.severity ?? "Unknown"],
          ["Status", metadata?.incident_status ?? report.status],
          ["Environment", metadata?.environment ?? "Unknown"],
          ["Confidence", `${report.diagnosis.confidence}%`],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border border-slate-800 bg-[#101722] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {label}
            </p>
            <p className="mt-2 text-sm font-semibold text-slate-100">{value}</p>
          </div>
        ))}
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">Root Cause</h3>
          <p className="mt-3 text-sm leading-6 text-slate-300">
            {report.diagnosis.root_cause}
          </p>
          <h3 className="mt-5 text-sm font-semibold text-slate-100">
            Business Impact
          </h3>
          <div className="mt-3 grid gap-2">
            {(impact.length ? impact : ["No business impact recorded."]).map((item) => (
              <p key={item} className="text-sm leading-6 text-slate-400">
                {item}
              </p>
            ))}
          </div>
        </div>

        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            AI Confidence Breakdown
          </h3>
          <div className="mt-3 grid gap-2">
            {confidence.map((item) => (
              <div key={item.source} className="grid grid-cols-[1fr_auto] gap-3 text-sm">
                <span className="text-slate-400">{item.source}</span>
                <span className="font-semibold text-cyan-200">
                  {item.contribution}%
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-2">
        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">Evidence Matrix</h3>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[420px] text-left text-sm">
              <tbody>
                {evidence.map((item) => (
                  <tr key={item.source} className="border-t border-slate-900">
                    <td className="py-2 pr-4 text-slate-400">{item.source}</td>
                    <td className="py-2 font-semibold text-slate-100">{item.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-md border border-slate-800 bg-[#080d14] p-4">
          <h3 className="text-sm font-semibold text-slate-100">
            Investigation Timeline
          </h3>
          <div className="mt-3 grid gap-2">
            {timeline.map((item) => (
              <p key={`${item.time}-${item.message}`} className="text-sm text-slate-400">
                <span className="mr-3 font-mono text-cyan-200">{item.time}</span>
                {item.message}
              </p>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

export function HistoryTable() {
  const { data = [], isLoading } = useQuery({
    queryKey: ["investigation-history"],
    queryFn: getInvestigationHistory,
  });
  const queryClient = useQueryClient();
  const [selectedReportId, setSelectedReportId] = useState("");

  const report = useQuery({
    queryKey: ["investigation-report", selectedReportId],
    queryFn: () => getInvestigationReport(selectedReportId),
    enabled: Boolean(selectedReportId),
  });

  const regenerate = useMutation({
    mutationFn: regenerateInvestigationReport,
    onSuccess: (updatedReport) => {
      queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
      queryClient.setQueryData(["investigation-report", selectedReportId], updatedReport);
    },
  });

  return (
    <div className="grid gap-5">
      {report.data ? (
        <ReportPreview report={report.data} onClose={() => setSelectedReportId("")} />
      ) : null}

      <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
        <h2 className="font-semibold text-slate-100">Recent Investigations</h2>
        <p className="mt-1 text-sm text-slate-400">
          Completed investigations are saved as SRE incident reports.
        </p>

        <div className="mt-5 overflow-x-auto">
          <table className="w-full min-w-[1080px] border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-xs uppercase tracking-wide text-slate-500">
              <th className="py-3 pr-4 font-semibold">Incident</th>
              <th className="py-3 pr-4 font-semibold">Timestamp</th>
              <th className="py-3 pr-4 font-semibold">Root Cause</th>
              <th className="py-3 pr-4 font-semibold">Namespace</th>
              <th className="py-3 pr-4 font-semibold">Severity</th>
              <th className="py-3 pr-4 font-semibold">Environment</th>
              <th className="py-3 pr-4 font-semibold">Confidence</th>
              <th className="py-3 pr-4 font-semibold">Status</th>
              <th className="py-3 pr-4 font-semibold">Actions</th>
              <th className="py-3 font-semibold">Report</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td className="py-4 text-slate-400" colSpan={10}>
                  Loading history...
                </td>
              </tr>
            ) : null}
            {!isLoading && data.length === 0 ? (
              <tr>
                <td className="py-4 text-slate-400" colSpan={10}>
                  No investigations yet.
                </td>
              </tr>
            ) : null}
            {data.map((item) => (
              <tr key={item.id} className="border-b border-slate-900">
                <td className="py-3 pr-4 font-mono text-xs text-cyan-200">
                  {item.incident_id ?? item.id.slice(0, 8)}
                </td>
                <td className="py-3 pr-4 text-slate-400">
                  {new Date(item.timestamp).toLocaleString()}
                </td>
                <td className="max-w-md py-3 pr-4 font-medium text-slate-100">
                  {item.root_cause}
                </td>
                <td className="py-3 pr-4 text-slate-400">{item.namespace}</td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.severity ?? "Unknown"}
                </td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.environment ?? "Unknown"}
                </td>
                <td className="py-3 pr-4 text-slate-400">{item.confidence}%</td>
                <td className="py-3 pr-4 text-slate-400">
                  {item.incident_status ?? item.status}
                </td>
                <td className="py-3 pr-4">
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setSelectedReportId(item.id)}
                      className="font-medium text-cyan-300 underline underline-offset-4"
                    >
                      Preview
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedReportId(item.id);
                        regenerate.mutate(item.id);
                      }}
                      className="font-medium text-lime-300 underline underline-offset-4 disabled:text-slate-600"
                      disabled={regenerate.isPending}
                    >
                      Regenerate
                    </button>
                  </div>
                </td>
                <td className="py-3">
                  <div className="flex gap-2">
                    <a
                      href={reportUrl(item.pdf_url)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium text-cyan-300 underline underline-offset-4"
                    >
                      PDF
                    </a>
                    {item.json_url ? (
                      <a
                        href={reportUrl(item.json_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium text-violet-300 underline underline-offset-4"
                      >
                        JSON
                      </a>
                    ) : null}
                    {item.markdown_url ? (
                      <a
                        href={reportUrl(item.markdown_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium text-amber-300 underline underline-offset-4"
                      >
                        MD
                      </a>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      </section>
    </div>
  );
}

/**
 * One investigation, at its own address.
 *
 * Renders a run that is still collecting, one that has finished, and one that
 * has been evicted from the job store and is served from its persisted report
 * — the backend answers all three from the same id, so this page does too.
 */
export function InvestigationPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const job = useInvestigationJob();
  const { attach } = job;

  useEffect(() => {
    if (id) {
      void attach(id);
    }
  }, [attach, id]);

  const terminal =
    job.phase === "succeeded" || job.phase === "failed" || job.phase === "cancelled";

  // The composition arrives with the persisted report, which is written before
  // the job reaches a terminal state — including a failed one.
  const report = useQuery({
    queryKey: ["investigation-report", id],
    queryFn: () => getInvestigationReport(id),
    enabled: Boolean(id) && terminal,
    retry: false,
  });

  const selectedEvidence = params.get("ev") ?? "";
  const selectEvidence = useCallback(
    (evidenceId: string) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (evidenceId && evidenceId !== next.get("ev")) {
            next.set("ev", evidenceId);
          } else {
            next.delete("ev");
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const investigation = job.investigation;
  const diagnosis = job.diagnosis;
  const evidence = evidenceIndex(investigation).get(selectedEvidence);

  useDocumentTitle(
    investigation?.context
      ? `${investigation.context} · ${job.isRunning ? "running" : job.phase}`
      : "Investigation",
  );

  // Severity is derived from findings, so a run that collected nothing has no
  // findings and reports "Healthy". Showing that next to a failure notice
  // would have the header contradict the body — the same misrepresentation the
  // grounding checks exist to prevent, moved into the UI. The outcome wins.
  const outcome =
    job.phase === "failed"
      ? { tone: "critical" as const, label: "Failed" }
      : job.phase === "cancelled"
        ? { tone: "neutral" as const, label: "Cancelled" }
        : investigation?.severity?.severity
          ? {
              tone: severityTone(investigation.severity.severity),
              label: investigation.severity.severity,
            }
          : null;

  return (
    <div className="flex min-h-full">
      <div className="min-w-0 flex-1">
        <div className="mx-auto max-w-document px-6 py-8">
          <Link
            to="/"
            className="text-sm text-ink-3 transition-colors duration-fast hover:text-ink-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
          >
            ← Fleet
          </Link>

          <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="truncate text-display">
                {investigation?.context || "Investigation"}
              </h1>
              <p className="mt-1 font-mono text-sm text-ink-3">{id}</p>
            </div>
            <div className="flex items-center gap-3">
              {outcome ? <SeverityDot tone={outcome.tone} label={outcome.label} /> : null}
              {job.isRunning ? (
                <button
                  type="button"
                  onClick={() => void job.cancel()}
                  className="rounded-md border border-line bg-raised px-3 py-1.5 text-sm transition-colors duration-fast hover:border-critical hover:text-critical focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
                >
                  Cancel
                </button>
              ) : null}
            </div>
          </div>

          {job.error ? (
            <p role="alert" className="mt-5 rounded-md border border-critical/40 bg-critical/5 px-4 py-3 text-sm text-critical">
              {job.error}
            </p>
          ) : null}

          {!terminal ? (
            <div className="mt-8">
              <LiveTimeline
                phase={job.phase}
                transport={job.transport}
                timeline={job.timeline}
                onCancel={() => void job.cancel()}
              />
            </div>
          ) : null}

          {terminal ? (
            <div className="mt-8">
              {report.isLoading ? (
                <ReportSkeleton />
              ) : (
                <ReportDocument
                  composition={report.data?.report}
                  diagnosis={diagnosis}
                  investigation={investigation}
                  selectedEvidence={selectedEvidence}
                  onSelectEvidence={selectEvidence}
                />
              )}
            </div>
          ) : null}

          {terminal && diagnosis ? (
            <div className="mt-8">
              <RemediationPanel diagnosis={diagnosis} investigation={investigation} />
            </div>
          ) : null}

          {terminal && job.historyItem ? (
            <div className="mt-8">
              <ArtifactsPanel historyItem={job.historyItem} />
            </div>
          ) : null}

        </div>
      </div>

      {selectedEvidence ? (
        <aside className="hidden w-[400px] shrink-0 border-l border-line-muted bg-surface xl:block">
          <div className="sticky top-14 max-h-[calc(100vh-3.5rem)] overflow-y-auto">
            <EvidenceInspector
              evidence={evidence}
              diagnosis={diagnosis}
              onClose={() => selectEvidence("")}
            />
          </div>
        </aside>
      ) : null}

      {/* Below the three-column breakpoint the inspector is an overlay, so it
          never squeezes the document narrower than it can be read at. */}
      {selectedEvidence ? (
        <div className="fixed inset-0 z-40 bg-black/60 xl:hidden" onClick={() => selectEvidence("")}>
          <div
            className="absolute inset-y-0 right-0 w-full max-w-md overflow-y-auto bg-surface"
            onClick={(event) => event.stopPropagation()}
          >
            <EvidenceInspector
              evidence={evidence}
              diagnosis={diagnosis}
              onClose={() => selectEvidence("")}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Matches the document's box model, so nothing reflows on arrival. */
function ReportSkeleton() {
  return (
    <div className="grid gap-8" aria-hidden="true">
      {[0, 1, 2].map((block) => (
        <div key={block} className="grid gap-3">
          <div className="h-4 w-40 rounded bg-raised" />
          <div className="h-3 w-full max-w-measure rounded bg-line-muted" />
          <div className="h-3 w-3/4 max-w-measure rounded bg-line-muted" />
        </div>
      ))}
    </div>
  );
}

function AuthenticatedApp() {
  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
  });

  const [token, setTokenState] = useState(getToken);
  const [authError, setAuthError] = useState("");
  const [acknowledged, setAcknowledged] = useState(isInsecureAcknowledged);

  // `http.ts` clears the credential when the backend answers 401, which fires
  // here. That is what turns an expired token into a sign-in prompt rather
  // than a screen full of failed requests.
  useEffect(
    () =>
      onTokenChange((next) => {
        setTokenState(next);
        setAcknowledged(isInsecureAcknowledged());
        if (!next) {
          setAuthError("Your session is no longer valid. Sign in again to continue.");
        }
      }),
    [],
  );

  const insecure = health?.insecure ?? false;
  const reachable = !isError && health !== undefined;

  // A backend that needs no credential still gets acknowledged once per tab,
  // so a dangerous configuration is visible rather than silent.
  if (reachable && insecure && !acknowledged) {
    return (
      <SignIn
        health={health}
        onAuthenticated={() => {
          acknowledgeInsecure();
          setAcknowledged(true);
        }}
      />
    );
  }

  if (!insecure && !token) {
    return (
      <SignIn
        health={reachable ? health : undefined}
        error={authError}
        onAuthenticated={() => setAuthError("")}
      />
    );
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<FleetPage />} />
        <Route path="/clusters/:context" element={<ClusterPage />} />
        <Route path="/investigations" element={<InvestigatePage />} />
        <Route path="/connect" element={<ConnectClusterPage />} />
        <Route path="/investigations/:id" element={<InvestigationPage />} />
        <Route path="/ask" element={<AskPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        {/* Destinations arrive as later phases give them data. Until then an
            unknown path returns to the one page that exists rather than 404. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

function App() {
  return <AuthenticatedApp />;
}

export default App;
