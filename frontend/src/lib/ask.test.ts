import { describe, expect, it } from "vitest";

import {
  recurredOnOneCluster,
  recurringFindings,
  search,
  sharedAcrossClusters,
  trendLabel,
  trendOf,
  type CorpusEntry,
} from "./ask";

const entry = (
  id: string,
  cluster: string,
  at: string,
  types: string[],
): CorpusEntry => ({
  investigationId: id,
  cluster,
  at,
  rootCause: "Memory limit too low",
  signals: types.map((type) => ({
    type,
    summary: `${type} observed`,
    severity: "critical",
  })),
});

describe("recurrence", () => {
  it("reports a finding seen in more than one investigation", () => {
    const findings = recurringFindings([
      entry("1", "prod", "2026-07-01T00:00:00Z", ["pod.crash_loop"]),
      entry("2", "prod", "2026-07-02T00:00:00Z", ["pod.crash_loop"]),
    ]);

    expect(findings).toHaveLength(1);
    expect(findings[0].occurrences).toHaveLength(2);
  });

  it("does not report a single occurrence as a pattern", () => {
    // One occurrence is an incident, and it is already on its own page.
    const findings = recurringFindings([
      entry("1", "prod", "2026-07-01T00:00:00Z", ["pod.crash_loop"]),
    ]);
    expect(findings).toEqual([]);
  });

  it("counts one investigation once, however noisy it was", () => {
    // Otherwise a single run raising the same signal repeatedly looks like a
    // pattern across time.
    const findings = recurringFindings([
      entry("1", "prod", "2026-07-01T00:00:00Z", ["pod.crash_loop", "pod.crash_loop"]),
      entry("2", "prod", "2026-07-02T00:00:00Z", ["pod.crash_loop"]),
    ]);
    expect(findings[0].occurrences).toHaveLength(2);
  });

  it("records the first and last time it was seen", () => {
    const findings = recurringFindings([
      entry("2", "prod", "2026-07-09T00:00:00Z", ["x"]),
      entry("1", "prod", "2026-07-01T00:00:00Z", ["x"]),
    ]);
    expect(findings[0].firstSeen).toBe("2026-07-01T00:00:00Z");
    expect(findings[0].lastSeen).toBe("2026-07-09T00:00:00Z");
  });

  it("puts what spans the most clusters first", () => {
    const findings = recurringFindings([
      entry("1", "a", "2026-07-01T00:00:00Z", ["wide", "narrow"]),
      entry("2", "b", "2026-07-02T00:00:00Z", ["wide", "narrow"]),
      entry("3", "c", "2026-07-03T00:00:00Z", ["wide"]),
    ]);
    expect(findings.map((finding) => finding.type)).toEqual(["wide", "narrow"]);
  });
});

describe("grounding", () => {
  it("names every investigation a claim was counted from", () => {
    // The structural form of the grounding contract: an answer is only ever a
    // list of occurrences that were actually found. There is no path that
    // produces a claim without the runs behind it.
    const findings = recurringFindings([
      entry("run-a", "prod", "2026-07-01T00:00:00Z", ["x"]),
      entry("run-b", "staging", "2026-07-02T00:00:00Z", ["x"]),
    ]);

    expect(findings[0].occurrences.map((item) => item.investigationId)).toEqual([
      "run-a",
      "run-b",
    ]);
    expect(findings[0].occurrences.every((item) => item.investigationId)).toBe(true);
  });

  it("says nothing at all about an empty corpus", () => {
    expect(recurringFindings([])).toEqual([]);
  });

  it("ignores a signal with no type rather than grouping it as blank", () => {
    const findings = recurringFindings([
      { ...entry("1", "prod", "2026-07-01T00:00:00Z", []), signals: [{ type: "", summary: "", severity: "" }] },
      { ...entry("2", "prod", "2026-07-02T00:00:00Z", []), signals: [{ type: "", summary: "", severity: "" }] },
    ]);
    expect(findings).toEqual([]);
  });
});

