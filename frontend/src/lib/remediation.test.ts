/**
 * The remediation artefacts, tested for the first time.
 *
 * These five functions lived inside `App.tsx` among the components that call
 * them, which is why they had no tests: reaching `buildRemediationYaml` meant
 * rendering a panel, clicking a tab and reading a `<pre>`. Moving them to
 * `src/lib/` is what makes the assertions below possible, and that is the whole
 * argument for the move.
 *
 * `buildRemediationYaml` writes a manifest a person is invited to apply to a
 * production cluster. It is the single highest-consequence piece of pure logic
 * in the console.
 */
import { describe, expect, it } from "vitest";

import {
  buildApplyPlan,
  buildPrDescription,
  buildRemediationYaml,
  firstAffectedWorkload,
} from "./remediation";
import type { Diagnosis, InvestigationData } from "../types/investigation";

const pod = (name: string, namespace: string) =>
  ({ pods: { problematic_pods: [{ name, namespace }] } }) as unknown as InvestigationData;

const deployment = (name: string, namespace: string) =>
  ({
    deployments: { unhealthy_deployments: [{ name, namespace }] },
  }) as unknown as InvestigationData;

const both = (podName: string, deploymentName: string) =>
  ({
    pods: { problematic_pods: [{ name: podName, namespace: "pods-ns" }] },
    deployments: { unhealthy_deployments: [{ name: deploymentName, namespace: "deploy-ns" }] },
  }) as unknown as InvestigationData;

describe("firstAffectedWorkload", () => {
  it("prefers the controller over the pod it owns", () => {
    // The same rule `app/remediation/` follows server-side: a pod is not the
    // thing to patch, because the controller will recreate it. Asserted on the
    // name as well as the kind — returning "Deployment" while naming the pod
    // would produce a manifest that patches an object that does not exist.
    expect(firstAffectedWorkload(both("web-7d4f-x9k2", "web"))).toEqual({
      kind: "Deployment",
      name: "web",
      namespace: "deploy-ns",
    });
  });

  it("falls back to the pod when nothing owns it", () => {
    expect(firstAffectedWorkload(pod("standalone", "prod"))).toEqual({
      kind: "Pod",
      name: "standalone",
      namespace: "prod",
    });
  });

  it("says it does not know rather than guessing", () => {
    const workload = firstAffectedWorkload(undefined);
    expect(workload.name).toBe("<deployment-name>");
    expect(workload.namespace).toBe("<namespace>");
  });
});

