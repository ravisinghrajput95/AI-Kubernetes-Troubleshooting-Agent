/**
 * Route smoke tests.
 *
 * `ReportsPage` imports `HistoryTable` from `App.tsx`, which imports
 * `ReportsPage` back. That cycle resolves because the import is only read
 * during render, but it is exactly the kind of thing that type-checks, builds,
 * and then throws a blank white page at runtime — so it gets a test until
 * Phase 3 moves the component out and removes the cycle.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "../components/ErrorBoundary";
import { InvestigationPage } from "./InvestigationPage";
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

    expect(await screen.findByText(/authentication is turned off/i)).toBeInTheDocument();
    // The warning has to say what to do about it. A security notice with no
    // next step trains operators to scroll past security notices.
    expect(screen.getByText(/AUTH_MODE=token/)).toBeInTheDocument();
  });

  it("shows whether any cluster agent is answering", async () => {
    vi.spyOn(api, "getAgents").mockResolvedValue({
      items: [
        {
          cluster_id: "prod-eu-1",
          online: true,
          connected_at: new Date().toISOString(),
          last_seen: new Date().toISOString(),
          seconds_since_seen: 2,
          degradation: "",
          agent_version: "0.2.0-m4b",
          kubernetes_version: "v1.31.0",
          supported_kinds: ["k8s.pods"],
          identity_source: "certificate",
          certificate_serial: "aa11",
          certificate_expires_at: "",
        },
      ],
      gateway_enabled: true,
      trust_domain: "test.local",
      scope: "fleet",
    });
    renderPage(<SettingsPage />);

    expect(await screen.findByText("prod-eu-1")).toBeInTheDocument();
    expect(screen.getByText(/agent online/i)).toBeInTheDocument();
    expect(screen.getByText(/1 of 1 online/i)).toBeInTheDocument();
  });

  it("says so when an agent has stopped answering", async () => {
    // A silent agent must not read as a connected one.
    vi.spyOn(api, "getAgents").mockResolvedValue({
      items: [
        {
          cluster_id: "prod-eu-1",
          online: false,
          connected_at: new Date().toISOString(),
          last_seen: new Date().toISOString(),
          seconds_since_seen: 120,
          degradation: "",
          agent_version: "0.2.0-m4b",
          kubernetes_version: "v1.31.0",
          supported_kinds: [],
          identity_source: "certificate",
          certificate_serial: "aa11",
          certificate_expires_at: "",
        },
      ],
      gateway_enabled: true,
      trust_domain: "test.local",
      scope: "fleet",
    });
    renderPage(<SettingsPage />);

    expect(await screen.findByText(/agent silent for 120s/i)).toBeInTheDocument();
    expect(screen.getByText(/0 of 1 online/i)).toBeInTheDocument();
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

  it("scopes the console to the cluster it investigated", async () => {
    // Started from a form scoped to one cluster, the page arrived with the
    // scope of another — the kubeconfig default — and the header said so.
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "succeeded",
      investigation: { context: "sweep-agent" },
      diagnosis: { root_cause: "Memory limit too low" },
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);

    function Scope() {
      return <output>{new URLSearchParams(useLocation().search).get("cluster")}</output>;
    }
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/investigations/job-1?cluster=kind-k8s-agent-dev"]}>
          <Routes>
            <Route
              path="/investigations/:id"
              element={
                <>
                  <InvestigationPage />
                  <Scope />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByRole("heading", { name: "sweep-agent" });
    expect(await screen.findByText("sweep-agent", { selector: "output" })).toBeInTheDocument();
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

  it("names the tab after the cluster and the outcome", async () => {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-1",
      status: "failed",
      investigation: { context: "staging-1" },
      diagnosis: {},
    } as never);
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({ report: undefined } as never);

    renderInvestigation();
    await screen.findByRole("heading", { name: "staging-1" });
    // `waitFor`, not a synchronous read: the title is set in an effect that
    // can flush a tick after the heading appears, which made this fail about
    // one run in six.
    await waitFor(() =>
      expect(document.title).toBe("staging-1 · failed · Kubernetes Operations"),
    );
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

/**
 * The remediation an operator is shown must be the one the diagnosis is about.
 *
 * The page mounted a panel that composed its own patch and "Apply Fix"
 * commands for `firstAffectedWorkload` — the first problematic workload in the
 * namespace. Reproduced against `docs/qa/audit-faults.yaml`: the diagnosis named
 * Service payments/checkout-svc, and on the same page that panel offered a
 * Deployment patch for payments/archiver (the PVC-blocked pod, unrelated) and
 * copied `kubectl edit deployment archiver -n payments` to the clipboard.
 *
 * The fixture below is that investigation's shape: a plan targeting the
 * Service, and a different workload first in the problematic list.
 */
