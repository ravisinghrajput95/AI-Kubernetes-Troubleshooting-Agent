/**
 * Minimal JSON transport over `fetch`.
 *
 * Replaces axios, which cost more gzipped than the entire console's own code
 * for eight JSON requests. This keeps the behaviour that was actually relied
 * on — a base URL, a request timeout, JSON encoding/decoding, and throwing on
 * non-2xx — and drops the rest.
 */

export type ApiErrorKind = "network" | "timeout" | "http";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;

  constructor(message: string, kind: ApiErrorKind, status: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

export const DEFAULT_TIMEOUT_MS = 120_000;

export const apiBaseUrl: string =
  import.meta.env.react_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function messageForStatus(status: number, detail: string): string {
  if (detail) {
    return detail;
  }
  if (status === 404) {
    return "The requested investigation was not found.";
  }
  if (status === 409) {
    return "The investigation is no longer in a cancellable state.";
  }
  if (status >= 500) {
    return "The backend failed to complete the request. Check backend logs.";
  }
  return `Request failed with status ${status}.`;
}

/** FastAPI reports errors as `{"detail": "..."}`; surface that when present. */
async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    return typeof body?.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

async function parseBody<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

export async function request<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // AbortController rather than AbortSignal.timeout so the timer is always
  // cleared, and so the reason for aborting is unambiguous.
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      method,
      signal: controller.signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (timedOut) {
      throw new ApiError(
        "The request timed out. Verify cluster access and try again.",
        "timeout",
      );
    }
    throw new ApiError(
      "Unable to reach the backend API. Confirm FastAPI is running on port 8000.",
      "network",
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    throw new ApiError(
      messageForStatus(response.status, await readDetail(response)),
      "http",
      response.status,
    );
  }

  return parseBody<T>(response);
}

export const get = <T>(path: string) => request<T>("GET", path);
export const post = <T>(path: string, body?: unknown) =>
  request<T>("POST", path, body ?? {});
