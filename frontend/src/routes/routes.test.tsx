/**
 * Route smoke tests.
 *
 * `ReportsPage` imports `HistoryTable` from `App.tsx`, which imports
 * `ReportsPage` back. That cycle resolves because the import is only read
 * during render, but it is exactly the kind of thing that type-checks, builds,
 * and then throws a blank white page at runtime — so it gets a test until
 * Phase 3 moves the component out and removes the cycle.
 */

import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReportsPage } from "./ReportsPage";
import { SettingsPage } from "./SettingsPage";
import * as api from "../services/api";

function renderPage(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([]);
  vi.spyOn(api, "getHealth").mockResolvedValue({
    status: "healthy",
    service: "ai-kubernetes-agent",
    auth_mode: "token",
    insecure: false,
  });
});

describe("reports", () => {
  it("renders through the import cycle", async () => {
    renderPage(<ReportsPage />);

    expect(screen.getByRole("heading", { name: "Reports", level: 1 })).toBeInTheDocument();
    // Proves `HistoryTable` resolved rather than arriving as undefined.
    expect(await screen.findByText(/recent investigations/i)).toBeInTheDocument();
  });
});

describe("settings", () => {
  it("shows what the console is connected to", async () => {
    renderPage(<SettingsPage />);

    expect(await screen.findByText("ai-kubernetes-agent")).toBeInTheDocument();
    expect(screen.getByText("token")).toBeInTheDocument();
  });

  it("offers a way out, which the console did not have before", () => {
    renderPage(<SettingsPage />);
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
  });

  it("repeats the unauthenticated warning where it can be found again", async () => {
    vi.spyOn(api, "getHealth").mockResolvedValue({
      status: "healthy",
      service: "ai-kubernetes-agent",
      auth_mode: "disabled",
      insecure: true,
    });
    renderPage(<SettingsPage />);

    expect(await screen.findByText(/this backend is unauthenticated/i)).toBeInTheDocument();
  });
});