describe("the remediation shown is the one the diagnosis is about", () => {
  const plan = {
    id: "align-service-selector",
    hypothesis_id: "network.service_without_endpoints",
    title: "Align Service payments/checkout-svc with its pods",
    summary: "The selector matches no ready pods.",
    target: { kind: "Service", name: "checkout-svc", namespace: "payments" },
    risk: {
      level: "Medium",
      change_kind: "service",
      restart_required: false,
      estimated_downtime: "None if the selector is corrected",
      blast_radius: "All clients of service payments/checkout-svc",
      reversible: true,
      notes: [],
    },
    requires_approval: true,
    preconditions: [],
    remediation: [{ description: "Correct the selector", command: "", manual: true }],
    verification: [],
    rollback: [],
    required_permissions: [],
    patches: [],
    signal_ids: [],
    evidence_ids: [],
    caveats: [],
  };

  async function renderInvestigation(remediation: unknown) {
    vi.spyOn(api, "getInvestigationJob").mockResolvedValue({
      id: "job-remediation",
      status: "succeeded",
      investigation: {
        context: "kind-audit",
        pods: {
          // First in the list, and not what the diagnosis is about.
          problematic_pods: [
            { name: "archiver-6795b9bc5d-mkrs8", namespace: "payments", status: "Pending" },
            { name: "checkout-5b5fd56dbf-rb9ps", namespace: "payments", status: "CrashLoopBackOff" },
          ],
        },
      },
      diagnosis: {
        root_cause: "Service has no ready endpoints (service/payments/checkout-svc).",
        remediation,
      },
    } as unknown as Awaited<ReturnType<typeof api.getInvestigationJob>>);
    vi.spyOn(api, "getInvestigationReport").mockResolvedValue({ report: undefined } as never);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/investigations/job-remediation"]}>
          <Routes>
            <Route path="/investigations/:id" element={<InvestigationPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("names the resource the plan targets", async () => {
    await renderInvestigation(plan);
    expect(await screen.findByText("Service/payments/checkout-svc")).toBeInTheDocument();
  });

  it("never offers a change to a workload the diagnosis did not name", async () => {
    const { container } = await renderInvestigation(plan);
    // Waits on something both panels leave alone, then for the terminal
    // render. Waiting on the plan's target made this test fail on its first
    // line against the defect, so the assertions naming the wrong workload
    // never ran — a guard that fails for the wrong reason is not guarding.
    await screen.findByRole("heading", { name: "kind-audit" });
    await screen.findAllByText(/remediation|fix/i);

    // Asserted on the page's text, not on a component: whatever is mounted, an
    // operator must not be handed the unrelated workload as something to fix.
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/deployment archiver/i);
    expect(text).not.toMatch(/name: archiver/);
    expect(screen.queryByRole("button", { name: /apply fix/i })).toBeNull();
  });

  it("says there is no plan rather than composing one", async () => {
    await renderInvestigation(null);
    expect(
      await screen.findByText("No remediation plan was produced for this investigation."),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/archiver/i);
  });
});