describe("trend", () => {
  const occurrence = (at: string) => ({ investigationId: at, cluster: "prod", at });
  const days = (...list: number[]) =>
    list.map((day) => occurrence(`2026-07-${String(day).padStart(2, "0")}T00:00:00Z`));

  it("refuses to call two data points a trend", () => {
    // Confident overclaim from thin evidence is exactly what this product
    // exists not to do.
    expect(trendOf(days(1), days(1, 2, 3, 4))).toBe("unknown");
    expect(trendOf(days(1, 2), days(1, 2, 3, 4))).toBe("unknown");
  });

  it("does not read a finding present in every run as falling", () => {
    // The live corpus: three runs in the first minute and one five minutes
    // later, the finding in all four. Judged by when occurrences fell, that
    // was "happening less often"; judged by share of runs it is constant.
    const runs = [
      occurrence("2026-09-14T17:47:00Z"),
      occurrence("2026-09-14T17:47:30Z"),
      occurrence("2026-09-14T17:48:00Z"),
      occurrence("2026-09-14T17:53:00Z"),
    ];
    expect(trendOf(runs, runs)).not.toBe("falling");
    const everyRun = days(1, 2, 3, 9, 10);
    expect(trendOf(everyRun, everyRun)).toBe("steady");
  });

  it("sees a finding appearing in a growing share of runs as rising", () => {
    const runs = days(1, 2, 3, 4, 7, 8, 9, 10);
    expect(trendOf(days(1, 7, 8, 9, 10), runs)).toBe("rising");
  });

  it("sees a shrinking share as falling", () => {
    const runs = days(1, 2, 3, 4, 7, 8, 9, 10);
    expect(trendOf(days(1, 2, 3, 4, 10), runs)).toBe("falling");
  });

  it("will not compare a half with a single run in it", () => {
    expect(trendOf(days(1, 2, 3), days(1, 2, 3, 10))).toBe("unknown");
  });

  it("does not count runs that could not have seen the finding as its absence", () => {
    // The live console: runs whose agent could not reach its API server, and
    // runs scoped to one deployment, early on; the finding in every run that
    // read its namespace. Every finding on the page read "happening more often".
    const at = (day: number) => `2026-07-${String(day).padStart(2, "0")}T00:00:00Z`;
    const archiver = [{ type: "pod.pending", summary: "archiver Pending", severity: "high", namespace: "payments" }];
    const corpus: CorpusEntry[] = [
      { ...entry("1", "prod", at(1), []), collected: false },
      { ...entry("2", "prod", at(2), []), collected: false },
      { ...entry("3", "prod", at(3), []), scope: { namespace: "payments", resource_kind: "deployment", resource_name: "checkout" } },
      { ...entry("4", "prod", at(4), []), scope: { namespace: "kube-system", resource_kind: "cluster" } },
      { ...entry("5", "prod", at(5), []), signals: archiver },
      { ...entry("6", "prod", at(6), []), signals: archiver, scope: { namespace: "payments" } },
      { ...entry("7", "prod", at(9), []), signals: archiver },
      { ...entry("8", "prod", at(10), []), signals: archiver },
    ];

    const [finding] = recurringFindings(corpus);

    expect(finding.namespaces).toEqual(["payments"]);
    expect(finding.trend).not.toBe("rising");
  });

  it("still counts a whole-cluster run that read the namespace and found nothing", () => {
    const at = (day: number) => `2026-07-${String(day).padStart(2, "0")}T00:00:00Z`;
    const oom = [{ type: "pod.oom_killed", summary: "OOM", severity: "critical", namespace: "payments" }];
    const corpus: CorpusEntry[] = [
      { ...entry("1", "prod", at(1), []) },
      { ...entry("2", "prod", at(2), []), scope: { namespace: "payments" } },
      { ...entry("3", "prod", at(3), []), signals: oom },
      { ...entry("4", "prod", at(8), []), signals: oom },
      { ...entry("5", "prod", at(9), []), signals: oom },
      { ...entry("6", "prod", at(10), []), signals: oom },
    ];

    expect(recurringFindings(corpus)[0].trend).toBe("rising");
  });

  it("says so plainly when there is not enough history", () => {
    expect(trendLabel("unknown")).toMatch(/not enough history/i);
  });
});

describe("across clusters", () => {
  it("splits into two disjoint lists so nothing is answered twice", () => {
    // A finding under both headings is the same answer printed twice, and the
    // reader has to work out it is not two problems.
    const findings = recurringFindings([
      entry("1", "prod", "2026-07-01T00:00:00Z", ["shared", "local"]),
      entry("2", "staging", "2026-07-02T00:00:00Z", ["shared"]),
      entry("3", "prod", "2026-07-03T00:00:00Z", ["local"]),
    ]);

    const shared = sharedAcrossClusters(findings).map((item) => item.type);
    const local = recurredOnOneCluster(findings).map((item) => item.type);

    expect(shared).toEqual(["shared"]);
    expect(local).toEqual(["local"]);
    expect(shared.filter((type) => local.includes(type))).toEqual([]);
  });

  it("keeps only what appeared on more than one", () => {
    const findings = recurringFindings([
      entry("1", "prod", "2026-07-01T00:00:00Z", ["shared", "local"]),
      entry("2", "staging", "2026-07-02T00:00:00Z", ["shared"]),
      entry("3", "prod", "2026-07-03T00:00:00Z", ["local"]),
    ]);

    expect(sharedAcrossClusters(findings).map((item) => item.type)).toEqual(["shared"]);
  });
});

describe("search", () => {
  const findings = recurringFindings([
    entry("1", "prod-eu-west", "2026-07-01T00:00:00Z", ["image.no_pull_secret"]),
    entry("2", "staging", "2026-07-02T00:00:00Z", ["image.no_pull_secret"]),
  ]);

  it("matches the finding's own text", () => {
    expect(search(findings, "image")).toHaveLength(1);
  });

  it("matches a cluster name", () => {
    expect(search(findings, "prod-eu")).toHaveLength(1);
  });

  it("returns nothing rather than something plausible", () => {
    // A query that matches nothing must not fall back to an approximate
    // answer; the page says so instead.
    expect(search(findings, "database deadlock")).toEqual([]);
  });

  it("returns everything for an empty query", () => {
    expect(search(findings, "  ")).toHaveLength(1);
  });
});

describe("names for the same cluster", () => {
  it("counts clusters, not the names that reach them", () => {
    const corpus = [
      entry("1", "kind-dev", "2026-07-01T00:00:00Z", ["pod.crash_loop"]),
      entry("2", "sweep-agent", "2026-07-02T00:00:00Z", ["pod.crash_loop"]),
    ];
    const sameNodes = (cluster: string) => (cluster === "sweep-agent" ? "kind-dev" : cluster);

    const findings = recurringFindings(corpus, sameNodes);

    expect(findings[0].distinct).toBe(1);
    expect(sharedAcrossClusters(findings)).toEqual([]);
    expect(recurredOnOneCluster(findings)).toHaveLength(1);
  });
});
