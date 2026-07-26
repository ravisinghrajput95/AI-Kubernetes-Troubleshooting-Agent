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
    evidence?: EvidenceEntry[];
    evidence_coverage?: EvidenceCoverage;
    deep_evidence?: Record<string, DeepEvidenceEntry[]>;
    playbook_rounds?: PlaybookRound[];
  };
}

export type EvidenceStatus =
  | "ok"
  | "empty"
  | "unavailable"
  | "forbidden"
  | "timeout"
  | "not_applicable"
  | "failed";

export interface ResourceRef {
  kind: string;
  name: string;
  namespace?: string | null;
  uid?: string | null;
}

export interface EvidenceEntry {
  id: string;
  kind: string;
  source: string;
  status: EvidenceStatus;
  target: ResourceRef;
  detail: string;
  command: string | null;
  collector_id: string;
  duration_ms: number;
  redacted: boolean;
  collected_at: string;
}

export interface EvidenceCoverage {
  total: number;
  usable: number;
  completeness: number;
  by_status?: Record<string, number>;
  degraded?: Array<{ kind: string; status: string; detail: string }>;
}

export interface DeepEvidenceEntry {
  id: string;
  target: ResourceRef;
  status: EvidenceStatus;
  data: unknown;
}

export interface PlaybookRound {
  round: number;
  playbooks: string[];
  collectors: string[];
  evidence_added: number;
}

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface Signal {
  id: string;
  type: string;
  domain: string;
  severity: Severity;
  summary: string;
  target: ResourceRef;
  evidence_ids: string[];
  attributes?: Record<string, unknown>;
}

export interface Hypothesis {
  id: string;
  title: string;
  category: string;
  severity: Severity;
  confidence: number;
  rationale: string;
  target: ResourceRef;
  supporting_signals: string[];
  refuting_signals: string[];
  missing_evidence: string[];
  remediation_hint: string;
}

export interface ConfidenceComponent {
  component: string;
  weight: number;
  score: number;
  contribution: number;
  detail: string;
}

export interface RemediationStep {
  description: string;
  command: string | null;
  manual: boolean;
}

export interface RemediationRiskDetail {
  level: "Low" | "Medium" | "High" | "Critical";
  change_kind: string;
  restart_required: boolean;
  estimated_downtime: string;
  blast_radius: string;
  reversible: boolean;
  notes: string[];
}

export interface RequiredPermission {
  verbs: string[];
  resources: string[];
  namespace: string | null;
  check_command: string;
}

export interface Patch {
  format: "kubectl" | "yaml" | "helm-values" | "kustomize";
  filename: string;
  content: string;
  description: string;
  apply_command: string | null;
}

export interface RemediationPlan {
  id: string;
  hypothesis_id: string;
  title: string;
  summary: string;
  target: ResourceRef;
  risk: RemediationRiskDetail;
  requires_approval: boolean;
  preconditions: RemediationStep[];
  remediation: RemediationStep[];
  verification: RemediationStep[];
  rollback: RemediationStep[];
  required_permissions: RequiredPermission[];
  patches: Patch[];
  signal_ids: string[];
  evidence_ids: string[];
  caveats: string[];
}

export interface Grounding {
  valid: boolean;
  reason: string;
  selected_hypothesis: string | null;
  cited_signals: string[];
  rejected_citations: string[];
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
  signals?: Signal[];
  hypotheses?: Hypothesis[];
  selected_hypothesis?: string | null;
  cited_signals?: string[];
  cited_evidence?: string[];
  confidence_breakdown?: ConfidenceComponent[];
  grounding?: Grounding;
  remediation?: RemediationPlan | null;
  patches?: Patch[];
}

export type JobStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface JobEvent {
  type: "queued" | "started" | "progress" | "completed" | "failed" | "cancelled";
  message: string;
  at: string;
  time: string;
  data?: Record<string, unknown>;
}

export interface InvestigationJobAccepted {
  id: string;
  status: JobStatus;
  status_url: string;
  events_url: string;
}

export interface InvestigationJobState {
  id: string;
  status: JobStatus;
  request?: Record<string, unknown>;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  progress?: { completed_steps: number; total_events: number };
  timeline?: JobEvent[];
  error?: string;
  persisted?: boolean;
  investigation?: InvestigationResponse["investigation"];
  diagnosis?: Diagnosis;
  history_item?: InvestigationHistoryItem;
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
