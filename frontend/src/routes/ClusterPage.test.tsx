/**
 * The cluster workspace: depth without a resource browser.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ClusterPage } from "./ClusterPage";
import * as api from "../services/api";
import type { InvestigationHistoryItem } from "../types/investigation";

const NOW = Date.now();
const at = (msAgo: number) => new Date(NOW - msAgo).toISOString();

function renderCluster(path = "/clusters/prod-eu-west") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/clusters/:context" element={<ClusterPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
    items: [{ name: "prod-eu-west", cluster: "eks-prod", current: true }],
    current_context: "prod-eu-west",
    error: "",
  });
  vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
    {
      id: "run-1",
      context: "prod-eu-west",
      timestamp: at(60_000),
      root_cause: "Memory limit too low",
      namespace: "payments",
      confidence: 87,
      severity: "Critical",
      status: "success",
    } as InvestigationHistoryItem,
  ]);
  vi.spyOn(api, "getInvestigationReport").mockResolvedValue({
    investigation: {
      overview: { nodes: "5/6 Healthy", pods: "148 Running", cpu_usage: "64%" },
      pods: { problematic_pods: [{ name: "checkout" }] },
      workloads: { inventory: [{ kind: "daemonsets", desired: 4, ready: 3 }] },
      security: {
        findings: [{ label: "Containers as root", status: "warning", detail: "2 containers" }],
      },
      metrics: {
        top_pods: [
          { namespace: "payments", name: "checkout-7d9f", cpu: "240m", memory: "412Mi" },
        ],
      },
      evidence_coverage: { total: 11, usable: 9, completeness: 90 },
      evidence: [
        {
          id: "k8s.nodes:cluster/a",
          kind: "k8s.nodes",
          status: "ok",
          command: "kubectl get nodes -o json",
          detail: "",
        },
        { id: "k8s.pods:cluster/a", kind: "k8s.pods", status: "ok", command: null, detail: "" },
        {
          id: "k8s.workloads:cluster/a",
          kind: "k8s.workloads",
          status: "ok",
          command: null,
          detail: "",
        },
      ],
    },
    diagnosis: {},
  } as never);
});

describe("overview", () => {
  it("shows what the last investigation established", async () => {
    renderCluster();
    expect(await screen.findByText("5/6 Healthy")).toBeInTheDocument();
    expect(screen.getByText("148 Running")).toBeInTheDocument();
  });

  it("builds the census from inventory nothing rendered before", async () => {
    renderCluster();
    expect(await screen.findByText("Daemonsets")).toBeInTheDocument();
    expect(screen.getByText(/1 · 1 not ready|1 not ready/)).toBeInTheDocument();
  });

  it("makes a figure traceable to the command that produced it", async () => {
    // The enforceable line against becoming a resource browser.
    const user = userEvent.setup();
    renderCluster();

    const chip = await screen.findByRole("button", { name: /kubectl get nodes/i });
    await user.click(chip);

    expect(await screen.findByText("k8s.nodes:cluster/a")).toBeInTheDocument();
  });

  it("lists only security checks that did not pass", async () => {
    renderCluster();
    expect(await screen.findByText("Containers as root")).toBeInTheDocument();
  });

  it("shows top consumers from kubectl top", async () => {
    renderCluster();
    expect(await screen.findByText("payments/checkout-7d9f")).toBeInTheDocument();
  });
});

describe("staleness", () => {
  it("says when the reading is from", async () => {
    renderCluster();
    expect(await screen.findByText(/as of the last investigation/i)).toBeInTheDocument();
  });

  it("warns when it is old enough to mislead", async () => {
    // A cluster page that looks live but is six days old is the most dangerous
    // screen this product could ship.
    vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([
      {
        id: "run-1",
        context: "prod-eu-west",
        timestamp: at(6 * 86_400_000),
        root_cause: "x",
        namespace: "p",
        confidence: 50,
        severity: "Healthy",
        status: "success",
      } as InvestigationHistoryItem,
    ]);

    renderCluster();
    expect(await screen.findByText(/not now/i)).toBeInTheDocument();
  });
});

describe("tabs", () => {
  it("offers five, not the twelve the brief asked for", async () => {
    renderCluster();
    const nav = await screen.findByRole("navigation", { name: /cluster sections/i });
    expect(within(nav).getAllByRole("button")).toHaveLength(5);
  });

  it("keeps the open tab in the URL so a view can be shared", async () => {
    renderCluster("/clusters/prod-eu-west?tab=evidence");
    expect(await screen.findByText("k8s.nodes")).toBeInTheDocument();
  });

  it("lists every run against this cluster", async () => {
    renderCluster("/clusters/prod-eu-west?tab=investigations");
    const link = await screen.findByRole("link", { name: /memory limit too low/i });
    expect(link).toHaveAttribute("href", "/investigations/run-1");
  });
});

describe("a cluster with nothing on record", () => {
  it("says so, and that nothing is ever applied", async () => {
    vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([]);
    renderCluster();

    expect(await screen.findByText(/nothing has been investigated/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is ever applied/i)).toBeInTheDocument();
  });
});
