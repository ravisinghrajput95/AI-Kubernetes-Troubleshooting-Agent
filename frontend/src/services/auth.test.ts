import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authHeaders, clearToken, getToken, onTokenChange, setToken } from "./auth";

beforeEach(() => {
  window.sessionStorage.clear();
  clearToken();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("credential storage", () => {
  it("round-trips a token", () => {
    setToken("abc123");
    expect(getToken()).toBe("abc123");
  });

  it("trims surrounding whitespace", () => {
    setToken("  abc123\n");
    expect(getToken()).toBe("abc123");
  });

  it("starts with no token", () => {
    expect(getToken()).toBe("");
  });

  it("uses session storage, not local storage", () => {
    // The credential reaches a backend holding a kubeconfig. Session scope
    // bounds how long a token left on a shared machine stays usable.
    setToken("abc123");
    expect(window.sessionStorage.getItem("k8s-agent-token")).toBe("abc123");
    expect(window.localStorage.getItem("k8s-agent-token")).toBeNull();
  });

  it("removes the stored value when cleared", () => {
    setToken("abc123");
    clearToken();
    expect(getToken()).toBe("");
    expect(window.sessionStorage.getItem("k8s-agent-token")).toBeNull();
  });

  it("survives storage being unavailable", () => {
    // Private modes and sandboxed frames throw on access; an in-memory
    // session is still a working session.
    const spy = vi.spyOn(window.sessionStorage, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(window.sessionStorage, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });

    expect(() => setToken("abc123")).not.toThrow();
    expect(getToken()).toBe("abc123");
    spy.mockRestore();
  });
});

describe("authHeaders", () => {
  it("sends nothing when there is no credential", () => {
    expect(authHeaders()).toEqual({});
  });

  it("sends a bearer header when there is one", () => {
    setToken("abc123");
    expect(authHeaders()).toEqual({ Authorization: "Bearer abc123" });
  });
});

describe("onTokenChange", () => {
  it("notifies on sign in", () => {
    const seen: string[] = [];
    const stop = onTokenChange((token) => seen.push(token));

    setToken("abc123");
    stop();

    expect(seen).toEqual(["abc123"]);
  });

  it("notifies with an empty token when the credential is rejected", () => {
    // This is what turns a 401 into a sign-in prompt rather than a screen of
    // failed requests.
    const seen: string[] = [];
    setToken("abc123");
    const stop = onTokenChange((token) => seen.push(token));

    clearToken();
    stop();

    expect(seen).toEqual([""]);
  });

  it("stops notifying once unsubscribed", () => {
    const seen: string[] = [];
    onTokenChange((token) => seen.push(token))();

    setToken("abc123");
    expect(seen).toEqual([]);
  });
});
