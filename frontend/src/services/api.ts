import axios from "axios";

import type { HealthResponse } from "../types/health";
import type {
  InvestigationHistoryItem,
  InvestigationReport,
  InvestigationResponse,
  KubernetesContextResponse,
} from "../types/investigation";

const apiBaseUrl =
  import.meta.env.react_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export const api = axios.create({
  baseURL: apiBaseUrl,
  timeout: 120000,
});

export async function getHealth(): Promise<HealthResponse> {
  const response = await api.get<HealthResponse>("/health");
  return response.data;
}

export async function investigateCluster(
  context?: string,
  options?: {
    namespace?: string;
    resource_kind?: string;
    resource_name?: string;
  },
): Promise<InvestigationResponse> {
  const response = await api.post<InvestigationResponse>("/investigate", {
    context,
    ...options,
  });
  return response.data;
}

export async function getKubernetesContexts(): Promise<KubernetesContextResponse> {
  const response = await api.get<KubernetesContextResponse>("/clusters");
  return response.data;
}

export async function getInvestigationHistory(): Promise<InvestigationHistoryItem[]> {
  const response = await api.get<{ items: InvestigationHistoryItem[] }>(
    "/investigations",
  );
  return response.data.items;
}

export async function getInvestigationReport(
  id: string,
): Promise<InvestigationReport> {
  const response = await api.get<InvestigationReport>(`/investigations/${id}/report`);
  return response.data;
}

export async function regenerateInvestigationReport(
  id: string,
): Promise<InvestigationReport> {
  const response = await api.post<{ report: InvestigationReport }>(
    `/investigations/${id}/regenerate`,
  );
  return response.data.report;
}

export function reportUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}
