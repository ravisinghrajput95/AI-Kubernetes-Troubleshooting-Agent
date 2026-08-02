/**
 * The console offers only what the caller may actually do.
 *
 * Not a security control — the backend is, and it answers 403 regardless. This
 * is about the failure mode where a viewer clicks "Start investigation", gets a
 * 403, and reasonably concludes the platform is broken. Gating is on the
 * **permission list** from `/me` rather than on the role name, so a role
 * gaining a permission needs no change here and there is no second copy of the
 * role table to drift.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectClusterPage } from "./ConnectClusterPage";
import { InvestigatePage } from "./InvestigatePage";
import * as api from "../services/api";

const VIEWER_PERMISSIONS = ["investigation.read", "cluster.read"];
const OPERATOR_PERMISSIONS = [...VIEWER_PERMISSIONS, "investigation.run"];
const ADMIN_PERMISSIONS = [
  ...OPERATOR_PERMISSIONS,
  "cluster.enrol",
  "cluster.revoke",
  "member.read",
  "member.manage",
];

function session(role: string, permissions: string[], roleSource = "assigned"): api.SessionInfo {
  return {
    subject: `${role}@example.com`,
    email: `${role}@example.com`,
    groups: [],
    tenant: "default",
    auth_method: "token",
    anonymous: false,
    end_session_url: "",
    multi_tenant: false,
    role,
    role_source: roleSource,
    permissions,
  };
}

function renderPage(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      {/* Scope lives in the URL, and the Investigate button is also disabled
          when no cluster is selected — so the permission has to be the only
          thing under test here. */}
      <MemoryRouter initialEntries={["/investigations?cluster=prod"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getInvestigationHistory").mockResolvedValue([]);
  vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
    items: [
      {
        name: "prod",
        cluster: "prod",
        current: true,
        connection: "kubeconfig",
        agent: null,
      },
    ],
    current_context: "prod",
    error: "",
  });
  vi.spyOn(api, "getAgents").mockResolvedValue({
    items: [],
    gateway_enabled: true,
    trust_domain: "k8s-agent.local",
    scope: "worker",
  });
});

describe("starting an investigation", () => {
  it("is offered to an operator", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("operator", OPERATOR_PERMISSIONS));
    renderPage(<InvestigatePage />);

    const button = await screen.findByRole("button", { name: /start investigation/i });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("is refused to a viewer, with the reason visible", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("viewer", VIEWER_PERMISSIONS));
    renderPage(<InvestigatePage />);

    const button = await screen.findByRole("button", { name: /start investigation/i });
    await waitFor(() => expect(button).toBeDisabled());
    // A disabled button with no stated reason reads as a broken console.
    expect(await screen.findByText(/requires the operator role/i)).toBeInTheDocument();
  });

  it("says so plainly when the account is suspended", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("", [], "suspended"));
    renderPage(<InvestigatePage />);

    expect(await screen.findByText(/suspended in this tenant/i)).toBeInTheDocument();
  });
});

describe("connecting a cluster", () => {
  it("is offered to an admin", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("admin", ADMIN_PERMISSIONS));
    renderPage(<ConnectClusterPage />);

    expect(await screen.findByLabelText(/cluster id/i)).toBeInTheDocument();
  });

  it("is refused to an operator, and the form is not shown at all", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("operator", OPERATOR_PERMISSIONS));
    renderPage(<ConnectClusterPage />);

    expect(await screen.findByText(/you cannot connect clusters/i)).toBeInTheDocument();
    // Not merely disabled: a token field nobody can submit is an invitation to
    // wonder what is wrong with it.
    expect(screen.queryByLabelText(/cluster id/i)).not.toBeInTheDocument();
  });

  it("names the role the caller holds", async () => {
    vi.spyOn(api, "getSession").mockResolvedValue(session("viewer", VIEWER_PERMISSIONS));
    renderPage(<ConnectClusterPage />);

    expect(await screen.findByText(/you hold the viewer role/i)).toBeInTheDocument();
  });
});

describe("while the session is still loading", () => {
  it("does not flicker actions into a disabled state", async () => {
    // Permissive until `/me` answers: the backend is the real gate, and
    // briefly offering an action that would 403 is better than every page load
    // showing a disabled console for a moment.
    vi.spyOn(api, "getSession").mockReturnValue(new Promise(() => {}));
    renderPage(<InvestigatePage />);

    const button = await screen.findByRole("button", { name: /start investigation/i });
    await waitFor(() => expect(button).toBeEnabled());
  });
});
