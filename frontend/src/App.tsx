import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getHealth,
  getInvestigationReport,
  getInvestigationHistory,
  getKubernetesContexts,
  investigateCluster,
  regenerateInvestigationReport,
  reportUrl,
} from "./services/api";
import type {
  Diagnosis,
  InvestigationReport,
  InvestigationResponse,
  KubernetesContext,
} from "./types/investigation";
import { useInvestigationJob } from "./hooks/useInvestigationJob";
import { ConfidenceBreakdown } from "./components/ConfidenceBreakdown";
import { EvidenceExplorer } from "./components/EvidenceExplorer";
import { HypothesisPanel } from "./components/HypothesisPanel";
import { LiveTimeline } from "./components/LiveTimeline";
import { PlaybookRounds } from "./components/PlaybookRounds";
import { RemediationPlanPanel } from "./components/RemediationPlanPanel";
import { SignalTable } from "./components/SignalTable";

type InvestigationData = InvestigationResponse["investigation"];

const logoSrc = "/ai-kubernetes-agent-logo.svg";

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

function LoginScreen({ onLogin }: { onLogin: (name: string) => void }) {
  const [name, setName] = useState("admin");

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#080d14] px-5 text-slate-100">
      <section className="w-full max-w-md rounded-lg border border-slate-800 bg-[#0d131c] p-8 shadow-2xl shadow-black/30">
        <div className="mb-6 flex items-center gap-3">
          <img
            src={logoSrc}
            alt="AI Kubernetes Agent logo"
            className="size-12 rounded-md"
          />
          <div>
            <p className="font-semibold">AI Kubernetes Agent</p>
            <p className="text-sm text-lime-300">Online</p>
          </div>
        </div>

        <h1 className="text-2xl font-semibold">Open troubleshooting console</h1>
        <p className="mt-2 text-sm leading-6 text-slate-400">
          Minimal local login for the troubleshooting dashboard.
        </p>

        <label className="mt-6 block text-sm font-medium text-slate-300">
          Display name
        </label>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="mt-2 w-full rounded-md border border-slate-700 bg-[#111823] px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
        />

        <button
          type="button"
          onClick={() => onLogin(name.trim() || "admin")}
          className="mt-6 w-full rounded-md bg-cyan-400 px-4 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300"
        >
          Open Dashboard
        </button>
      </section>
    </main>
  );
}

