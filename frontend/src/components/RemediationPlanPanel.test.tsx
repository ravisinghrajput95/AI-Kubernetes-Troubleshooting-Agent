/**
 * Risk colour must not fail towards looking safe.
 *
 * The panel this replaced coloured everything except `Medium` as "good", so the
 * two `High` plans the platform emits — marking a StorageClass default, and
 * opening ingress through a default-deny NetworkPolicy — rendered green. This
 * panel fell back to `Low` for a level it did not recognise, which is the same
 * mistake waiting for the first new level or casing change.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RemediationPlanPanel } from "./RemediationPlanPanel";
import type { Diagnosis } from "../types/investigation";

function diagnosisAt(level: string): Diagnosis {
  return {
    remediation: {
      id: "permit-required-ingress",
      hypothesis_id: "network.ingress_denied_by_policy",
      title: "Permit the required ingress traffic",
      summary: "A default-deny policy drops the traffic.",
      target: { kind: "NetworkPolicy", name: "default-deny-ingress", namespace: "payments" },
      risk: {
        level,
        change_kind: "network_policy",
        restart_required: false,
        estimated_downtime: "None",
        blast_radius: "Network reachability for selected pods in payments",
        reversible: true,
        notes: [],
      },
      requires_approval: true,
      preconditions: [],
      remediation: [],
      verification: [],
      rollback: [],
      required_permissions: [],
      patches: [],
      signal_ids: [],
      evidence_ids: [],
      caveats: [],
    },
  } as unknown as Diagnosis;
}

const riskTag = (level: string) => screen.getByText(`${level} risk`);
const SAFE = /lime|emerald|green/;

describe("risk colour", () => {
  it.each(["High", "Critical"])("does not render %s risk in the safe colour", (level) => {
    render(<RemediationPlanPanel diagnosis={diagnosisAt(level)} />);
    expect(riskTag(level).className).not.toMatch(SAFE);
  });

  it("renders Low in the safe colour, so the check above can fail", () => {
    // The control: without it, a panel that coloured nothing would pass.
    render(<RemediationPlanPanel diagnosis={diagnosisAt("Low")} />);
    expect(riskTag("Low").className).toMatch(SAFE);
  });

  it("renders a level it does not recognise as neutral, not as Low", () => {
    render(<RemediationPlanPanel diagnosis={diagnosisAt("Severe")} />);
    expect(riskTag("Severe").className).not.toMatch(SAFE);
  });
});
