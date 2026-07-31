import { describe, expect, it } from "vitest";

import {
  clusterOverview,
  evidenceIdForKind,
  securityWarnings,
  topConsumers,
} from "./cluster";
import type { InvestigationResponse } from "../types/investigation";

type Investigation = InvestigationResponse["investigation"];

const INVESTIGATION = {
  overview: {
    nodes: "5/6 Healthy",
    pods: "148 Running",
    cpu_usage: "64%",
    memory_usage: "71%",
  },
  pods: { problematic_pods: [{ name: "checkout-7d9f" }, { name: "web-0" }] },
  deployments: { unhealthy_deployments: [{ name: "checkout" }] },
  workloads: {
    inventory: [
      { kind: "statefulsets", desired: 3, ready: 3 },
      { kind: "daemonsets", desired: 4, ready: 3 },
      { kind: "cronjobs", desired: 1, ready: 1 },
    ],
  },
  storage: { findings: [{ issue: "PVC pending" }] },
  network: { findings: [] },
  nodes: { findings: [{ issue: "NotReady" }] },
  security: {
    findings: [
      { label: "No Privileged Containers", status: "pass", detail: "" },
      { label: "Containers as root", status: "warning", detail: "2 containers run as root" },
    ],
  },
  metrics: {
    top_pods: [
      { namespace: "payments", name: "checkout-7d9f", cpu: "240m", memory: "412Mi" },
    ],
  },
  evidence_coverage: { total: 11, usable: 9, completeness: 90 },
  evidence: [
    { id: "k8s.pods:cluster/a", kind: "k8s.pods", status: "ok" },
    { id: "k8s.nodes:cluster/a", kind: "k8s.nodes", status: "ok" },
    { id: "k8s.workloads:cluster/a", kind: "k8s.workloads", status: "ok" },
    { id: "k8s.deployments:cluster/a", kind: "k8s.deployments", status: "ok" },
    { id: "k8s.storage:cluster/a", kind: "k8s.storage", status: "ok" },
    { id: "k8s.metrics.nodes:cluster/a", kind: "k8s.metrics.nodes", status: "ok" },
  ],
} as unknown as Investigation;

describe("every figure can be traced", () => {
  it("names the evidence kind each figure was read from", () => {
    // The rule that keeps this from becoming a resource browser: a figure with
    // no evidence behind it does not belong on the page.
    const groups = clusterOverview(INVESTIGATION);
    const capacity = groups.find((group) => group.title === "Capacity");

    expect(capacity?.figures.find((f) => f.label === "Nodes")?.kind).toBe("k8s.nodes");
    expect(capacity?.figures.find((f) => f.label === "Pods")?.kind).toBe("k8s.pods");
  });

  it("resolves a kind to the record that produced it", () => {
    expect(evidenceIdForKind(INVESTIGATION, "k8s.nodes")).toBe("k8s.nodes:cluster/a");
  });

  it("returns nothing for a kind that was never collected", () => {
    expect(evidenceIdForKind(INVESTIGATION, "prometheus.pod")).toBeUndefined();
  });
});

describe("groups", () => {
  it("omits a group with nothing behind it rather than padding it", () => {
    // Same rule the report composer applies. A page full of "N/A" teaches an
    // operator to stop reading it.
    const groups = clusterOverview(INVESTIGATION).map((group) => group.title);
    expect(groups).toContain("Storage");
    expect(groups).not.toContain("Networking");
  });

  it("builds the census from inventory nothing rendered before", () => {
    const workloads = clusterOverview(INVESTIGATION).find((g) => g.title === "Workloads");
    const labels = workloads?.figures.map((f) => f.label);

    expect(labels).toContain("Statefulsets");
    expect(labels).toContain("Daemonsets");
    expect(workloads?.figures.find((f) => f.label === "Daemonsets")?.value).toContain(
      "not ready",
    );
  });

  it("marks failing pods as critical, not as a number", () => {
    const capacity = clusterOverview(INVESTIGATION).find((g) => g.title === "Capacity");
    expect(capacity?.figures.find((f) => f.label === "Failing pods")?.tone).toBe("critical");
  });

  it("reports coverage including the gap count", () => {
    const coverage = clusterOverview(INVESTIGATION).find((g) => g.title === "Coverage");
    expect(coverage?.figures.find((f) => f.label === "Usable evidence")?.value).toBe(
      "9 of 11",
    );
    expect(coverage?.figures.find((f) => f.label === "Gaps")?.value).toBe("2");
  });

  it("is empty for an investigation that established nothing", () => {
    expect(clusterOverview(undefined)).toEqual([]);
    expect(clusterOverview({} as Investigation)).toEqual([]);
  });

  it("hides a metric the cluster could not report", () => {
    const groups = clusterOverview({
      ...INVESTIGATION,
      overview: { nodes: "5/6 Healthy", pods: "148 Running", cpu_usage: "N/A" },
    } as Investigation);
    const capacity = groups.find((group) => group.title === "Capacity");
    expect(capacity?.figures.map((f) => f.label)).not.toContain("CPU");
  });

  it("prints no figure whose evidence was not usable", () => {
    // "Nodes: Unavailable" and "Pods: 0 Running" from a failed read state an
    // absence as though it were a measurement. The gap belongs on the
    // Evidence tab, where it is reported as one.
    const groups = clusterOverview({
      ...INVESTIGATION,
      evidence: [
        { id: "k8s.nodes:cluster/a", kind: "k8s.nodes", status: "failed" },
        { id: "k8s.pods:cluster/a", kind: "k8s.pods", status: "unavailable" },
      ],
    } as unknown as Investigation);

    expect(groups.find((group) => group.title === "Capacity")).toBeUndefined();
  });

  it("reports the same number of warnings as it lists", () => {
    // These read from one investigation and used to disagree: the count
    // matched status === "warning", the list matched status !== "pass".
    const security = clusterOverview(INVESTIGATION).find((g) => g.title === "Security");
    const counted = Number(security?.figures.find((f) => f.label === "Warnings")?.value);
    expect(counted).toBe(securityWarnings(INVESTIGATION).length);
  });
});

describe("security", () => {
  it("lists only the checks that did not pass", () => {
    const warnings = securityWarnings(INVESTIGATION);
    expect(warnings).toHaveLength(1);
    expect(warnings[0].label).toBe("Containers as root");
  });
});

describe("top consumers", () => {
  it("comes from what kubectl top reported", () => {
    expect(topConsumers(INVESTIGATION)[0].name).toBe("checkout-7d9f");
  });

  it("is empty when metrics were unavailable", () => {
    expect(topConsumers({} as Investigation)).toEqual([]);
  });
});
