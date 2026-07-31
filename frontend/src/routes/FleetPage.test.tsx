import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FleetPage } from "./FleetPage";
import * as api from "../services/api";
import type { InvestigationHistoryItem } from "../types/investigation";

const NOW = Date.now();
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

function renderFleet() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FleetPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getInvestigationJobs").mockResolvedValue([]);
  vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
    items: [
      { name: "prod-eu-west", cluster: "eks-prod", current: true },
      { name: "staging-1", cluster: "eks-staging", current: false },
      { name: "dev-local", cluster: "kind", current: false },
    ],
    current_context: "prod-eu-west",
    error: "",
  });
  vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
    entry({ id: "1", context: "prod-eu-west", severity: "Critical" }),
    entry({
      id: "2",
      context: "staging-1",
      severity: "Healthy",
      root_cause: "No problems found",
      timestamp: at(6 * 86_400_000),
    }),
  ]);
  vi.spyOn(api, "getInvestigationReport").mockResolvedValue({
    diagnosis: { signals: [] },
  } as never);
});

describe("fleet", () => {
  it("leads with the finding, not the inventory", async () => {
    renderFleet();
    expect(await screen.findByText("Memory limit too low")).toBeInTheDocument();
  });

  it("orders worst first", async () => {
    renderFleet();
    await screen.findByText("prod-eu-west");

    const names = screen
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "")
      .filter((text) => text.includes("prod-eu-west") || text.includes("staging-1") || text.includes("dev-local"));

    expect(names[0]).toContain("prod-eu-west");
  });

  it("treats a six-day-old healthy verdict as stale, not healthy", async () => {
    // Rendering unknown as green is lying by omission.
    renderFleet();
    expect(await screen.findByText("Stale")).toBeInTheDocument();
    expect(screen.getByText(/this is what was true then/i)).toBeInTheDocument();
  });

  it("shows a cluster that has never been investigated", async () => {
    renderFleet();
    expect(await screen.findByText("Never investigated")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /investigate/i })).toBeInTheDocument();
  });

  it("says the state is not live", async () => {
    // There is no background watcher. A live-looking number that nothing
    // refreshes would be worse than showing nothing.
    renderFleet();
    expect(await screen.findByText(/not a live reading/i)).toBeInTheDocument();
  });

  it("links an investigated cluster to its investigation", async () => {
    renderFleet();
    const link = await screen.findByRole("link", { name: /prod-eu-west/i });
    expect(link).toHaveAttribute("href", "/investigations/1");
  });

  it("counts each state in the rollup", async () => {
    renderFleet();
    expect(await screen.findByText("1 critical")).toBeInTheDocument();
    expect(screen.getByText("1 stale")).toBeInTheDocument();
    expect(screen.getByText("1 not investigated")).toBeInTheDocument();
  });
});

describe("fleet-wide correlation", () => {
  it("surfaces a failure seen on more than one cluster", async () => {
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({
      incident_id: "INC",
      diagnosis: {
        signals: [
          {
            type: "image.no_pull_secret",
            summary: "Image pull is failing with no pull secret",
            severity: "critical",
          },
        ],
      },
    } as never);

    renderFleet();
    expect(await screen.findByText(/across the fleet/i)).toBeInTheDocument();
    const region = screen.getByText(/across the fleet/i).parentElement as HTMLElement;
    expect(within(region).getByText("image.no_pull_secret")).toBeInTheDocument();
    expect(within(region).getByText(/2 clusters/i)).toBeInTheDocument();
  });

  it("shows nothing when no failure is shared", async () => {
    renderFleet();
    expect(screen.queryByText(/across the fleet/i)).not.toBeInTheDocument();
  });
});

describe("an empty fleet", () => {
  it("explains what will appear, and that nothing is applied", async () => {
    vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
      items: [],
      current_context: "",
      error: "",
    });
    vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([]);

    renderFleet();
    expect(await screen.findByText(/no clusters yet/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is ever applied/i)).toBeInTheDocument();
  });
});
