/**
 * What an investigation says about the cluster as a whole.
 *
 * The line this holds against becoming a resource browser: **every figure here
 * can be clicked to see the evidence record and the command that produced it.**
 * A figure with no evidence behind it does not belong on the page. That is
 * enforceable rather than stylistic, and it is what a live-polled resource tree
 * can never offer — this is a snapshot from an investigation, timestamped, not
 * a reading of the cluster now.
 */

import { isGap, severityTone, type SeverityTone } from "./report";
import type { InvestigationResponse } from "../types/investigation";

type Investigation = InvestigationResponse["investigation"];

export interface Figure {
  label: string;
  value: string;
  tone?: SeverityTone;
  /** Evidence kind this figure was read from; resolved to a record for citation. */
  kind?: string;
}

export interface OverviewGroup {
  title: string;
  figures: Figure[];
}

/** The evidence record a figure was read from, if it was collected. */
export function evidenceIdForKind(
  investigation: Investigation | undefined,
  kind: string,
): string | undefined {
  return investigation?.evidence?.find((record) => record.kind === kind)?.id;
}

/**
 * Whether a figure read from this kind can be believed.
 *
 * "Every figure is citable" is only worth anything if it cites something
 * *usable*. A read that failed produces no number worth printing — showing
 * "Nodes: Unavailable" or "Pods: 0 Running" from a failed read states an
 * absence as though it were a measurement. The gap belongs on the Evidence
 * tab, where it is reported as a gap.
 */
export function kindIsUsable(investigation: Investigation | undefined, kind: string): boolean {
  const record = investigation?.evidence?.find((item) => item.kind === kind);
  return record !== undefined && !isGap(record.status);
}

