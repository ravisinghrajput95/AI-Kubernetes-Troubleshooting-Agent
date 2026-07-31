/**
 * The application frame: navigation, scope, and the command palette.
 *
 * These cover the behaviour that replaces the 320px context column — scope in
 * the URL, keyboard navigation, and a palette that does not fire while someone
 * is typing.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";
import { NavRail } from "./NavRail";
import { ScopeSwitcher } from "./ScopeSwitcher";
import * as api from "../../services/api";

function renderWithin(ui: React.ReactNode, initialEntry = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function shell(initialEntry = "/") {
  return renderWithin(
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<p>investigate page</p>} />
        <Route path="/reports" element={<p>reports page</p>} />
        <Route path="/settings" element={<p>settings page</p>} />
      </Route>
    </Routes>,
    initialEntry,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();

  vi.spyOn(api, "getHealth").mockResolvedValue({
    status: "healthy",
    service: "ai-kubernetes-agent",
    auth_mode: "disabled",
    insecure: true,
  });
  vi.spyOn(api, "getKubernetesContexts").mockResolvedValue({
    items: [
      { name: "prod-eu-west", cluster: "eks-prod", current: true },
      { name: "staging", cluster: "eks-staging", current: false },
    ],
    current_context: "prod-eu-west",
    error: "",
  });
});

describe("navigation rail", () => {
  it("shows only destinations that have data behind them", () => {
    renderWithin(<NavRail onOpenPalette={vi.fn()} />);

    expect(screen.getByRole("link", { name: /investigations/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /reports/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /settings/i })).toBeInTheDocument();
    // Absent until they are real — see docs/CONSOLE_REDESIGN.md §0.
    expect(screen.queryByRole("link", { name: /knowledge graph/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /incidents/i })).not.toBeInTheDocument();
  });

  it("remembers that it was collapsed", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithin(<NavRail onOpenPalette={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /collapse navigation/i }));
    unmount();

    renderWithin(<NavRail onOpenPalette={vi.fn()} />);
    expect(screen.getByRole("button", { name: /expand navigation/i })).toBeInTheDocument();
  });
});

describe("scope switcher", () => {
  it("adopts the kubeconfig's current context", async () => {
    renderWithin(<ScopeSwitcher />);
    expect(await screen.findByText("prod-eu-west")).toBeInTheDocument();
  });

  it("scopes to a chosen cluster", async () => {
    const user = userEvent.setup();
    renderWithin(<ScopeSwitcher />);

    await user.click(await screen.findByRole("button", { name: /prod-eu-west/i }));
    const list = await screen.findByRole("listbox", { name: /clusters/i });
    await user.click(within(list).getByRole("option", { name: /staging/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /staging/i })).toBeInTheDocument(),
    );
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    renderWithin(<ScopeSwitcher />);

    await user.click(await screen.findByRole("button", { name: /prod-eu-west/i }));
    expect(screen.getByRole("listbox", { name: /clusters/i })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox", { name: /clusters/i })).not.toBeInTheDocument();
  });
});

describe("command palette", () => {
  it("opens with the keyboard", async () => {
    const user = userEvent.setup();
    shell();

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByRole("dialog", { name: /command palette/i })).toBeInTheDocument();
  });

  it("filters and runs a command", async () => {
    const user = userEvent.setup();
    shell();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.type(await screen.findByLabelText(/search commands/i), "settings");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("settings page")).toBeInTheDocument();
  });

  it("says so when nothing matches", async () => {
    const user = userEvent.setup();
    shell();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.type(await screen.findByLabelText(/search commands/i), "zzzz");

    expect(screen.getByText(/nothing matches/i)).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    shell();

    await user.keyboard("{Meta>}k{/Meta}");
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("offers the clusters it can scope to", async () => {
    const user = userEvent.setup();
    shell();

    await user.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByText(/scope to cluster/i)).toBeInTheDocument();
  });
});

describe("keyboard navigation", () => {
  it("jumps with the g chord", async () => {
    const user = userEvent.setup();
    shell();

    await user.keyboard("gr");
    expect(await screen.findByText("reports page")).toBeInTheDocument();
  });

  it("does not hijack keys while typing", async () => {
    // The chord must not fire inside the scope switcher's search or an
    // investigation's namespace field.
    const user = userEvent.setup();
    shell();

    await user.keyboard("{Meta>}k{/Meta}");
    const search = await screen.findByLabelText(/search commands/i);
    await user.type(search, "gr");

    expect(search).toHaveValue("gr");
    expect(screen.queryByText("reports page")).not.toBeInTheDocument();
  });
});

describe("platform status", () => {
  it("reports a reachable backend in the header, not as a tile", async () => {
    shell();
    expect(await screen.findByText(/connected/i)).toBeInTheDocument();
  });

  it("reports an unreachable one", async () => {
    vi.spyOn(api, "getHealth").mockRejectedValue(new Error("down"));
    shell();
    expect(await screen.findByText(/offline/i)).toBeInTheDocument();
  });
});
