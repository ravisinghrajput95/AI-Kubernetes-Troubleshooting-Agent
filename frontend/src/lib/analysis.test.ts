import { describe, expect, it } from "vitest";

import {
  describeCollectionCache,
  filterEvidence,
  formatDuration,
  formatTarget,
  groupEvidenceByKind,
  groupSignalsByDomain,
  humanizeKind,
  rankHypotheses,
  sortSignals,
  totalContribution,
} from "./analysis";
import type {
  ConfidenceComponent,
  EvidenceEntry,
  Hypothesis,
  Severity,
  Signal,
} from "../types/investigation";

function signal(id: string, severity: Severity, domain = "pod"): Signal {
  return {
    id,
    type: `${domain}.thing`,
    domain,
    severity,
    summary: `${id} summary`,
    target: { kind: "Pod", name: "web-0", namespace: "prod" },
    evidence_ids: ["k8s.pods:cluster/_cluster/test"],
  };
}

function evidence(
  id: string,
  kind: string,
  status: EvidenceEntry["status"],
  command: string | null = null,
): EvidenceEntry {
  return {
    id,
    kind,
    source: "kubectl",
    status,
    target: { kind: "Cluster", name: "test" },
    detail: "",
    command,
    collector_id: kind,
    duration_ms: 12,
    redacted: true,
    collected_at: "2026-07-26T10:00:00Z",
  };
}

function hypothesis(id: string, severity: Severity, confidence: number): Hypothesis {
  return {
    id,
    title: id,
    category: "workload",
    severity,
    confidence,
    rationale: "",
    target: { kind: "Pod", name: "web-0", namespace: "prod" },
    supporting_signals: [],
    refuting_signals: [],
    missing_evidence: [],
    remediation_hint: "",
  };
}

describe("sortSignals", () => {
  it("orders by severity, then id for stability", () => {
    const sorted = sortSignals([
      signal("b", "low"),
      signal("a", "critical"),
      signal("c", "critical"),
    ]);
    expect(sorted.map((item) => item.id)).toEqual(["a", "c", "b"]);
  });

  it("does not mutate its input", () => {
    const input = [signal("b", "low"), signal("a", "critical")];
    sortSignals(input);
    expect(input.map((item) => item.id)).toEqual(["b", "a"]);
  });
});

describe("groupSignalsByDomain", () => {
  it("puts the most severe domain first", () => {
    const groups = groupSignalsByDomain([
      signal("net-1", "low", "network"),
      signal("pod-1", "critical", "pod"),
    ]);
    expect(groups.map(([domain]) => domain)).toEqual(["pod", "network"]);
  });

  it("falls back to the type prefix when domain is absent", () => {
    const bare = { ...signal("x", "high"), domain: "" };
    expect(groupSignalsByDomain([bare])[0][0]).toBe("pod");
  });
});

describe("groupEvidenceByKind", () => {
  it("surfaces kinds with degraded evidence first", () => {
    const groups = groupEvidenceByKind([
      evidence("a", "k8s.pods", "ok"),
      evidence("b", "k8s.nodes", "forbidden"),
    ]);
    expect(groups[0].kind).toBe("k8s.nodes");
    expect(groups[0].degraded).toBe(1);
  });

  it("counts empty evidence as usable", () => {
    const groups = groupEvidenceByKind([evidence("a", "k8s.storage", "empty")]);
    expect(groups[0].usable).toBe(1);
    expect(groups[0].degraded).toBe(0);
  });
});

describe("filterEvidence", () => {
  const entries = [
    evidence("a", "k8s.pods", "ok", "kubectl get pods -A -o json"),
    evidence("b", "k8s.nodes", "forbidden", "kubectl get nodes -o json"),
  ];

  it("matches on kind, id, and command", () => {
    expect(filterEvidence(entries, "nodes", false)).toHaveLength(1);
    expect(filterEvidence(entries, "get pods", false)[0].id).toBe("a");
  });

  it("is case insensitive and ignores surrounding whitespace", () => {
    expect(filterEvidence(entries, "  NODES  ", false)).toHaveLength(1);
  });

  it("restricts to gaps when requested", () => {
    const gaps = filterEvidence(entries, "", true);
    expect(gaps).toHaveLength(1);
    expect(gaps[0].status).toBe("forbidden");
  });

  it("returns everything for an empty query", () => {
    expect(filterEvidence(entries, "", false)).toHaveLength(2);
  });
});

describe("rankHypotheses", () => {
  it("orders by severity then confidence", () => {
    const ranked = rankHypotheses([
      hypothesis("low-sev-high-conf", "medium", 95),
      hypothesis("high-sev-low-conf", "critical", 50),
      hypothesis("high-sev-high-conf", "critical", 80),
    ]);
    expect(ranked.map((item) => item.id)).toEqual([
      "high-sev-high-conf",
      "high-sev-low-conf",
      "low-sev-high-conf",
    ]);
  });
});

describe("totalContribution", () => {
  it("sums weighted contributions", () => {
    const components: ConfidenceComponent[] = [
      { component: "Evidence", weight: 70, score: 92, contribution: 64, detail: "" },
      { component: "Completeness", weight: 30, score: 100, contribution: 30, detail: "" },
    ];
    expect(totalContribution(components)).toBe(94);
  });

  it("is zero for no components", () => {
    expect(totalContribution([])).toBe(0);
  });
});

describe("formatting helpers", () => {
  it("formats namespaced and cluster-scoped targets", () => {
    expect(formatTarget({ kind: "Pod", name: "web-0", namespace: "prod" })).toBe(
      "Pod/prod/web-0",
    );
    expect(formatTarget({ kind: "Node", name: "node-1" })).toBe("Node/node-1");
    expect(formatTarget(undefined)).toBe("cluster");
  });

  it("formats durations across the second boundary", () => {
    expect(formatDuration(120)).toBe("120ms");
    expect(formatDuration(1500)).toBe("1.5s");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("humanizes evidence kinds", () => {
    expect(humanizeKind("k8s.pod.logs.previous")).toBe("Pod Logs Previous");
    expect(humanizeKind("k8s.pods")).toBe("Pods");
  });
});

describe("describeCollectionCache", () => {
  it("says nothing when every read was live", () => {
    // A line reading "0 reused" on every investigation trains people to stop
    // reading it, which is how a genuinely stale run would go unnoticed.
    expect(
      describeCollectionCache({
        enabled: true,
        hits: 0,
        misses: 20,
        oldest_evidence_seconds: null,
      }),
    ).toBeNull();
  });

  it("says nothing when caching is off", () => {
    expect(
      describeCollectionCache({
        enabled: false,
        hits: 0,
        misses: 0,
        oldest_evidence_seconds: null,
      }),
    ).toBeNull();
  });

  it("leads with the age of the oldest fact the diagnosis rests on", () => {
    const described = describeCollectionCache({
      enabled: true,
      hits: 17,
      misses: 3,
      oldest_evidence_seconds: 42,
    });
    expect(described?.label).toBe("17 of 20 reads reused");
    expect(described?.detail).toContain("42s");
  });

  it("reads minutes once seconds stop being useful", () => {
    expect(
      describeCollectionCache({
        enabled: true,
        hits: 1,
        misses: 0,
        oldest_evidence_seconds: 305,
      })?.detail,
    ).toContain("5m");
  });

  it("still reports reuse when the backend sent no age", () => {
    const described = describeCollectionCache({
      enabled: true,
      hits: 4,
      misses: 1,
      oldest_evidence_seconds: null,
    });
    expect(described?.label).toBe("4 of 5 reads reused");
    expect(described?.detail).not.toContain("null");
  });
});