function count(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function problemTone(n: number): SeverityTone {
  return n > 0 ? "warning" : "healthy";
}

/**
 * Group figures by domain, omitting any group with nothing behind it.
 *
 * Same rule the report composer applies: sections with nothing in them are
 * omitted, not padded. A cluster page full of "N/A" teaches an operator to
 * stop reading it.
 */
export function clusterOverview(investigation?: Investigation): OverviewGroup[] {
  if (!investigation) {
    return [];
  }

  const overview = investigation.overview ?? {};
  const pods = (investigation.pods ?? {}) as Record<string, unknown>;
  const workloads = (investigation.workloads ?? {}) as Record<string, unknown>;
  const storage = (investigation.storage ?? {}) as Record<string, unknown>;
  const network = (investigation.network ?? {}) as Record<string, unknown>;
  const nodes = (investigation.nodes ?? {}) as Record<string, unknown>;
  const security = investigation.security ?? {};
  const coverage = investigation.evidence_coverage;

  const groups: OverviewGroup[] = [];

  const capacity: Figure[] = [];
  if (overview.nodes && kindIsUsable(investigation, "k8s.nodes")) {
    capacity.push({
      label: "Nodes",
      value: overview.nodes,
      kind: "k8s.nodes",
      tone: count(nodes.findings) > 0 ? "warning" : undefined,
    });
  }
  if (overview.pods && kindIsUsable(investigation, "k8s.pods")) {
    const problematic = count(pods.problematic_pods);
    capacity.push({
      label: "Pods",
      value: overview.pods,
      kind: "k8s.pods",
      tone: problematic > 0 ? "critical" : undefined,
    });
    if (problematic > 0) {
      capacity.push({
        label: "Failing pods",
        value: String(problematic),
        tone: "critical",
        kind: "k8s.pods",
      });
    }
  }
  if (
    overview.cpu_usage &&
    overview.cpu_usage !== "N/A" &&
    kindIsUsable(investigation, "k8s.metrics.nodes")
  ) {
    capacity.push({ label: "CPU", value: overview.cpu_usage, kind: "k8s.metrics.nodes" });
  }
  if (
    overview.memory_usage &&
    overview.memory_usage !== "N/A" &&
    kindIsUsable(investigation, "k8s.metrics.nodes")
  ) {
    capacity.push({ label: "Memory", value: overview.memory_usage, kind: "k8s.metrics.nodes" });
  }
  if (capacity.length > 0) {
    groups.push({ title: "Capacity", figures: capacity });
  }

  // The census comes from `workloads.inventory`, which every investigation
  // collects and which nothing rendered before this page existed.
  const inventory = (workloads.inventory ?? []) as Array<Record<string, unknown>>;
  const census: Figure[] = [];
  const unhealthyDeployments = count(
    (investigation.deployments as Record<string, unknown> | undefined)?.unhealthy_deployments,
  );
  if (unhealthyDeployments > 0 || inventory.length > 0) {
    if (unhealthyDeployments > 0) {
      census.push({
        label: "Unhealthy deployments",
        value: String(unhealthyDeployments),
        tone: "critical",
        kind: "k8s.deployments",
      });
    }
    for (const kind of ["statefulsets", "daemonsets", "jobs", "cronjobs"]) {
      const items = inventory.filter((item) => item.kind === kind);
      if (items.length === 0) {
        continue;
      }
      const failing = items.filter(
        (item) => Number(item.ready ?? 0) < Number(item.desired ?? 0),
      ).length;
      census.push({
        label: kind.charAt(0).toUpperCase() + kind.slice(1),
        value: failing > 0 ? `${items.length} · ${failing} not ready` : String(items.length),
        tone: failing > 0 ? "warning" : undefined,
        kind: "k8s.workloads",
      });
    }
  }
  if (census.length > 0) {
    groups.push({ title: "Workloads", figures: census });
  }

  const storageFindings = count(storage.findings);
  if (storageFindings > 0) {
    groups.push({
      title: "Storage",
      figures: [
        {
          label: "Findings",
          value: String(storageFindings),
          tone: problemTone(storageFindings),
          kind: "k8s.storage",
        },
      ],
    });
  }

  const networkFindings = count(network.findings);
  if (networkFindings > 0) {
    groups.push({
      title: "Networking",
      figures: [
        {
          label: "Findings",
          value: String(networkFindings),
          tone: problemTone(networkFindings),
          kind: "k8s.network",
        },
      ],
    });
  }

  const securityFindings = security.findings ?? [];
  if (securityFindings.length > 0) {
    // Same predicate the list below uses. These read from one investigation
    // and must not report different numbers of the same thing.
    const warnings = securityFindings.filter((item) => item.status !== "pass").length;
    groups.push({
      title: "Security",
      figures: [
        {
          label: "Checks",
          value: String(securityFindings.length),
        },
        {
          label: "Warnings",
          value: String(warnings),
          tone: problemTone(warnings),
        },
      ],
    });
  }

  if (coverage?.total) {
    const gaps = coverage.total - coverage.usable;
    groups.push({
      title: "Coverage",
      figures: [
        {
          label: "Usable evidence",
          value: `${coverage.usable} of ${coverage.total}`,
          tone: coverage.usable === 0 ? "critical" : undefined,
        },
        {
          label: "Completeness",
          value: `${coverage.completeness ?? 0}%`,
          tone: severityTone(coverage.completeness && coverage.completeness > 80 ? "ok" : "warning"),
        },
        ...(gaps > 0
          ? [{ label: "Gaps", value: String(gaps), tone: "warning" as SeverityTone }]
          : []),
      ],
    });
  }

  return groups;
}

export interface Consumer {
  namespace: string;
  name: string;
  cpu: string;
  memory: string;
}

export function topConsumers(investigation?: Investigation): Consumer[] {
  return (investigation?.metrics?.top_pods ?? []).slice(0, 8);
}

/** Security checks that did not pass, which are the only ones worth listing. */
export function securityWarnings(
  investigation?: Investigation,
): Array<{ label: string; detail: string }> {
  return (investigation?.security?.findings ?? [])
    .filter((finding) => finding.status !== "pass")
    .map((finding) => ({ label: finding.label, detail: finding.detail }));
}
