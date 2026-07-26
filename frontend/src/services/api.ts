import { apiBaseUrl, get, post } from "./http";

import type { HealthResponse } from "../types/health";
import type {
  InvestigationHistoryItem,
  InvestigationJobAccepted,
  InvestigationJobState,
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

export function investigateCluster(
  context?: string,
  options?: InvestigationScope,
): Promise<InvestigationResponse> {
  return post<InvestigationResponse>("/investigate", { context, ...options });
}

export function getKubernetesContexts(): Promise<KubernetesContextResponse> {
  return get<KubernetesContextResponse>("/clusters");
}

export async function getInvestigationHistory(): Promise<InvestigationHistoryItem[]> {
  const response = await get<{ items: InvestigationHistoryItem[] }>("/investigations");
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

export async function cancelInvestigationJob(id: string): Promise<void> {
  await post(`/investigations/${id}/cancel`);
}

/** Absolute URL for the progress stream; EventSource cannot use the JSON client. */
export function eventStreamUrl(id: string): string {
  return `${apiBaseUrl}/investigations/${id}/events`;
}
