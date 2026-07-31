export interface HealthResponse {
  status: string;
  service: string;
  /** Authentication the backend requires: "oidc" | "token" | "disabled". */
  auth_mode: string;
  /** True when the backend accepts unauthenticated requests. */
  insecure: boolean;
}