describe("buildRemediationYaml", () => {
  it("emits a Pod manifest for a pod, with no template stanza", () => {
    // The structural difference that makes the two manifests valid or not: a
    // Pod has containers directly under `spec`, a Deployment has them under
    // `spec.template.spec`. Emitting one shape for the other kind produces
    // something kubectl rejects — or worse, something it accepts and that does
    // nothing.
    const yaml = buildRemediationYaml(undefined, pod("standalone", "prod"));
    expect(yaml).toContain("apiVersion: v1");
    expect(yaml).toContain("kind: Pod");
    expect(yaml).not.toContain("template:");
    expect(yaml).toMatch(/^spec:\n {2}containers:$/m);
  });

  it("emits a Deployment manifest with the containers under the template", () => {
    const yaml = buildRemediationYaml(undefined, deployment("web", "prod"));
    expect(yaml).toContain("apiVersion: apps/v1");
    expect(yaml).toContain("kind: Deployment");
    expect(yaml).toMatch(/^spec:\n {2}template:\n {4}spec:\n {6}containers:$/m);
  });

  it("names the workload it was given, in its own namespace", () => {
    const yaml = buildRemediationYaml(undefined, deployment("payments-api", "billing"));
    expect(yaml).toContain("  name: payments-api");
    expect(yaml).toContain("  namespace: billing");
  });

  it("asks for a valid tag when the root cause is about the image", () => {
    const diagnosis = { root_cause: "ImagePullBackOff: the tag does not exist" } as Diagnosis;
    expect(buildRemediationYaml(diagnosis, deployment("web", "prod"))).toContain(
      "<replace-with-valid-image-tag>",
    );
  });

  it("does not, when it is about something else", () => {
    // The inverse matters: if both branches produced the same string the check
    // above would pass for a function that ignores the diagnosis entirely.
    const diagnosis = { root_cause: "OOMKilled: the limit is too low" } as Diagnosis;
    const yaml = buildRemediationYaml(diagnosis, deployment("web", "prod"));
    expect(yaml).toContain("<validated-image-tag>");
    expect(yaml).not.toContain("<replace-with-valid-image-tag>");
  });

  it("survives a diagnosis with no root cause", () => {
    // A defect that shipped: `diagnosis?.root_cause.toLowerCase()` guards the
    // diagnosis and not the field, and the backend types this dict as
    // `dict[str, Any]` — so the interface saying `root_cause` is required
    // proves nothing about what arrives. It crashed the whole page.
    expect(() => buildRemediationYaml({} as Diagnosis, deployment("web", "prod"))).not.toThrow();
    expect(() => buildRemediationYaml(undefined, undefined)).not.toThrow();
  });

  it("never emits an empty name or namespace", () => {
    // An empty `name:` is valid YAML and applies to nothing. Every path must
    // produce either a real value or a visible placeholder.
    for (const investigation of [undefined, pod("p", "n"), deployment("d", "n")]) {
      const yaml = buildRemediationYaml(undefined, investigation);
      expect(yaml).not.toMatch(/^ *(name|namespace):\s*$/m);
    }
  });
});

describe("buildApplyPlan", () => {
  it("validates in the namespace it targeted", () => {
    const plan = buildApplyPlan(undefined, deployment("web", "billing"));
    expect(plan).toContain("Target: Deployment billing/web");
    expect(plan).toContain("kubectl get pods -n billing");
  });

  it("uses the diagnosis's own commands when it has them", () => {
    const diagnosis = { kubectl_commands: ["kubectl describe pod web-0"] } as Diagnosis;
    const plan = buildApplyPlan(diagnosis, deployment("web", "prod"));
    expect(plan).toContain("- kubectl describe pod web-0");
    expect(plan).not.toContain("- kubectl get pods -A");
  });

  it("falls back to something safe when it has none", () => {
    expect(buildApplyPlan(undefined, undefined)).toContain("kubectl get pods -A");
  });
});

describe("buildPrDescription", () => {
  it("carries the diagnosis through", () => {
    const diagnosis = {
      root_cause: "The image tag was deleted upstream",
      fix: "Pin the digest",
      kubectl_commands: ["kubectl rollout status deploy/web"],
      remediation_risk: { level: "medium" },
    } as Diagnosis;
    const text = buildPrDescription(diagnosis);
    expect(text).toContain("The image tag was deleted upstream");
    expect(text).toContain("Pin the digest");
    expect(text).toContain("- `kubectl rollout status deploy/web`");
    expect(text).toContain("medium");
  });

  it("says there is nothing yet rather than asserting a clean bill of health", () => {
    // The console's own rule: never display something the backend did not
    // report. An empty "Root Cause" section reads as "no root cause found",
    // which in a document titled "Kubernetes Remediation" is a claim.
    //
    // **Asserted per section, not on the document.** The first version of this
    // test checked `toContain("Run an investigation")` against the whole
    // string, and passed with the Root Cause fallback emptied — because the
    // Fix section carries a near-identical sentence. It survived its own
    // mutation, and did so while reading as though it had caught it.
    const text = buildPrDescription(undefined);
    const section = (name: string) =>
      text.split(`## ${name}\n`)[1]?.split("\n##")[0]?.trim();

    expect(section("Root Cause")).toBe("Run an investigation to generate a root cause.");
    expect(section("Fix")).toBe("Run an investigation to generate a suggested fix.");
    expect(section("Risk")).toBe("Pending");
    expect(section("Validation Commands")).toContain("kubectl get pods -A");
  });
});
