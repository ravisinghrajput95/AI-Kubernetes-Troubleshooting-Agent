/**
 * The artefacts the remediation panel offers: YAML, a PR description, an apply
 * plan, and the browser download that delivers them.
 *
 * Pure logic, in `src/lib/` for the reason `analysis.ts` is — it is testable
 * without rendering, and it was not tested while it lived in `App.tsx` among
 * the components that call it. `buildRemediationYaml` in particular writes a
 * manifest a person is invited to apply to a cluster, which is the last thing
 * that should only ever be exercised through a DOM.
 *
 * None of this is a fix the *platform* generated: `app/remediation/` does that
 * server-side under `assert_read_only()`, and the placeholders here are
 * deliberate — the backend refuses to invent secret values, a memory limit it
 * never observed, or a NetworkPolicy selector, and says so in a caveat rather
 * than guessing. These builders must not become cleverer than that.
 */
import type { Diagnosis, InvestigationData } from "../types/investigation";

export function downloadText(filename: string, content: string, type = "text/plain") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function firstAffectedWorkload(investigation?: InvestigationData) {
  const pods = investigation?.pods as
    | { problematic_pods?: Array<{ name?: string; namespace?: string }> }
    | undefined;
  const deployments = investigation?.deployments as
    | { unhealthy_deployments?: Array<{ name?: string; namespace?: string }> }
    | undefined;
  const pod = pods?.problematic_pods?.[0];
  const deployment = deployments?.unhealthy_deployments?.[0];

  return {
    name: deployment?.name ?? pod?.name ?? "<deployment-name>",
    namespace: deployment?.namespace ?? pod?.namespace ?? "<namespace>",
    kind: deployment ? "Deployment" : pod ? "Pod" : "Deployment",
  };
}

export function buildRemediationYaml(
  diagnosis?: Diagnosis,
  investigation?: InvestigationData,
) {
  const workload = firstAffectedWorkload(investigation);
  // `a?.b.c()` guards `a` and not `b`: a diagnosis without a root cause used
  // to crash the whole page here. The backend types this dict as
  // `dict[str, Any]`, so the interface saying it is required proves nothing.
  const rootCause = diagnosis?.root_cause?.toLowerCase() ?? "";
  const imageValue = rootCause.includes("image")
    ? "<replace-with-valid-image-tag>"
    : "<validated-image-tag>";

  if (workload.kind === "Pod") {
    return `apiVersion: v1
kind: Pod
metadata:
  name: ${workload.name}
  namespace: ${workload.namespace}
spec:
  containers:
    - name: <container-name>
      image: ${imageValue}
      resources:
        requests:
          cpu: "100m"
          memory: "128Mi"
        limits:
          cpu: "500m"
          memory: "512Mi"`;
  }

  return `apiVersion: apps/v1
kind: ${workload.kind}
metadata:
  name: ${workload.name}
  namespace: ${workload.namespace}
spec:
  template:
    spec:
      containers:
        - name: <container-name>
          image: ${imageValue}
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"`;
}

export function buildPrDescription(diagnosis?: Diagnosis) {
  return `# Kubernetes Remediation

## Root Cause
${diagnosis?.root_cause ?? "Run an investigation to generate a root cause."}

## Fix
${diagnosis?.fix ?? "Run an investigation to generate a suggested fix."}

## Validation Commands
${(diagnosis?.kubectl_commands ?? ["kubectl get pods -A"])
  .map((command) => `- \`${command}\``)
  .join("\n")}

## Risk
${diagnosis?.remediation_risk?.level ?? "Pending"}
`;
}

export function buildApplyPlan(diagnosis?: Diagnosis, investigation?: InvestigationData) {
  const workload = firstAffectedWorkload(investigation);
  const commands = diagnosis?.kubectl_commands?.length
    ? diagnosis.kubectl_commands
    : ["kubectl get pods -A", "kubectl get events -A --sort-by=.lastTimestamp"];

  return [
    `Target: ${workload.kind} ${workload.namespace}/${workload.name}`,
    "",
    "Review the generated YAML and replace any placeholder values before applying.",
    "",
    "Recommended commands:",
    ...commands.map((command) => `- ${command}`),
    "",
    "Validation:",
    `- kubectl get pods -n ${workload.namespace}`,
    `- kubectl get events -n ${workload.namespace} --sort-by=.lastTimestamp`,
  ].join("\n");
}
