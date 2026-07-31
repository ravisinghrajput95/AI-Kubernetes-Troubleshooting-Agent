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
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "../components/ErrorBoundary";
import { InvestigationPage } from "../App";
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

describe("an investigation at its own address", () => {
  it("renders the run that id refers to", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "succeeded",
      investigation: { context: "prod-eu-west" },
      diagnosis: { root_cause: "Memory limit too low" },
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/investigations/job-1"]}>
          <Routes>
            <Route path="/investigations/:id" element={<InvestigationPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The cluster it ran against, and the id itself, both addressable.
    expect(await screen.findByRole("heading", { name: "prod-eu-west" })).toBeInTheDocument();
    expect(screen.getByText("job-1")).toBeInTheDocument();
    expect(api.getInvestigationJob).toHaveBeenCalledWith("job-1");
  });
});

describe("a payload the console did not expect", () => {
  it("degrades to a message instead of blanking the page", () => {
    // The backend types investigation and diagnosis as dict[str, Any], so the
    // TypeScript interfaces are the only contract. A report written by an
    // older version can be missing a field the UI treats as required.
    function Exploding(): React.ReactNode {
      throw new Error("kubectl_commands is not iterable");
    }

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ErrorBoundary>
            <Exploding />
          </ErrorBoundary>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText(/could not be displayed/i)).toBeInTheDocument();
    expect(screen.getByText(/kubectl_commands is not iterable/i)).toBeInTheDocument();
  });
});

describe("the headline must not contradict the body", () => {
  function renderInvestigation() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/investigations/job-1"]}>
          <Routes>
            <Route path="/investigations/:id" element={<InvestigationPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("does not report a failed run as healthy", async () => {
    // Severity is derived from findings, so a run that collected nothing has
    // no findings and the backend reports "Healthy". Showing that beside a
    // failure notice is the misrepresentation the grounding checks exist to
    // prevent, moved into the UI.
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "failed",
      error: "Kubernetes investigation failed.",
      investigation: { context: "staging-1", severity: { severity: "Healthy" } },
      diagnosis: {},
    } as never);
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({ report: undefined } as never);

    renderInvestigation();

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.queryByText("Healthy")).not.toBeInTheDocument();
  });

  it("reports severity when the run actually produced findings", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "succeeded",
      investigation: { context: "prod", severity: { severity: "Critical" } },
      diagnosis: {},
    } as never);
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({ report: undefined } as never);

    renderInvestigation();
    expect(await screen.findByText("Critical")).toBeInTheDocument();
  });

  it("does not repeat the timeline once the run is over", async () => {
    // The live stream is for watching; the composed Investigation Timeline
    // section is the record. Both at once said the same thing twice.
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "succeeded",
      timeline: [{ type: "progress", message: "Retrieved Pods", at: "", time: "17:43" }],
      investigation: { context: "prod" },
      diagnosis: {},
    } as never);
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({ report: undefined } as never);

    renderInvestigation();
    await screen.findByRole("heading", { name: "prod" });
    expect(screen.queryByText(/investigation progress/i)).not.toBeInTheDocument();
  });
});
