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

  it("refuses to call two data points a trend", () => {
    // Confident overclaim from thin evidence is exactly what this product
    // exists not to do.
    expect(trendOf([occurrence("2026-07-01T00:00:00Z")])).toBe("unknown");
    expect(
      trendOf([occurrence("2026-07-01T00:00:00Z"), occurrence("2026-07-02T00:00:00Z")]),
    ).toBe("unknown");
  });

  it("sees a finding concentrated in its recent half as rising", () => {
    expect(
      trendOf([
        occurrence("2026-07-01T00:00:00Z"),
        occurrence("2026-07-09T00:00:00Z"),
        occurrence("2026-07-10T00:00:00Z"),
      ]),
    ).toBe("rising");
  });

  it("sees the reverse as falling", () => {
    expect(
      trendOf([
        occurrence("2026-07-01T00:00:00Z"),
        occurrence("2026-07-02T00:00:00Z"),
        occurrence("2026-07-10T00:00:00Z"),
      ]),
    ).toBe("falling");
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
