import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AskPage } from "./AskPage";
import * as api from "../services/api";
import type { InvestigationHistoryItem } from "../types/investigation";

const item = (id: string, context: string, at: string): InvestigationHistoryItem =>
  ({
    id,
    context,
    timestamp: at,
    root_cause: "Image pull is failing",
    namespace: "payments",
    confidence: 80,
    severity: "Critical",
    status: "success",
  }) as InvestigationHistoryItem;

function renderAsk() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AskPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function withSignals(types: Record<string, string[]>) {
  vi.spyOn(api, "getInvestigationReport").mockImplementation(async (id: string) => ({
    incident_id: id,
    diagnosis: {
      signals: (types[id] ?? []).map((type) => ({
        type,
        summary: `${type} observed`,
        severity: "critical",
      })),
    },
  }) as never);
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
    item("run-a", "prod-eu-west", "2026-07-01T00:00:00Z"),
    item("run-b", "staging-1", "2026-07-02T00:00:00Z"),
  ]);
  withSignals({
    "run-a": ["image.no_pull_secret"],
    "run-b": ["image.no_pull_secret"],
  });
});

describe("the boundary", () => {
  it("says permanently that no cluster was queried", async () => {
    // Without this an operator reasonably assumes live access and receives
    // confident answers about a cluster nobody looked at.
    renderAsk();
    expect(await screen.findByText(/no cluster is\s+queried/i)).toBeInTheDocument();
  });

  it("says how many investigations the answers come from", async () => {
    renderAsk();
    expect(await screen.findByText(/2 stored investigations/i)).toBeInTheDocument();
  });
});

describe("answers", () => {
  it("reports a finding seen across clusters", async () => {
    renderAsk();
    expect(await screen.findByText(/seen on more than one cluster/i)).toBeInTheDocument();
    expect(screen.getAllByText("image.no_pull_secret").length).toBeGreaterThan(0);
  });

  it("does not answer the same finding under two headings", async () => {
    renderAsk();
    await screen.findByText(/seen on more than one cluster/i);
    expect(screen.getAllByText("image.no_pull_secret")).toHaveLength(1);
  });

  it("shows every investigation a claim was counted from", async () => {
    const user = userEvent.setup();
    renderAsk();

    const button = (await screen.findAllByRole("button", { expanded: false }))[0];
    await user.click(button);

    const links = await screen.findAllByRole("link");
    expect(links.some((link) => link.getAttribute("href") === "/investigations/run-a")).toBe(
      true,
    );
    expect(links.some((link) => link.getAttribute("href") === "/investigations/run-b")).toBe(
      true,
    );
  });

  it("does not claim a trend from two occurrences", async () => {
    renderAsk();
    expect(await screen.findAllByText(/not enough history/i)).not.toHaveLength(0);
  });
});

describe("trends", () => {
  it("does not read runs that could not see a finding as the finding being absent", async () => {
    // Read off a live sweep: early runs whose agent could not reach its API
    // server (a failed run, and one saved before failures were marked) and a
    // run scoped to one deployment, then the finding in every run that read
    // its namespace. Every finding on the page read "happening more often".
    const at = (day: number) => `2026-07-${String(day).padStart(2, "0")}T00:00:00Z`;
    vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
      item("r8", "prod", at(10)),
      item("r7", "prod", at(9)),
      item("r6", "prod", at(6)),
      item("r5", "prod", at(5)),
      { ...item("r3", "prod", at(3)), scope: { namespace: "payments", resource_kind: "deployment", resource_name: "checkout" } },
      item("r2", "prod", at(2)),
      { ...item("r1", "prod", at(1)), status: "failed" },
    ]);
    vi.spyOn(api, "getInvestigationReport").mockImplementation(async (id: string) => ({
      incident_id: id,
      investigation: { health: { status: id === "r2" ? "error" : "issues_found" } },
      diagnosis: {
        signals: ["r1", "r2", "r3"].includes(id)
          ? []
          : [{ type: "pod.pending", summary: "archiver Pending", severity: "high", target: { kind: "Pod", name: "archiver", namespace: "payments" } }],
      },
    }) as never);

    renderAsk();

    expect(await screen.findByText("pod.pending")).toBeInTheDocument();
    expect(screen.queryByText(/happening more often/i)).not.toBeInTheDocument();
  });
});

describe("names for one cluster", () => {
  it("does not call a finding cross-cluster when the names read the same nodes", async () => {
    vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
      { ...item("run-a", "kind-dev", "2026-07-01T00:00:00Z"), node_uids: ["uid-1"] },
      { ...item("run-b", "sweep-agent", "2026-07-02T00:00:00Z"), node_uids: ["uid-1"] },
    ]);

    renderAsk();

    expect(await screen.findByText("image.no_pull_secret")).toBeInTheDocument();
    expect(screen.queryByText(/seen on more than one cluster/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/2 clusters/)).not.toBeInTheDocument();
  });
});

describe("nothing on record", () => {
  it("says so rather than answering approximately", async () => {
    const user = userEvent.setup();
    renderAsk();
    await screen.findByText(/seen on more than one cluster/i);

    await user.type(screen.getByLabelText(/filter findings/i), "database deadlock");
    expect(await screen.findByText(/nothing on record matches/i)).toBeInTheDocument();
  });

  it("explains why an unrecurring corpus is empty", async () => {
    withSignals({ "run-a": ["only.once"], "run-b": [] });
    renderAsk();

    expect(await screen.findByText(/nothing has recurred yet/i)).toBeInTheDocument();
    expect(screen.getByText(/is an incident,\s*not a pattern/i)).toBeInTheDocument();
  });
});

describe("coverage counts the fleet it claims to count", () => {
  /**
   * The headline divided every cluster in history by the clusters that exist
   * now. History outlives clusters, so a live console with one connected cluster
   * and runs on record from a decommissioned one read "across 2 of 1 cluster".
   */
  const fleetOf = (...names: string[]) =>
    vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
      items: names.map((name) => ({ name, cluster: name, current: false })),
      current_context: names[0] ?? "",
      error: "",
    } as never);

  const headline = async () =>
    (await screen.findByText(/stored investigations across/i)).textContent ?? "";

  it("never counts more covered clusters than the fleet has", async () => {
    fleetOf("prod-eu-west"); // staging-1 is on record and no longer present
    renderAsk();
    await screen.findByText(/2 stored investigations/i);
    const text = await vi.waitFor(async () => {
      const value = await headline();
      if (!/no longer in the fleet/.test(value)) throw new Error("fleet not loaded yet");
      return value;
    });

    const [, covered, of] = text.match(/across\s+(\d+)\s+of\s+(\d+)/) ?? [];
    expect(Number(covered)).toBeLessThanOrEqual(Number(of));
    expect(text).toMatch(/1 of 1 cluster/);
  });

  it("says what is on record but no longer present, rather than dropping it", async () => {
    fleetOf("prod-eu-west");
    renderAsk();
    expect(await screen.findByText(/1 cluster no longer in the fleet/)).toBeInTheDocument();
  });

  it("does not call every cluster departed when the fleet failed to load", async () => {
    vi.spyOn(api, "getKubernetesContexts").mockRejectedValue(new Error("offline"));
    renderAsk();
    await screen.findByText(/2 stored investigations/i);
    expect(screen.queryByText(/no longer in the fleet/)).toBeNull();
  });
});
