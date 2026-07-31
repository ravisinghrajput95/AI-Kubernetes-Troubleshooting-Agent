import { describe, expect, it } from "vitest";

import { correlateSignals, fleetState, relativeAge, rollup, STALE_AFTER_MS } from "./fleet";
import type { InvestigationHistoryItem, KubernetesContext } from "../types/investigation";

const NOW = Date.parse("2026-07-31T12:00:00Z");

const at = (msAgo: number) => new Date(NOW - msAgo).toISOString();

const entry = (
  overrides: Partial<InvestigationHistoryItem> & { id: string },
): InvestigationHistoryItem =>
  ({
    timestamp: at(60_000),
    root_cause: "Memory limit too low",
    namespace: "payments",
    confidence: 87,
    severity: "Critical",
    status: "success",
    ...overrides,
  }) as InvestigationHistoryItem;

const context = (name: string): KubernetesContext =>
  ({ name, cluster: `eks-${name}`, current: false }) as KubernetesContext;

describe("worst first", () => {
  it("orders by state, never alphabetically", () => {
    // The default sort of an operations surface is a statement about what the
    // product thinks matters.
    const rows = fleetState(
      [context("a-healthy"), context("z-critical"), context("m-degraded")],
      [
        entry({ id: "1", context: "a-healthy", severity: "Healthy" }),
        entry({ id: "2", context: "z-critical", severity: "Critical" }),
        entry({ id: "3", context: "m-degraded", severity: "Warning" }),
      ],
      new Map(),
      NOW,
    );

    expect(rows.map((row) => row.name)).toEqual(["z-critical", "m-degraded", "a-healthy"]);
  });

  it("puts the stalest first within a state", () => {
    const rows = fleetState(
      [context("recent"), context("old")],
      [
        entry({ id: "1", context: "recent", severity: "Critical", timestamp: at(60_000) }),
        entry({ id: "2", context: "old", severity: "Critical", timestamp: at(600_000) }),
      ],
      new Map(),
      NOW,
    );

    expect(rows.map((row) => row.name)).toEqual(["old", "recent"]);
  });
});

describe("staleness", () => {
  it("is a state of its own, not a healthy one", () => {
    // A cluster investigated six days ago is unknown, not healthy. Rendering
    // unknown as green is lying by omission.
    const rows = fleetState(
      [context("prod")],
      [entry({ id: "1", context: "prod", severity: "Healthy", timestamp: at(STALE_AFTER_MS + 1) })],
      new Map(),
      NOW,
    );

    expect(rows[0].state).toBe("stale");
  });

  it("outranks a healthy verdict in the ordering", () => {
    const rows = fleetState(
      [context("fresh"), context("stale")],
      [
        entry({ id: "1", context: "fresh", severity: "Healthy", timestamp: at(60_000) }),
        entry({
          id: "2",
          context: "stale",
          severity: "Healthy",
          timestamp: at(STALE_AFTER_MS * 3),
        }),
      ],
      new Map(),
      NOW,
    );

    expect(rows.map((row) => row.name)).toEqual(["stale", "fresh"]);
  });

  it("leaves a recent investigation alone", () => {
    const rows = fleetState(
      [context("prod")],
      [entry({ id: "1", context: "prod", severity: "Healthy", timestamp: at(60_000) })],
      new Map(),
      NOW,
    );
    expect(rows[0].state).toBe("healthy");
  });
});

describe("attribution", () => {
  it("uses the cluster the history entry records", () => {
    const rows = fleetState([], [entry({ id: "1", context: "prod-eu-west" })], new Map(), NOW);
    expect(rows[0].name).toBe("prod-eu-west");
  });

  it("falls back to the job store for entries written before that existed", () => {
    const rows = fleetState(
      [],
      [entry({ id: "1", context: undefined })],
      new Map([["1", "prod-legacy"]]),
      NOW,
    );
    expect(rows[0].name).toBe("prod-legacy");
  });

  it("drops what cannot be attributed rather than guessing a cluster", () => {
    const rows = fleetState([], [entry({ id: "1", context: undefined })], new Map(), NOW);
    expect(rows).toEqual([]);
  });

  it("shows a configured cluster that has never been investigated", () => {
    const rows = fleetState([context("dev-local")], [], new Map(), NOW);
    expect(rows[0].state).toBe("unknown");
    expect(rows[0].rootCause).toBe("Never investigated");
  });

  it("keeps only the newest investigation per cluster", () => {
    const rows = fleetState(
      [context("prod")],
      [
        entry({ id: "old", context: "prod", root_cause: "Older", timestamp: at(600_000) }),
        entry({ id: "new", context: "prod", root_cause: "Newer", timestamp: at(60_000) }),
      ],
      new Map(),
      NOW,
    );

    expect(rows).toHaveLength(1);
    expect(rows[0].investigationId).toBe("new");
    expect(rows[0].rootCause).toBe("Newer");
  });
});