function Sidebar({
  selectedContext,
  onSelectContext,
}: {
  selectedContext: string;
  onSelectContext: (context: string) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
  });

  useEffect(() => {
    if (!selectedContext && data?.current_context) {
      onSelectContext(data.current_context);
    }
  }, [data?.current_context, onSelectContext, selectedContext]);

  return (
    <aside className="w-full border-b border-slate-800 bg-[#0b1119] lg:min-h-screen lg:w-80 lg:border-b-0 lg:border-r">
      <div className="border-b border-slate-800 p-5">
        <div className="flex items-center gap-3">
          <img
            src={logoSrc}
            alt="AI Kubernetes Agent logo"
            className="size-12 rounded-md"
          />
          <div>
            <p className="font-semibold text-slate-100">AI Kubernetes Agent</p>
            <p className="text-sm text-lime-300">Online</p>
          </div>
        </div>
      </div>

      <div className="p-5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Kubeconfig Contexts
            </p>
            <p className="mt-1 text-xs leading-5 text-slate-500">
              Contexts are loaded from your local kubeconfig. Each context points
              to a cluster.
            </p>
          </div>
          <StatusPill label={`${data?.items.length ?? 0}`} />
        </div>

        {isLoading ? (
          <p className="mt-4 text-sm text-slate-400">Loading clusters...</p>
        ) : null}

        {data?.error ? (
          <div className="mt-4 rounded-md border border-red-900/70 bg-red-950/40 p-3 text-sm text-red-200">
            {data.error}
          </div>
        ) : null}

        <div className="mt-4 space-y-2">
          {data?.items.map((context) => (
            <button
              key={context.name}
              type="button"
              onClick={() => onSelectContext(context.name)}
              className={`w-full rounded-md border px-3 py-3 text-left text-sm transition ${
                selectedContext === context.name
                  ? "border-cyan-400 bg-cyan-950/30 text-cyan-200 shadow-sm shadow-cyan-950"
                  : "border-slate-800 bg-[#0f1621] text-slate-300 hover:border-slate-600"
              }`}
            >
              <span className="flex items-center justify-between gap-3">
                <span className="font-medium">{context.name}</span>
                {context.current ? (
                  <span className="rounded bg-lime-500/15 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-lime-300">
                    Current
                  </span>
                ) : null}
              </span>
              <span className="mt-2 block text-xs text-slate-500">
                Cluster target: {context.cluster}
              </span>
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}

function ClusterHealthOverview({
  overview,
}: {
  overview?: {
    nodes?: string;
    pods?: string;
    cpu_usage?: string;
    memory_usage?: string;
    alerts?: number;
    critical_issues?: number;
  };
}) {
  const items = [
    ["Nodes", overview?.nodes ?? "Not checked", "text-sky-200"],
    ["Pods", overview?.pods ?? "Not checked", "text-violet-200"],
    ["CPU Usage", overview?.cpu_usage ?? "N/A", "text-amber-200"],
    ["Memory Usage", overview?.memory_usage ?? "N/A", "text-cyan-200"],
    ["Alerts", overview?.alerts ?? 0, "text-fuchsia-200"],
    ["Critical Issues", overview?.critical_issues ?? 0, "text-red-200"],
  ];

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Cluster Health Overview</h2>
      <p className="mt-1 text-sm text-slate-400">
        Immediate operational snapshot from the selected context.
      </p>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        {items.map(([label, value, color]) => (
          <div key={label} className="rounded-md border border-slate-800 bg-[#101722] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {label}
            </p>
            <p className={`mt-2 text-lg font-semibold ${color}`}>{value}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function MetricsPanel({ metrics }: { metrics?: InvestigationData["metrics"] }) {
  const topPods = metrics?.top_pods ?? [];
  const nodeMetrics = metrics?.node_metrics ?? [];

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">Real Cluster Metrics</h2>
          <p className="mt-1 text-sm text-slate-400">
            {metrics?.message ?? "Metrics appear after a cluster investigation."}
          </p>
        </div>
        <StatusPill
          label={metrics?.available ? "metrics-server" : "Waiting"}
          tone={metrics?.available ? "good" : "neutral"}
        />
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2">
        <div className="rounded-md border border-slate-800 bg-[#101722] p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            CPU Usage
          </p>
          <p className="mt-2 text-2xl font-semibold text-amber-200">
            {metrics?.cpu_usage ?? "N/A"}
          </p>
        </div>
        <div className="rounded-md border border-slate-800 bg-[#101722] p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Memory Usage
          </p>
          <p className="mt-2 text-2xl font-semibold text-cyan-200">
            {metrics?.memory_usage ?? "N/A"}
          </p>
        </div>
      </div>

      {nodeMetrics.length ? (
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {nodeMetrics.slice(0, 6).map((node) => (
            <div
              key={node.name}
              className="rounded-md border border-slate-800 bg-[#080d14] p-3 text-sm"
            >
              <p className="truncate font-semibold text-slate-200">{node.name}</p>
              <p className="mt-2 text-slate-400">
                CPU {node.cpu_percent} · Memory {node.memory_percent}
              </p>
            </div>
          ))}
        </div>
      ) : null}

      {topPods.length ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-2 pr-4">Namespace</th>
                <th className="py-2 pr-4">Pod</th>
                <th className="py-2 pr-4">CPU</th>
                <th className="py-2">Memory</th>
              </tr>
            </thead>
            <tbody>
              {topPods.map((pod) => (
                <tr key={`${pod.namespace}-${pod.name}`} className="border-t border-slate-900">
                  <td className="py-2 pr-4 text-slate-400">{pod.namespace}</td>
                  <td className="py-2 pr-4 font-medium text-slate-200">{pod.name}</td>
                  <td className="py-2 pr-4 text-amber-200">{pod.cpu}</td>
                  <td className="py-2 text-cyan-200">{pod.memory}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function SeverityPanel({
  severity,
}: {
  severity?: {
    severity?: string;
    impact?: string;
    affected_workloads?: number;
    affected_namespace?: string;
  };
}) {
  const items = [
    ["Severity", severity?.severity ?? "Not assessed", "text-red-200", "border-red-900/70 bg-red-950/20"],
    ["Impact", severity?.impact ?? "Unknown", "text-orange-200", "border-orange-900/70 bg-orange-950/20"],
    ["Affected Workloads", severity?.affected_workloads ?? 0, "text-purple-200", "border-purple-900/70 bg-purple-950/20"],
    ["Affected Namespace", severity?.affected_namespace ?? "none", "text-cyan-200", "border-cyan-900/70 bg-cyan-950/20"],
  ];

  return (
    <section className="grid gap-4 md:grid-cols-4">
      {items.map(([label, value, color, panel]) => (
        <div key={label} className={`rounded-lg border p-4 ${panel}`}>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            {label}
          </p>
          <p className={`mt-2 truncate text-sm font-semibold ${color}`}>{value}</p>
        </div>
      ))}
    </section>
  );
}

function SecurityFindingsPanel({
  security,
}: {
  security?: InvestigationData["security"];
}) {
  const findings = security?.findings ?? [
    {
      label: "No Privileged Containers",
      status: "unknown" as const,
      detail: "Run an investigation to inspect pod security contexts.",
    },
    {
      label: "Latest Tag Used",
      status: "unknown" as const,
      detail: "Run an investigation to inspect container image tags.",
    },
    {
      label: "Missing Resource Limits",
      status: "unknown" as const,
      detail: "Run an investigation to inspect resource limits.",
    },
    {
      label: "High CVEs Found",
      status: "unknown" as const,
      detail: "Image vulnerability scan is not configured.",
    },
  ];

  const tone = {
    pass: "border-lime-800 bg-lime-950/30 text-lime-200",
    warning: "border-amber-800 bg-amber-950/30 text-amber-200",
    unknown: "border-slate-800 bg-[#101722] text-slate-300",
  };
  const marker = {
    pass: "✓",
    warning: "⚠",
    unknown: "○",
  };

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">Security Findings</h2>
          <p className="mt-1 text-sm text-slate-400">
            Pod-level DevSecOps checks from the selected cluster.
          </p>
        </div>
        <StatusPill
          label={`${security?.warning_count ?? 0} warnings`}
          tone={security?.warning_count ? "warning" : "good"}
        />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {findings.map((finding) => (
          <div
            key={finding.label}
            className={`rounded-md border p-4 text-sm ${tone[finding.status]}`}
          >
            <p className="font-semibold">
              <span className="mr-2">{marker[finding.status]}</span>
              {finding.label}
            </p>
            <p className="mt-2 leading-5 text-slate-400">{finding.detail}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function ConfidenceEvidence({ diagnosis }: { diagnosis?: Diagnosis }) {
  // Only reasoning the backend actually reported. Showing placeholder evidence
  // labels here would assert support that was never established.
  const signals = Array.isArray(diagnosis?.confidence_reasoning)
    ? diagnosis.confidence_reasoning
    : diagnosis?.confidence_reasoning
      ? [diagnosis.confidence_reasoning]
      : [];

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">AI Confidence Score</h2>
          <p className="mt-1 text-sm text-slate-400">
            Evidence used to support the root cause.
          </p>
        </div>
        <StatusPill label={`Confidence: ${diagnosis?.confidence ?? 0}%`} tone="info" />
      </div>
      {signals.length === 0 ? (
        <p className="mt-4 rounded-md border border-dashed border-slate-800 bg-[#101722] px-4 py-6 text-center text-sm text-slate-500">
          No confidence reasoning was reported for this investigation.
        </p>
      ) : (
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {signals.slice(0, 4).map((signal, index) => (
            <div
              key={`${signal}-${index}`}
              className="rounded-md border border-slate-800 bg-[#101722] px-4 py-3 text-sm text-slate-300"
            >
              <span className="mr-2 text-lime-300">✓</span>
              {signal}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function TimelinePanel({
  timeline = [],
}: {
  timeline?: Array<{ time: string; message: string }>;
}) {
  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Investigation Timeline</h2>
      <div className="mt-4 space-y-3">
        {timeline.length === 0 ? (
          <p className="text-sm text-slate-400">Timeline appears after a run.</p>
        ) : null}
        {timeline.map((item) => (
          <div key={`${item.time}-${item.message}`} className="flex gap-3 text-sm">
            <span className="w-20 shrink-0 text-slate-500">{item.time}</span>
            <span className="text-slate-300">{item.message}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function CommandsPanel({ commands = [] }: { commands?: string[] }) {
  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Evidence Collected</h2>
      <p className="mt-1 text-sm text-slate-400">
        kubectl commands executed by the investigation layer.
      </p>
      <div className="mt-4 space-y-2">
        {commands.length === 0 ? (
          <p className="text-sm text-slate-400">No commands captured yet.</p>
        ) : null}
        {commands.map((command) => (
          <code
            key={command}
            className="block overflow-x-auto rounded-md border border-slate-800 bg-[#080d14] px-4 py-3 text-xs text-cyan-200"
          >
            {command}
          </code>
        ))}
      </div>
    </section>
  );
}

function ClusterTopologyPanel({
  topology,
}: {
  topology?: InvestigationData["topology"];
}) {
  const nodes = topology?.nodes ?? [];

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <h2 className="font-semibold text-slate-100">Cluster Topology</h2>
      <p className="mt-1 text-sm text-slate-400">
        Node-to-pod placement for the selected context.
      </p>

      <div className="mt-5 rounded-md border border-slate-800 bg-[#080d14] p-4 font-mono text-xs leading-6 text-slate-300">
        <div className="text-cyan-200">
          Cluster: {topology?.cluster ?? "Not investigated"}
        </div>
        {nodes.length === 0 ? (
          <div className="mt-2 text-slate-500">No topology captured yet.</div>
        ) : null}
        {nodes.slice(0, 5).map((node, nodeIndex) => (
          <div key={node.name} className="mt-2">
            <div className="text-slate-200">
              {nodeIndex === nodes.length - 1 ? "└──" : "├──"} Node {node.name}
              <span className="ml-2 text-slate-500">({node.pod_count} pods)</span>
            </div>
            {node.pods.map((pod, podIndex) => (
              <div key={`${pod.namespace}-${pod.name}`} className="pl-6 text-slate-400">
                {podIndex === node.pods.length - 1 ? "└──" : "├──"} {pod.namespace}/{pod.name}
                <span className="ml-2 text-slate-500">{pod.phase}</span>
              </div>
            ))}
            {node.pod_count > node.pods.length ? (
              <div className="pl-6 text-slate-500">
                └── +{node.pod_count - node.pods.length} more pods
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </section>
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
          onClick={() => onInvestigateAll(data?.items.map((item) => item.name) ?? [])}
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
        {data?.items.map((context) => {
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
  const rootCause = diagnosis?.root_cause.toLowerCase() ?? "";
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

function IncidentAssistantPanel({
  diagnosis,
  investigation,
}: {
  diagnosis?: Diagnosis;
  investigation?: InvestigationData;
}) {
  const [selectedView, setSelectedView] = useState("evidence");
  const evidence = investigationEvidence(investigation);
  const assistantViews = {
    evidence: {
      title: "Evidence",
      lines: [
        diagnosis?.root_cause ?? "Run an investigation to generate evidence.",
        evidence.firstPod
          ? `Pod: ${evidence.firstPod.namespace}/${evidence.firstPod.name} is ${evidence.firstPod.status}`
          : "Pod: no problematic pod captured",
        evidence.firstEvent
          ? `Event: ${evidence.firstEvent.reason ?? "Warning"} - ${evidence.firstEvent.message ?? ""}`
          : "Event: no warning event captured",
        evidence.firstLog?.relevant_lines?.[0]
          ? `Log: ${evidence.firstLog.relevant_lines[0]}`
          : "Log: no relevant log line captured",
      ],
    },
    actions: {
      title: "Fix Plan",
      lines: [
        diagnosis?.fix ?? "Run an investigation to generate remediation guidance.",
        "Review generated YAML before applying.",
        "Replace placeholder values with validated production values.",
        "Verify pod status, rollout status, and recent events after the change.",
      ],
    },
    commands: {
      title: "Commands",
      lines: diagnosis?.kubectl_commands?.length
        ? diagnosis.kubectl_commands
        : ["No recommended kubectl commands returned yet."],
      code: true,
    },
    risk: {
      title: "Risk & Rollback",
      lines: [
        `Risk: ${diagnosis?.remediation_risk?.level ?? "Pending"}`,
        ...(diagnosis?.remediation_risk?.impact ?? ["Impact not assessed yet."]),
        "Rollback: keep the previous manifest or use rollout undo for managed deployments.",
      ],
    },
    security: {
      title: "Security",
      lines: evidence.securityWarnings.length
        ? evidence.securityWarnings.map(
            (finding) => `${finding.label}: ${finding.detail}`,
          )
        : [
            "No privileged-container, latest-tag, or missing-limit warnings were detected.",
            "CVE scanning is not configured in this local evidence collection.",
          ],
    },
    metrics: {
      title: "Metrics & Topology",
      lines: [
        `CPU: ${investigation?.metrics?.cpu_usage ?? "N/A"}`,
        `Memory: ${investigation?.metrics?.memory_usage ?? "N/A"}`,
        `Cluster: ${investigation?.topology?.cluster ?? "Not captured"}`,
        ...(investigation?.topology?.nodes?.slice(0, 4).map(
          (node) => `${node.name}: ${node.pod_count} pod(s)`,
        ) ?? ["No node placement captured."]),
      ],
    },
  } as const;
  const selected = assistantViews[selectedView as keyof typeof assistantViews];
  const selectedIsCode = "code" in selected && selected.code;

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">Incident Assistant</h2>
          <p className="mt-1 text-sm text-slate-400">
            Structured runbook views generated from collected evidence.
          </p>
        </div>
        <StatusPill label="Runbook" tone="info" />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2">
        {Object.entries(assistantViews).map(([key, view]) => (
          <button
            key={key}
            type="button"
            onClick={() => setSelectedView(key)}
            className={`rounded-md border px-3 py-2 text-left text-sm font-semibold transition ${
              selectedView === key
                ? "border-cyan-700 bg-cyan-950/40 text-cyan-200"
                : "border-slate-800 bg-[#101722] text-slate-400 hover:border-slate-600"
            }`}
          >
            {view.title}
          </button>
        ))}
      </div>

      <div className="mt-4 rounded-md border border-slate-800 bg-[#080d14] p-4">
        <h3 className="text-sm font-semibold text-slate-100">{selected.title}</h3>
        <div className="mt-3 space-y-2">
          {selected.lines.map((line, index) =>
            selectedIsCode ? (
              <code
                key={`${line}-${index}`}
                className="block overflow-x-auto rounded-md border border-slate-800 bg-[#050a10] px-3 py-2 text-xs text-cyan-200"
              >
                {line}
              </code>
            ) : (
              <p key={`${line}-${index}`} className="text-sm leading-6 text-slate-300">
                {line}
              </p>
            ),
          )}
        </div>
      </div>
    </section>
  );
}

function DiagnosisCard({ diagnosis }: { diagnosis: Diagnosis }) {
  const firstCommand = diagnosis.kubectl_commands[0] ?? "No command returned";
  const healthy = diagnosis.root_cause
    .toLowerCase()
    .includes("no critical kubernetes issues");

  return (
    <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Diagnosis
          </p>
          <h2 className="mt-2 text-xl font-semibold text-slate-100">
            {diagnosis.root_cause}
          </h2>
        </div>
        <span className="rounded-md border border-fuchsia-800 bg-fuchsia-950/40 px-3 py-1 text-sm font-semibold text-fuchsia-200">
          {diagnosis.confidence}% confidence
        </span>
      </div>

      {healthy ? (
        <div className="mt-5 rounded-md border border-lime-800 bg-lime-950/30 p-4 text-sm text-lime-200">
          No critical Kubernetes issues detected. Cluster appears healthy.
        </div>
      ) : null}

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <div>
          <p className="text-sm font-semibold text-slate-200">Explanation</p>
          <p className="mt-2 text-sm leading-6 text-slate-400">
            {diagnosis.explanation}
          </p>
        </div>
        <div>
          <p className="text-sm font-semibold text-slate-200">Suggested Fix</p>
          <p className="mt-2 text-sm leading-6 text-slate-400">{diagnosis.fix}</p>
        </div>
      </div>

      <div className="mt-5">
        <p className="text-sm font-semibold text-slate-200">kubectl Command</p>
        <code className="mt-2 block overflow-x-auto rounded-md border border-slate-800 bg-[#080d14] px-4 py-3 text-xs leading-5 text-slate-200">
          {firstCommand}
        </code>
      </div>

      {diagnosis.next_steps?.length ? (
        <div className="mt-5">
          <p className="text-sm font-semibold text-slate-200">Next Steps</p>
          <div className="mt-2 grid gap-2">
            {diagnosis.next_steps.slice(0, 3).map((step) => (
              <p
                key={step}
                className="rounded-md border border-slate-800 bg-[#080d14] px-4 py-3 text-sm leading-5 text-slate-300"
              >
                {step}
              </p>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
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

function HistoryTable() {
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

function Dashboard({ userName }: { userName: string }) {
  const queryClient = useQueryClient();
  const [selectedContext, setSelectedContext] = useState("");
  const [scopeNamespace, setScopeNamespace] = useState("");
  const [scopeKind, setScopeKind] = useState("");
  const [scopeName, setScopeName] = useState("");
  const [clusterStatuses, setClusterStatuses] = useState<Record<string, string>>({});
  const [isInvestigatingAll, setIsInvestigatingAll] = useState(false);

  const { data, isError: healthError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
  });

  // Investigations run as background jobs; progress streams from the backend.
  const job = useInvestigationJob();

  function startInvestigation(context?: string) {
    const target = context ?? selectedContext;
    if (context) {
      setSelectedContext(context);
    }
    void job.start(target, {
      namespace: scopeNamespace.trim() || undefined,
      resource_kind: scopeKind || undefined,
      resource_name: scopeName.trim() || undefined,
    });
  }

  useEffect(() => {
    if (job.phase !== "succeeded" || !job.investigation) {
      return;
    }

    const investigatedContext = job.investigation.context ?? selectedContext;
    const severity = job.investigation.severity?.severity;
    if (investigatedContext) {
      setClusterStatuses((current) => ({
        ...current,
        [investigatedContext]:
          severity === "Critical" || severity === "High"
            ? "Critical"
            : severity === "Healthy"
              ? "Healthy"
              : "Warning",
      }));
    }
    queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
    // selectedContext is intentionally excluded: this must fire once per result.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.phase, job.investigation, queryClient]);

  async function investigateAllClusters(contexts: string[]) {
    if (contexts.length === 0 || isInvestigatingAll) {
      return;
    }

    setIsInvestigatingAll(true);
    for (const context of contexts) {
      setSelectedContext(context);
      setClusterStatuses((current) => ({ ...current, [context]: "Running" }));
      try {
        const response = await investigateCluster(context);
        const severity = response.investigation.severity?.severity;
        setClusterStatuses((current) => ({
          ...current,
          [context]:
            severity === "Critical" || severity === "High"
              ? "Critical"
              : severity === "Healthy"
                ? "Healthy"
                : "Warning",
        }));
      } catch {
        setClusterStatuses((current) => ({ ...current, [context]: "Warning" }));
      }
    }
    setIsInvestigatingAll(false);
    queryClient.invalidateQueries({ queryKey: ["investigation-history"] });
  }

  const investigationData = job.investigation;
  const diagnosis = job.diagnosis;
  const healthMessage = investigationData?.health?.message;
  const healthStatus = investigationData?.health?.status;
  const systemStatus = useMemo(() => {
    if (healthError) {
      return "Backend Offline";
    }
    return data?.status === "healthy" ? "Ready" : "Checking";
  }, [data?.status, healthError]);

  return (
    <main className="min-h-screen bg-[#080d14] text-slate-100">
      <div className="flex min-h-screen flex-col lg:flex-row">
        <Sidebar
          selectedContext={selectedContext}
          onSelectContext={setSelectedContext}
        />

        <section className="min-w-0 flex-1">
          <header className="border-b border-slate-800 bg-[#0b1119] px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <p className="text-sm font-semibold text-slate-100">
                  Operations Dashboard
                </p>
                <p className="mt-1 text-sm text-slate-500">
                  {selectedContext
                    ? `Selected context: ${selectedContext}`
                    : "Select a kubeconfig context to begin"}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <StatusPill
                  label={systemStatus}
                  tone={systemStatus === "Ready" ? "good" : "warning"}
                />
                <StatusPill label={userName} tone="info" />
              </div>
            </div>
          </header>

          <div className="grid gap-5 p-5">
            <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-5 shadow-sm shadow-black/20">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-fuchsia-300">
                    Incident Response
                  </p>
                  <h1 className="mt-2 text-2xl font-semibold text-slate-100">
                    Investigate Kubernetes Cluster
                  </h1>
                  <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
                    Collect pod, log, event, deployment, and networking evidence,
                    then generate a root cause and a PDF investigation report.
                  </p>
                </div>
                <div className="grid w-full gap-3 lg:w-auto lg:min-w-[520px]">
                  <div className="grid gap-3 md:grid-cols-3">
                    <input
                      value={scopeNamespace}
                      onChange={(event) => setScopeNamespace(event.target.value)}
                      placeholder="Namespace"
                      className="rounded-md border border-slate-700 bg-[#111823] px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-400"
                    />
                    <select
                      value={scopeKind}
                      onChange={(event) => setScopeKind(event.target.value)}
                      className="rounded-md border border-slate-700 bg-[#111823] px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
                    >
                      <option value="">Cluster</option>
                      <option value="pod">Pod</option>
                      <option value="deployment">Deployment</option>
                    </select>
                    <input
                      value={scopeName}
                      onChange={(event) => setScopeName(event.target.value)}
                      placeholder="Resource name"
                      disabled={!scopeKind}
                      className="rounded-md border border-slate-700 bg-[#111823] px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-900 disabled:text-slate-600"
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => startInvestigation()}
                    disabled={job.isRunning || isInvestigatingAll || !selectedContext}
                    className="rounded-md bg-cyan-400 px-5 py-3 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
                  >
                    {job.isRunning || isInvestigatingAll
                      ? "Investigating..."
                      : "Investigate Cluster"}
                  </button>
                </div>
              </div>
            </section>

            <section className="grid gap-4 md:grid-cols-3">
              <div className="rounded-lg border border-sky-900/70 bg-sky-950/20 p-4">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Target Context
                </p>
                <p className="mt-2 truncate text-sm font-semibold text-sky-200">
                  {selectedContext || "Not selected"}
                </p>
              </div>
              <div className="rounded-lg border border-violet-900/70 bg-violet-950/20 p-4">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Investigation
                </p>
                <p className="mt-2 text-sm font-semibold text-violet-200">
                  {job.isRunning || isInvestigatingAll ? "Running" : "Ready"}
                </p>
              </div>
              <div className="rounded-lg border border-amber-900/70 bg-amber-950/20 p-4">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Last Result
                </p>
                <p className="mt-2 text-sm font-semibold text-amber-200">
                  {healthStatus ? healthStatus.replace("_", " ") : "No run yet"}
                </p>
              </div>
            </section>

            <ClusterHealthOverview overview={investigationData?.overview} />

            <SeverityPanel severity={investigationData?.severity} />

            <MultiClusterPanel
              selectedContext={selectedContext}
              clusterStatuses={clusterStatuses}
              onSelectContext={setSelectedContext}
              onInvestigateAll={investigateAllClusters}
              isInvestigating={job.isRunning || isInvestigatingAll}
            />

            <MetricsPanel metrics={investigationData?.metrics} />

            <SecurityFindingsPanel security={investigationData?.security} />

            {job.error ? (
              <div className="rounded-lg border border-red-900/70 bg-red-950/40 px-4 py-3 text-sm text-red-200">
                {job.error}
                <div className="mt-2 text-red-100">
                  Please verify kubeconfig path, cluster access, kubectl
                  permissions, and backend connectivity.
                </div>
              </div>
            ) : null}

            {healthMessage ? (
              <div className="rounded-lg border border-slate-800 bg-[#0d131c] px-4 py-3 text-sm text-slate-300">
                {healthMessage}
              </div>
            ) : null}

            <LiveTimeline
              phase={job.phase}
              transport={job.transport}
              timeline={job.timeline}
              onCancel={() => void job.cancel()}
            />

            {diagnosis ? (
              <div className="grid items-start gap-5 xl:grid-cols-[1.3fr_0.7fr]">
                <div className="grid gap-5">
                  <DiagnosisCard diagnosis={diagnosis} />
                  <HypothesisPanel diagnosis={diagnosis} />
                  <ConfidenceBreakdown diagnosis={diagnosis} />
                  <SignalTable diagnosis={diagnosis} />
                  <RemediationPlanPanel diagnosis={diagnosis} />
                  <ConfidenceEvidence diagnosis={diagnosis} />
                  <RemediationPanel
                    diagnosis={diagnosis}
                    investigation={investigationData}
                  />
                </div>
                <div className="grid gap-5 xl:sticky xl:top-5">
                  <IncidentAssistantPanel
                    diagnosis={diagnosis}
                    investigation={investigationData}
                  />
                  <PlaybookRounds investigation={investigationData} />
                </div>
              </div>
            ) : (
              <section className="rounded-lg border border-slate-800 bg-[#0d131c] p-8 text-center shadow-sm shadow-black/20">
                <p className="text-sm font-semibold text-slate-100">
                  No investigation has been run yet.
                </p>
                <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-slate-400">
                  Select a kubeconfig context from the sidebar and click
                  Investigate Cluster to start collecting evidence.
                </p>
              </section>
            )}

            <section className="grid gap-5 xl:grid-cols-2">
              <ClusterTopologyPanel topology={investigationData?.topology} />
              <TimelinePanel timeline={investigationData?.timeline} />
            </section>

            <EvidenceExplorer
              investigation={investigationData}
              citedEvidence={diagnosis?.cited_evidence}
            />

            <section className="grid gap-5 xl:grid-cols-2">
              <CommandsPanel commands={investigationData?.executed_commands} />
              <ArtifactsPanel historyItem={job.historyItem} />
            </section>

            <HistoryTable />
          </div>
        </section>
      </div>
    </main>
  );
}

function App() {
  const [userName, setUserName] = useState(
    window.localStorage.getItem("ai-k8s-user") ?? "",
  );

  if (!userName) {
    return (
      <LoginScreen
        onLogin={(name) => {
          window.localStorage.setItem("ai-k8s-user", name);
          setUserName(name);
        }}
      />
    );
  }

  return <Dashboard userName={userName} />;
}

export default App;
