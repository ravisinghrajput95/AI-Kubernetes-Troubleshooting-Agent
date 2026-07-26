import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiBaseUrl, get, post, request } from "./http";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("request", () => {
  it("prefixes the configured base URL", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "healthy" }));
    await get("/health");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Asserted against the configured value, not a literal, so the suite still
    // passes when react_PUBLIC_API_BASE_URL points elsewhere.
    expect(fetchMock.mock.calls[0][0]).toBe(`${apiBaseUrl}/health`);
  });

  it("defaults the base URL to the local backend", () => {
    expect(apiBaseUrl).toMatch(/^https?:\/\//);
  });

  it("parses a JSON response body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [1, 2] }));
    await expect(get("/clusters")).resolves.toEqual({ items: [1, 2] });
  });

  it("sends a JSON body and content type on POST", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "job-1" }));
    await post("/investigations", { context: "prod" });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ context: "prod" }));
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("omits a body and content type on GET", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await get("/health");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined();
  });

  it("returns undefined for 204 and for an empty body", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(get("/nothing")).resolves.toBeUndefined();

    fetchMock.mockResolvedValue(new Response("", { status: 200 }));
    await expect(get("/empty")).resolves.toBeUndefined();
  });
});

describe("error handling", () => {
  it("throws with the FastAPI detail message when present", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "Investigation report not found" }, 404),
    );

    await expect(get("/investigations/missing/report")).rejects.toMatchObject({
      name: "ApiError",
      kind: "http",
      status: 404,
      message: "Investigation report not found",
    });
  });

  it("falls back to a status-specific message when there is no detail", async () => {
    fetchMock.mockResolvedValue(new Response("not json", { status: 404 }));
    await expect(get("/missing")).rejects.toThrow("was not found");

    fetchMock.mockResolvedValue(new Response("", { status: 500 }));
    await expect(get("/boom")).rejects.toThrow("Check backend logs");

    fetchMock.mockResolvedValue(new Response("", { status: 409 }));
    await expect(get("/conflict")).rejects.toThrow("cancellable");
  });

  it("reports an unreachable backend as a network error", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(get("/health")).rejects.toMatchObject({
      kind: "network",
      status: null,
    });
    await expect(get("/health")).rejects.toThrow("Unable to reach the backend API");
  });

  it("reports an aborted request as a timeout", async () => {
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );

    const pending = request("GET", "/slow", undefined, 5);
    await expect(pending).rejects.toMatchObject({ kind: "timeout" });
    await expect(pending).rejects.toThrow("timed out");
  });

  it("aborts the underlying request when the timeout fires", async () => {
    let captured: AbortSignal | undefined;
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          captured = init.signal ?? undefined;
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );

    await expect(request("GET", "/slow", undefined, 5)).rejects.toThrow();
    expect(captured?.aborted).toBe(true);
  });

  it("does not abort a request that completes in time", async () => {
    let captured: AbortSignal | undefined;
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      captured = init.signal ?? undefined;
      return Promise.resolve(jsonResponse({ ok: true }));
    });

    await get("/fast");
    expect(captured?.aborted).toBe(false);
  });

  it("exposes ApiError for instanceof checks", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 500 }));
    await expect(get("/boom")).rejects.toBeInstanceOf(ApiError);
  });
});
