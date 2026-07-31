import { useState, type FormEvent } from "react";

import type { HealthResponse } from "../types/health";
import { setToken } from "../services/auth";

/**
 * Sign in against whatever the backend actually requires.
 *
 * The mode comes from `/health`, which is unauthenticated precisely so the
 * console can read it before holding a credential. The predecessor took a
 * display name, wrote it to `localStorage` and authenticated nothing — see
 * docs/CONSOLE_REDESIGN.md §1.4 and §17.1.
 *
 * `token` and `oidc` both authenticate the same way over the wire: the backend
 * validates a bearer credential, statically or against the provider's JWKS. So
 * pasting a valid OIDC access token works today. What is *not* implemented is
 * the redirect dance that would obtain one — that needs backend callback
 * endpoints which do not exist, and is tracked as follow-up work rather than
 * faked with a button that cannot work.
 */
export function SignIn({
  health,
  error,
  onAuthenticated,
}: {
  health?: HealthResponse;
  error?: string;
  onAuthenticated: () => void;
}) {
  const [token, setTokenValue] = useState("");
  const mode = health?.auth_mode ?? "disabled";
  const insecure = health?.insecure ?? false;
  const unreachable = health === undefined;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!token.trim()) {
      return;
    }
    setToken(token);
    onAuthenticated();
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-5 text-ink">
      <section className="w-full max-w-md">
        <div className="mb-8 flex items-center gap-3">
          <span
            aria-hidden="true"
            className="grid size-9 place-items-center rounded-md border border-line bg-surface text-info"
          >
            ◈
          </span>
          <div>
            <p className="text-h2">Kubernetes Operations</p>
            <p className="text-sm text-ink-3">
              {unreachable ? "Backend unreachable" : health?.service}
            </p>
          </div>
        </div>

        {insecure ? (
          <div className="mb-6 rounded-md border border-warning/40 bg-warning/5 p-4">
            <p className="text-sm font-semibold text-warning">
              This backend is accepting unauthenticated requests
            </p>
            <p className="mt-2 text-sm leading-6 text-ink-2">
              It is running with <code className="font-mono text-sm">AUTH_MODE=disabled</code>,
              so anyone who can reach the port has whatever access its kubeconfig
              has. Acceptable for local development, never against a production
              cluster.
            </p>
            <button
              type="button"
              onClick={onAuthenticated}
              className="mt-4 rounded-md bg-info px-4 py-2 text-sm font-semibold text-canvas transition-colors duration-fast hover:bg-info/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
            >
              Continue anyway
            </button>
          </div>
        ) : null}

        {!insecure ? (
          <form onSubmit={submit}>
            <h1 className="text-h1">Sign in</h1>
            <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
              {unreachable
                ? "The backend could not be reached, so the sign-in method it requires is unknown. A token will be sent once it is available."
                : mode === "oidc"
                  ? "This deployment validates OIDC tokens. Paste an access token from your identity provider."
                  : "This deployment uses API tokens. Paste the token issued to you."}
            </p>

            <label htmlFor="token" className="mt-6 block text-label uppercase text-ink-3">
              {mode === "oidc" ? "Access token" : "API token"}
            </label>
            <input
              id="token"
              type="password"
              value={token}
              onChange={(event) => setTokenValue(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="••••••••••••••••"
              className="mt-2 w-full rounded-md border border-line bg-raised px-3 py-2 font-mono text-sm text-ink outline-none transition-colors duration-fast placeholder:text-ink-3 focus:border-info focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
            />

            {error ? (
              <p role="alert" className="mt-3 text-sm text-critical">
                {error}
              </p>
            ) : null}

            <button
              type="submit"
              disabled={!token.trim()}
              className="mt-6 w-full rounded-md bg-info px-4 py-2.5 text-sm font-semibold text-canvas transition-colors duration-fast hover:bg-info/90 disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
            >
              Sign in
            </button>

            <p className="mt-6 text-sm leading-6 text-ink-3">
              Tokens are held for this browser tab only and are cleared when it
              closes.
            </p>
          </form>
        ) : null}
      </section>
    </main>
  );
}
