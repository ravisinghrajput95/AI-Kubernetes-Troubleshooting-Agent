import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";

import { AgentDot } from "../components/fleet/AgentDot";
import { apiBaseUrl } from "../services/http";
import { getAgents, getHealth, getSession } from "../services/api";
import { getToken, signOut } from "../services/auth";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

/**
 * Connection and session.
 *
 * Small on purpose, but it carries something the console did not have at all
 * before Phase 1: a way to sign out. Phase 0 added a credential and no way to
 * discard one.
 */
export function SettingsPage() {
  useDocumentTitle("Settings");
  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
  });

  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: getAgents,
    retry: false,
    refetchInterval: 10_000,
  });

  // `/health` knows the auth *mode*; only `/me` knows who you are. The
  // console used to show "Bearer token" here, which is the mechanism and not
  // the person — the only part anyone wants on a shared deployment.
  const session = useQuery({
    queryKey: ["session"],
    queryFn: getSession,
    retry: false,
  });

  const authenticated = Boolean(getToken());
  const connected = agents.data?.items ?? [];
  const online = connected.filter((agent) => agent.online).length;

  const identity = session.data?.email || session.data?.subject || "";

  const rows: Array<[string, string]> = [
    ["Backend", apiBaseUrl],
    ["Status", isError ? "Unreachable" : (health?.status ?? "Checking…")],
    ["Service", health?.service ?? "—"],
    ["Authentication", health?.auth_mode ?? "—"],
    [
      "Signed in as",
      identity || (authenticated ? "Checking…" : "No credential"),
    ],
  ];

  // Only shown where it means something. On a single-tenant deployment every
  // caller is in `default`, and a row saying so is noise pretending to be
  // information.
  if (session.data?.multi_tenant) {
    rows.push(["Tenant", session.data.tenant]);
  }
  if (session.data?.groups?.length) {
    rows.push(["Groups", session.data.groups.join(", ")]);
  }
  // What this caller may actually do, and where it came from. Shown
  // unconditionally once known: a deployment where everyone is an admin by
  // default should say so rather than leave it to be discovered.
  if (session.data) {
    const source =
      session.data.role_source === "suspended"
        ? "suspended"
        : session.data.role_source === "default"
          ? "deployment default"
          : session.data.role_source === "open-deployment"
            ? "authentication is disabled"
            : session.data.role_source;
    rows.push(["Role", session.data.role ? `${session.data.role} (${source})` : `none (${source})`]);
  }

  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-h1">Settings</h1>

      <section className="mt-6 rounded-lg border border-line bg-surface">
        <h2 className="border-b border-line-muted px-4 py-3 text-h2">Connection</h2>
        <dl className="divide-y divide-line-muted">
          {rows.map(([label, value]) => (
            <div key={label} className="flex items-baseline gap-4 px-4 py-3">
              <dt className="w-40 shrink-0 text-sm text-ink-3">{label}</dt>
              <dd className="min-w-0 break-all font-mono text-sm text-ink-2">{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="mt-4 rounded-lg border border-line bg-surface">
        <div className="flex items-baseline justify-between gap-4 border-b border-line-muted px-4 py-3">
          <h2 className="text-h2">Cluster agents</h2>
          <span className="text-sm text-ink-3">
            {connected.length === 0
              ? "None connected"
              : `${online} of ${connected.length} online`}
          </span>
        </div>

        {!agents.data?.gateway_enabled ? (
          <p className="max-w-measure px-4 py-3 text-sm leading-6 text-ink-2">
            No agent gateway is running. Set{" "}
            <code className="font-mono text-sm">AGENT_GATEWAY_PORT</code> to let
            clusters dial in; without it every cluster is read with the
            platform&apos;s own kubeconfig.
          </p>
        ) : connected.length === 0 ? (
          <p className="max-w-measure px-4 py-3 text-sm leading-6 text-ink-2">
            The gateway is listening and no agent has connected yet.{" "}
            <Link to="/connect" className="text-info underline-offset-4 hover:underline">
              Connect a cluster
            </Link>
            .
          </p>
        ) : (
          <ul className="divide-y divide-line-muted">
            {connected.map((agent) => (
              <li
                key={agent.cluster_id}
                className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-4 py-3"
              >
                <span className="font-mono text-sm text-ink">{agent.cluster_id}</span>
                <span className="flex items-center gap-3">
                  <span className="text-sm text-ink-3">
                    agent {agent.agent_version || "unknown"}
                  </span>
                  <AgentDot agent={agent} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {health?.insecure ? (
        <section className="mt-4 rounded-lg border border-warning/40 bg-warning/5 p-4">
          <h2 className="text-h2 text-warning">Authentication is turned off</h2>
          <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
            This backend is running with{" "}
            <code className="font-mono text-sm">AUTH_MODE=disabled</code> and{" "}
            <code className="font-mono text-sm">ALLOW_INSECURE_NO_AUTH=true</code>,
            so anyone who can reach the port has whatever access its kubeconfig
            has. That is a deliberate local-development setting, not a fault —
            but it is not safe on a shared network, and enrolling new clusters
            is refused while it is on.
          </p>
          <p className="mt-3 max-w-measure text-sm leading-6 text-ink-2">
            To turn it on, restart the backend with{" "}
            <code className="font-mono text-sm">AUTH_MODE=token</code> and an{" "}
            <code className="font-mono text-sm">API_TOKENS</code> entry, then
            sign in with that token.
          </p>
        </section>
      ) : null}

      <section className="mt-4 rounded-lg border border-line bg-surface p-4">
        <h2 className="text-h2">Session</h2>
        <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
          Credentials are held for this browser tab only and are cleared when it
          closes.
          {session.data?.end_session_url ? (
            <>
              {" "}
              Signing out also ends your session with the identity provider —
              without that step the next sign-in is a silent redirect straight
              back in.
            </>
          ) : null}
        </p>
        <button
          type="button"
          onClick={() => signOut(session.data?.end_session_url ?? "")}
          className="mt-4 rounded-md border border-line bg-raised px-3 py-2 text-sm transition-colors duration-fast hover:border-critical hover:text-critical focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
        >
          Sign out
        </button>
      </section>
    </div>
  );
}
