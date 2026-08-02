import { apiBaseUrl, get, post } from "./http";

import type { HealthResponse } from "../types/health";
import type {
  AgentStatus,
  InvestigationHistoryItem,
  InvestigationJobAccepted,
  InvestigationJobState,
  InvestigationJobStatus,
  InvestigationReport,
  InvestigationResponse,
  KubernetesContextResponse,
} from "../types/investigation";

export interface InvestigationScope {
  namespace?: string;
  resource_kind?: string;
  resource_name?: string;
}

export { ApiError } from "./http";

export function getHealth(): Promise<HealthResponse> {
  return get<HealthResponse>("/health");
}

/** The signed-in caller. `/health` is unauthenticated and cannot answer this. */
export interface SessionInfo {
  subject: string;
  email: string;
  groups: string[];
  tenant: string;
  auth_method: string;
  anonymous: boolean;
  /** Where to end the provider's session. Empty unless OIDC publishes one. */
  end_session_url: string;
  multi_tenant: boolean;
  /** This caller's role in their tenant. Empty means they hold none. */
  role: string;
  /**
   * Where the role came from: `assigned`, `group`, `assigned+group`,
   * `default`, `open-deployment`, `suspended`, or `none`.
   *
   * Carried so the console can distinguish "you were never granted anything"
   * from "you were suspended" — the same denial to the API, and two different
   * answers to the person reading the screen.
   */
  role_source: string;
  /**
   * What this caller may do. The console gates on these rather than on the
   * role name, so a role gaining a permission needs no console change and a
   * button is never shown for something the backend will refuse.
   */
  permissions: string[];
}

/** Permission strings the console gates on. Mirrors `app/authz/models.py`. */
export const PERMISSIONS = {
  investigationRun: "investigation.run",
  clusterEnrol: "cluster.enrol",
  memberRead: "member.read",
} as const;

export function permits(session: SessionInfo | undefined, permission: string): boolean {
  // Undefined while `/me` is in flight. Treated as permitted so the UI does not
  // flicker a disabled state on every load; the backend is the actual gate and
  // answers 403 regardless.
  return session === undefined || session.permissions.includes(permission);
}

export function getSession(): Promise<SessionInfo> {
  return get<SessionInfo>("/me");
}

export function investigateCluster(
  context?: string,
  options?: InvestigationScope,
): Promise<InvestigationResponse> {
  return post<InvestigationResponse>("/investigate", { context, ...options });
}

export function getKubernetesContexts(): Promise<KubernetesContextResponse> {
  return get<KubernetesContextResponse>("/clusters");
}

export interface AgentFleet {
  items: AgentStatus[];
  gateway_enabled: boolean;
  trust_domain: string;
  /** "worker": agents attached to the worker that answered, not the fleet. */
  scope: string;
}

export function getAgents(): Promise<AgentFleet> {
  return get<AgentFleet>("/agents");
}

export interface Enrolment {
  cluster_id: string;
  /** Returned exactly once. The platform stores only its digest. */
  token: string;
  expires_in_minutes: number;
  ca_bundle: string;
  gateway_endpoint: string;
  enrolment_endpoint: string;
  manifest: string;
  docker_command: string;
}

export function createEnrolment(
  clusterId: string,
  ttlMinutes = 60,
): Promise<Enrolment> {
  return post<Enrolment>("/agents/enrolment", {
    cluster_id: clusterId,
    ttl_minutes: ttlMinutes,
  });
}

export async function getInvestigationHistory(): Promise<InvestigationHistoryItem[]> {
  const response = await get<{ items: InvestigationHistoryItem[] }>("/investigations");
  return response.items;
}

/** In-flight and recently finished jobs. Used to attribute older history
 *  entries to a cluster; new entries carry their own context. */
export async function getInvestigationJobs(): Promise<InvestigationJobState[]> {
  const response = await get<{ items: InvestigationJobState[] }>("/investigation-jobs");
  return response.items;
}

export function getInvestigationReport(id: string): Promise<InvestigationReport> {
  return get<InvestigationReport>(`/investigations/${id}/report`);
}

export async function regenerateInvestigationReport(
  id: string,
): Promise<InvestigationReport> {
  const response = await post<{ report: InvestigationReport }>(
    `/investigations/${id}/regenerate`,
  );
  return response.report;
}

export function reportUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

/**
 * Submit an investigation and return immediately with its id.
 * The id is also the report id once the run completes.
 */
export function startInvestigationJob(
  context?: string,
  scope?: InvestigationScope,
): Promise<InvestigationJobAccepted> {
  return post<InvestigationJobAccepted>("/investigations", { context, ...scope });
}

export function getInvestigationJob(id: string): Promise<InvestigationJobState> {
  return get<InvestigationJobState>(`/investigations/${id}`);
}

/**
 * State and timeline, without the investigation.
 *
 * For the polling fallback, which asks every 1.5 seconds and reads `status` and
 * `timeline`. The full endpoint would re-serialise the whole finished
 * investigation on every tick; polling is already the degraded transport
 * because a proxy blocked SSE, and it should not also be the expensive one.
 */
export function getInvestigationJobStatus(id: string): Promise<InvestigationJobStatus> {
  return get<InvestigationJobStatus>(`/investigations/${id}/status`);
}

export async function cancelInvestigationJob(id: string): Promise<void> {
  await post(`/investigations/${id}/cancel`);
}

/** Absolute URL for the progress stream; EventSource cannot use the JSON client. */
export function eventStreamUrl(id: string): string {
  return `${apiBaseUrl}/investigations/${id}/events`;
}
