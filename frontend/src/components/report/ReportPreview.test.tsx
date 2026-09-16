import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReportPreview } from "./ReportPreview";
import type { InvestigationReport } from "../../types/investigation";

const report = (
  breakdown: Array<{ source: string; contribution: number; weight?: number; score?: number }>,
) =>
  ({
    incident_id: "INC-20260916-AC115D4C",
    timestamp: "2026-09-16T05:44:00Z",
    status: "success",
    namespace: "payments",
    report_metadata: {
      cluster: "kind-k8s-agent-dev",
      severity: "Critical",
      incident_status: "Issues found",
      environment: "Development",
      confidence_breakdown: breakdown,
    },
    diagnosis: { confidence: 94, root_cause: "Container image cannot be pulled" },
    investigation: {},
  }) as unknown as InvestigationReport;

describe("the confidence breakdown", () => {
  it("is not called the model's, and shows the arithmetic that reaches the score", () => {
    // It rendered "AI Confidence Breakdown / Pod Analysis 25%" on a
    // deterministic diagnosis: fixed per-section weights unrelated to the 94%
    // printed above them.
    render(
      <ReportPreview
        report={report([
          { source: "Evidence Strength", contribution: 64, weight: 70, score: 92 },
          { source: "Evidence Completeness", contribution: 30, weight: 30, score: 100 },
        ])}
        onClose={() => {}}
      />,
    );

    expect(screen.queryByText(/AI Confidence Breakdown/i)).not.toBeInTheDocument();
    expect(screen.getByText(/how the confidence was reached/i)).toBeInTheDocument();
    expect(screen.getByText(/Evidence Strength · 92% × weight 70%/)).toBeInTheDocument();
    expect(screen.queryByText(/Pod Analysis/)).not.toBeInTheDocument();
  });

  it("is omitted rather than padded when the diagnosis carries none", () => {
    render(<ReportPreview report={report([])} onClose={() => {}} />);
    expect(screen.queryByText(/how the confidence was reached/i)).not.toBeInTheDocument();
  });
});
