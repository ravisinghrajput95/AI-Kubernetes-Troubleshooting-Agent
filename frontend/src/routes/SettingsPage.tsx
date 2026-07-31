import { useQuery } from "@tanstack/react-query";

import { apiBaseUrl } from "../services/http";
import { getHealth } from "../services/api";
import { getToken, signOut } from "../services/auth";

/**
 * Connection and session.
 *
 * Small on purpose, but it carries something the console did not have at all
 * before Phase 1: a way to sign out. Phase 0 added a credential and no way to
 * discard one.
 */
export function SettingsPage() {
  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
  });

  const authenticated = Boolean(getToken());

  const rows: Array<[string, string]> = [
    ["Backend", apiBaseUrl],
    ["Status", isError ? "Unreachable" : (health?.status ?? "Checking…")],
    ["Service", health?.service ?? "—"],
    ["Authentication", health?.auth_mode ?? "—"],
    ["This session", authenticated ? "Bearer token" : "No credential"],
  ];

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

      {health?.insecure ? (
        <section className="mt-4 rounded-lg border border-warning/40 bg-warning/5 p-4">
          <h2 className="text-h2 text-warning">This backend is unauthenticated</h2>
          <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
            It is running with <code className="font-mono text-sm">AUTH_MODE=disabled</code>,
            so anyone who can reach it has whatever access its kubeconfig has.
            Acceptable for local development, never against a production cluster.
          </p>
        </section>
      ) : null}

      <section className="mt-4 rounded-lg border border-line bg-surface p-4">
        <h2 className="text-h2">Session</h2>
        <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
          Credentials are held for this browser tab only and are cleared when it
          closes.
        </p>
        <button
          type="button"
          onClick={signOut}
          className="mt-4 rounded-md border border-line bg-raised px-3 py-2 text-sm transition-colors duration-fast hover:border-critical hover:text-critical focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
        >
          Sign out
        </button>
      </section>
    </div>
  );
}
