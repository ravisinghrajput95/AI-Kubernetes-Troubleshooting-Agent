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
