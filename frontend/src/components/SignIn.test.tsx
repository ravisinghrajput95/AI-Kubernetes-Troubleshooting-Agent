/**
 * The sign-in screen replaces one that authenticated nothing.
 *
 * These assert the two things it exists for: sending a real credential, and
 * making an unauthenticated backend visible instead of silent.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SignIn } from "./SignIn";
import { clearToken, getToken } from "../services/auth";
import type { HealthResponse } from "../types/health";

const TOKEN_BACKEND: HealthResponse = {
  status: "healthy",
  service: "ai-kubernetes-agent",
  auth_mode: "token",
  insecure: false,
};

const OIDC_BACKEND: HealthResponse = { ...TOKEN_BACKEND, auth_mode: "oidc" };

const OPEN_BACKEND: HealthResponse = {
  ...TOKEN_BACKEND,
  auth_mode: "disabled",
  insecure: true,
};

beforeEach(() => {
  window.sessionStorage.clear();
  clearToken();
});

describe("an authenticated backend", () => {
  it("stores the credential and continues", async () => {
    const user = userEvent.setup();
    const onAuthenticated = vi.fn();
    render(<SignIn health={TOKEN_BACKEND} onAuthenticated={onAuthenticated} />);

    await user.type(screen.getByLabelText(/api token/i), "abc123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(getToken()).toBe("abc123");
    expect(onAuthenticated).toHaveBeenCalled();
  });

  it("will not submit an empty credential", async () => {
    render(<SignIn health={TOKEN_BACKEND} onAuthenticated={vi.fn()} />);
    expect(screen.getByRole("button", { name: /sign in/i })).toBeDisabled();
  });

  it("names the credential the backend actually wants", () => {
    render(<SignIn health={OIDC_BACKEND} onAuthenticated={vi.fn()} />);
    expect(screen.getByLabelText(/access token/i)).toBeInTheDocument();
    expect(screen.getByText(/identity provider/i)).toBeInTheDocument();
  });

  it("does not warn when the backend is protected", () => {
    render(<SignIn health={TOKEN_BACKEND} onAuthenticated={vi.fn()} />);
    expect(screen.queryByText(/unauthenticated requests/i)).not.toBeInTheDocument();
  });

  it("shows why a previous attempt failed", () => {
    render(
      <SignIn
        health={TOKEN_BACKEND}
        error="Your session is no longer valid. Sign in again to continue."
        onAuthenticated={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer valid/i);
  });

  it("keeps the credential out of view while typing", async () => {
    const user = userEvent.setup();
    render(<SignIn health={TOKEN_BACKEND} onAuthenticated={vi.fn()} />);

    const field = screen.getByLabelText(/api token/i);
    await user.type(field, "abc123");
    expect(field).toHaveAttribute("type", "password");
  });
});

describe("an unauthenticated backend", () => {
  it("says so, in terms of what it means", () => {
    render(<SignIn health={OPEN_BACKEND} onAuthenticated={vi.fn()} />);

    expect(screen.getByText(/accepting unauthenticated requests/i)).toBeInTheDocument();
    expect(screen.getByText(/kubeconfig/i)).toBeInTheDocument();
  });

  it("asks for no credential, because none would be checked", () => {
    render(<SignIn health={OPEN_BACKEND} onAuthenticated={vi.fn()} />);
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
  });

  it("still requires an explicit acknowledgement", async () => {
    const user = userEvent.setup();
    const onAuthenticated = vi.fn();
    render(<SignIn health={OPEN_BACKEND} onAuthenticated={onAuthenticated} />);

    expect(onAuthenticated).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /continue anyway/i }));
    expect(onAuthenticated).toHaveBeenCalled();
  });
});

describe("an unreachable backend", () => {
  it("says the method is unknown rather than guessing", () => {
    render(<SignIn onAuthenticated={vi.fn()} />);

    expect(screen.getByText(/backend unreachable/i)).toBeInTheDocument();
    expect(screen.getByText(/sign-in method it requires is unknown/i)).toBeInTheDocument();
  });

  it("still accepts a credential, for when it returns", async () => {
    const user = userEvent.setup();
    render(<SignIn onAuthenticated={vi.fn()} />);

    await user.type(screen.getByLabelText(/token/i), "abc123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(getToken()).toBe("abc123");
  });
});