describe("rollup", () => {
  it("counts each state", () => {
    const rows = fleetState(
      [context("a"), context("b"), context("c")],
      [
        entry({ id: "1", context: "a", severity: "Critical" }),
        entry({ id: "2", context: "b", severity: "Healthy" }),
      ],
      new Map(),
      NOW,
    );

    const counts = rollup(rows);
    expect(counts.critical).toBe(1);
    expect(counts.healthy).toBe(1);
    expect(counts.unknown).toBe(1);
  });
});

describe("fleet-wide correlation", () => {
  const signal = (type: string, severity = "critical") => ({
    type,
    summary: `${type} observed`,
    severity,
  });

  it("groups the same failure across clusters", () => {
    // Eight clusters failing the same image pull is one incident, not eight,
    // and no single investigation can see it.
    const groups = correlateSignals([
      { cluster: "prod-eu", signals: [signal("image.no_pull_secret")] },
      { cluster: "prod-us", signals: [signal("image.no_pull_secret")] },
      { cluster: "staging", signals: [signal("image.no_pull_secret")] },
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].clusters).toEqual(["prod-eu", "prod-us", "staging"]);
  });

  it("ignores a failure seen on only one cluster", () => {
    const groups = correlateSignals([
      { cluster: "prod-eu", signals: [signal("pod.crash_loop")] },
      { cluster: "prod-us", signals: [signal("node.disk_pressure")] },
    ]);
    expect(groups).toEqual([]);
  });

  it("puts the most widespread first", () => {
    const groups = correlateSignals([
      { cluster: "a", signals: [signal("wide"), signal("narrow")] },
      { cluster: "b", signals: [signal("wide"), signal("narrow")] },
      { cluster: "c", signals: [signal("wide")] },
    ]);
    expect(groups.map((group) => group.type)).toEqual(["wide", "narrow"]);
  });

  it("does not count one cluster twice", () => {
    const groups = correlateSignals([
      { cluster: "a", signals: [signal("dup"), signal("dup")] },
      { cluster: "b", signals: [signal("dup")] },
    ]);
    expect(groups[0].clusters).toEqual(["a", "b"]);
  });
});

describe("age", () => {
  it("reads at a glance", () => {
    expect(relativeAge(30_000)).toBe("just now");
    expect(relativeAge(5 * 60_000)).toBe("5m ago");
    expect(relativeAge(4 * 3_600_000)).toBe("4h ago");
    expect(relativeAge(6 * 86_400_000)).toBe("6d ago");
  });

  it("says nothing when there is nothing to say", () => {
    expect(relativeAge(null)).toBe("");
  });
});

describe("a cluster that could not be read", () => {
  it("is its own state, not a healthy or an uninvestigated one", () => {
    // The backend reports "Unknown" when the collectors that produce findings
    // did not run. That is different from never having looked.
    const rows = fleetState(
      [context("prod")],
      [entry({ id: "1", context: "prod", severity: "Unknown" })],
      new Map(),
      NOW,
    );
    expect(rows[0].state).toBe("unreadable");
  });

  it("never sorts below a healthy cluster", () => {
    // The finding count is not just bad, it is not trustworthy — burying it
    // under clusters that were successfully read would hide the gap.
    const rows = fleetState(
      [context("readable"), context("unreadable")],
      [
        entry({ id: "1", context: "readable", severity: "Healthy" }),
        entry({ id: "2", context: "unreadable", severity: "Unknown" }),
      ],
      new Map(),
      NOW,
    );
    expect(rows.map((row) => row.name)).toEqual(["unreadable", "readable"]);
  });

  it("still sorts below a cluster with real findings", () => {
    const rows = fleetState(
      [context("broken"), context("unreadable")],
      [
        entry({ id: "1", context: "broken", severity: "Critical" }),
        entry({ id: "2", context: "unreadable", severity: "Unknown" }),
      ],
      new Map(),
      NOW,
    );
    expect(rows.map((row) => row.name)).toEqual(["broken", "unreadable"]);
  });

  it("is counted separately in the rollup", () => {
    const rows = fleetState(
      [context("a")],
      [entry({ id: "1", context: "a", severity: "Unknown" })],
      new Map(),
      NOW,
    );
    expect(rollup(rows).unreadable).toBe(1);
    expect(rollup(rows).healthy).toBe(0);
  });
});
