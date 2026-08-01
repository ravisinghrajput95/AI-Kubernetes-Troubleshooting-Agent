/**
 * Where the console keeps its credential.
 *
 * `sessionStorage`, not `localStorage`, and deliberately: a bearer token here
 * reaches a backend holding a kubeconfig, so it is worth more than a display
 * name. Session storage is scoped to the tab and cleared when it closes, which
 * bounds the window in which a token left on a shared machine is usable. The
 * cost is signing in again after closing the tab, which is the right trade for
 * a credential of this value.
 *
 * The predecessor stored a display name in `localStorage` and authenticated
 * nothing — see docs/CONSOLE_REDESIGN.md §1.4.
 */

const TOKEN_KEY = "k8s-agent-token";
/** Acknowledgement that this backend accepts unauthenticated requests. */
const INSECURE_ACK_KEY = "k8s-agent-insecure-ack";

type Listener = (token: string) => void;

const listeners = new Set<Listener>();

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    // Storage can throw in private modes and sandboxed frames. An in-memory
    // session is still a working session.
    return null;
  }
}

let memoryToken = "";

export function getToken(): string {
  const store = storage();
  if (store === null) {
    return memoryToken;
  }
  return store.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  const trimmed = token.trim();
  memoryToken = trimmed;

  const store = storage();
  if (store !== null) {
    if (trimmed) {
      store.setItem(TOKEN_KEY, trimmed);
    } else {
      store.removeItem(TOKEN_KEY);
    }
  }

  for (const listener of listeners) {
    listener(trimmed);
  }
}

export function clearToken(): void {
  setToken("");
}

/**
 * Notify on credential change, including the rejection that `http.ts` reports
 * when the backend answers 401. That is what turns an expired token into a
 * sign-in prompt rather than a page of failed requests.
 */
export function onTokenChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Authorization header, or nothing when the backend needs no credential. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Whether the operator has already been told this backend is unauthenticated.
 *
 * Scoped to the tab like the credential, so the warning returns in a new
 * session rather than being dismissed once and forgotten.
 */
export function isInsecureAcknowledged(): boolean {
  return storage()?.getItem(INSECURE_ACK_KEY) === "1";
}

export function acknowledgeInsecure(): void {
  storage()?.setItem(INSECURE_ACK_KEY, "1");
}

/**
 * End the session, whichever kind it is.
 *
 * Clears the credential *and* the unauthenticated acknowledgement, so signing
 * out of an open backend returns to the warning rather than straight back in.
 *
 * With OIDC that is only half of it. Discarding this tab's copy of a token
 * leaves the provider's own session intact, so the next sign-in is a silent
 * redirect straight back in — the user believes they signed out and did not.
 * When the provider publishes an end-session endpoint the browser is sent
 * there afterwards, local state first so a failed redirect still leaves this
 * console signed out.
 */
export function signOut(endSessionUrl = ""): void {
  storage()?.removeItem(INSECURE_ACK_KEY);
  clearToken();

  if (endSessionUrl) {
    window.location.assign(endSessionUrl);
  }
}
