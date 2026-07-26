export interface InvestigationResponse {
  status: string;
  diagnosis?: Diagnosis;
  history_item?: InvestigationHistoryItem;
  investigation: {
    context?: string;
    scope?: {
      namespace?: string;
      resource_kind?: string;
      resource_name?: string;
    };
    health?: {
      status?: string;
      message?: string;
    };
    overview?: {
      nodes?: string;
      pods?: string;
      cpu_usage?: string;
      memory_usage?: string;
      alerts?: number;
      critical_issues?: number;
    };
    severity?: {
      severity?: string;
      impact?: string;
      affected_workloads?: number;
      affected_namespace?: string;
    };
    metrics?: {
      available?: boolean;
      cpu_usage?: string;
      memory_usage?: string;
      message?: string;
      node_metrics?: Array<{
        name: string;
        cpu: string;
        cpu_percent: string;
        memory: string;
        memory_percent: string;
      }>;
      top_pods?: Array<{
        namespace: string;
        name: string;
        cpu: string;
        memory: string;
      }>;
    };
    security?: {
      status?: string;
      warning_count?: number;
      findings?: Array<{
        label: string;
        status: "pass" | "warning" | "unknown";
        detail: string;
      }>;
    };
    topology?: {
      cluster: string;
      nodes: Array<{
        name: string;
        pod_count: number;
        pods: Array<{
          name: string;
          namespace: string;
          phase: string;
        }>;
      }>;
    };
    timeline?: Array<{
      time: string;
      message: string;
    }>;
    executed_commands?: string[];
    pods?: Record<string, unknown>;
    logs?: Record<string, unknown>;
    events?: Record<string, unknown>;
    deployments?: Record<string, unknown>;
    network?: Record<string, unknown>;
    nodes?: Record<string, unknown>;
    storage?: Record<string, unknown>;
    workloads?: Record<string, unknown>;
  };
}

export interface Diagnosis {
  root_cause: string;
  explanation: string;
  fix: string;
  kubectl_commands: string[];
  prevention: string;
  evidence_gaps?: string[];
  next_steps?: string[];
  confidence: number;
  confidence_reasoning: string[] | string;
  remediation_risk?: {
    level: string;
    impact: string[];
  };
  remediation_plan?: {
    requires_approval: boolean;
    dry_run_first: boolean;
    pre_checks: string[];
    review_commands: string[];
    rollback_commands: string[];
  };
  ai_generated: boolean;
}

export interface InvestigationHistoryItem {
  id: string;
  incident_id?: string;
  timestamp: string;
  root_cause: string;
  namespace: string;
  confidence: number;
  status: string;
  severity?: string;
  incident_status?: string;
  environment?: string;
  pdf_url: string;
  json_url?: string;
  markdown_url?: string;
}

export interface InvestigationReport {
  incident_id?: string;
  timestamp: string;
  status: string;
  namespace: string;
  report_metadata?: {
    cluster?: string;
    environment?: string;
    severity?: string;
    incident_status?: string;
    business_impact?: string[];
    confidence_breakdown?: Array<{
      source: string;
      contribution: number;
    }>;
    evidence_matrix?: Array<{
      source: string;
      status: string;
    }>;
  };
  diagnosis: Diagnosis;
  investigation: InvestigationResponse["investigation"];
}

export interface KubernetesContext {
  name: string;
  cluster: string;
  current: boolean;
}

export interface KubernetesContextResponse {
  items: KubernetesContext[];
  current_context: string;
  error: string;
}
