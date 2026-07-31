/**
 * The document, and the citation interaction that defines it.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReportDocument } from "./ReportDocument";
import type { IncidentComposition } from "../../lib/report";
import type {
  Diagnosis,
  EvidenceEntry,
  InvestigationResponse,
} from "../../types/investigation";

const section = (title: string, extra: Partial<IncidentComposition["sections"][0]> = {}) => ({
  title,
  body: [],
  fields: [],
  table: [],
  headers: [],
  note: "",
  ...extra,
});

const COMPOSITION: IncidentComposition = {
  incident_id: "INC-1",
  title: "Memory limit too low",
  generated_at: "2026-07-31T02:41:00Z",
  sections: [
    section("Executive Summary", { fields: [{ label: "Cluster", value: "prod-eu-west" }] }),
    section("Root Cause", { body: ["The checkout container is being OOMKilled."] }),
    section("Evidence", { body: ["9 of 11 records were usable."] }),
    section("Confidence Assessment", { body: ["Overall confidence: 87%."] }),
    section("Preventive Actions", { body: ["Add an alert on restart count."] }),
  ],
};

const DIAGNOSIS = {
  root_cause: "Memory limit too low",
  confidence: 87,
  cited_evidence: ["k8s.pods.logs:pod/a"],
  signals: [
    {
      id: "pod.oom:pod/a",
      summary: "Container OOMKilled",
      severity: "critical",
      evidence_ids: ["k8s.pods.logs:pod/a"],
    },
  ],
  confidence_breakdown: [
    { component: "evidence strength", weight: 50, score: 90, contribution: 45, detail: "" },
    { component: "ai confidence", weight: 30, score: 80, contribution: 24, detail: "" },
  ],
} as unknown as Diagnosis;

const INVESTIGATION = {
  evidence: [
    {
      id: "k8s.pods.logs:pod/a",
      kind: "k8s.pods.logs",
      status: "ok",
      command: "kubectl logs checkout --previous",
      detail: "",
    },
    {
      id: "k8s.metrics.pods:cluster",
      kind: "k8s.metrics.pods",
      status: "unavailable",
      command: null,
      detail: "metrics-server is unavailable",
    },
  ] as EvidenceEntry[],
  evidence_coverage: { total: 11, usable: 9, completeness: 90 },
} as unknown as InvestigationResponse["investigation"];

function renderDocument(overrides: Partial<Parameters<typeof ReportDocument>[0]> = {}) {
  const onSelectEvidence = vi.fn();
  render(
    <ReportDocument
      composition={COMPOSITION}
      diagnosis={DIAGNOSIS}
      investigation={INVESTIGATION}
      selectedEvidence=""
      onSelectEvidence={onSelectEvidence}
      {...overrides}
    />,
  );
  return { onSelectEvidence };
}

describe("sections", () => {
  it("renders them in the composer's order", () => {
    renderDocument();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent);
    expect(headings).toEqual([
      "Executive Summary",
      "Root Cause",
      "Evidence",
      "Confidence Assessment",
      "Preventive Actions",
    ]);
  });

  it("renders a section it has never seen before", () => {
    // The property that keeps the screen and the postmortem in step: a section
    // added to the composer must appear without a frontend change.
    renderDocument({
      composition: {
        ...COMPOSITION,
        sections: [section("Blast Radius", { body: ["Three services depend on this."] })],
      },
    });

    expect(screen.getByRole("heading", { name: "Blast Radius" })).toBeInTheDocument();
    expect(screen.getByText("Three services depend on this.")).toBeInTheDocument();
  });

  it("renders nothing without a composition", () => {
    const { container } = render(
      <ReportDocument
        composition={undefined}
        selectedEvidence=""
        onSelectEvidence={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("citations", () => {
  it("attaches the evidence a claim rests on to the claim", async () => {
    const user = userEvent.setup();
    const { onSelectEvidence } = renderDocument();

    const chip = screen.getAllByRole("button", { name: /evidence 1/i })[0];
    await user.click(chip);

    expect(onSelectEvidence).toHaveBeenCalledWith("k8s.pods.logs:pod/a");
  });

  it("names the record and its command, so the chip is not a mystery", () => {
    renderDocument();
    expect(
      screen.getAllByRole("button", { name: /kubectl logs checkout --previous/i })[0],
    ).toBeInTheDocument();
  });

  it("says so when a conclusion cited nothing", () => {
    // Never a fabricated chip: the absence of evidence is itself reportable.
    renderDocument({ diagnosis: { ...DIAGNOSIS, cited_evidence: [] } as Diagnosis });
    expect(screen.getByText(/no evidence was cited/i)).toBeInTheDocument();
  });

  it("marks the selected citation as pressed", () => {
    renderDocument({ selectedEvidence: "k8s.pods.logs:pod/a" });
    const chips = screen
      .getAllByRole("button", { name: /evidence 1/i })
      .filter((node) => node.getAttribute("aria-pressed") === "true");
    expect(chips.length).toBeGreaterThan(0);
  });
});

describe("evidence", () => {
  it("shows coverage as collected over total", () => {
    renderDocument();
    expect(screen.getByText(/9 of 11 records were usable/i)).toBeInTheDocument();
  });

  it("lists gaps as findings rather than omitting them", () => {
    // A gap is a result. "We could not look" must stay distinguishable from
    // "we looked and everything was fine".
    renderDocument();
    const heading = screen.getByRole("heading", { name: "Evidence" });
    const region = heading.parentElement as HTMLElement;

    expect(within(region).getByText(/gaps/i)).toBeInTheDocument();
    expect(within(region).getByText("k8s.metrics.pods")).toBeInTheDocument();
    expect(within(region).getByText(/metrics-server is unavailable/i)).toBeInTheDocument();
  });

  it("does not list a usable record as a gap", () => {
    renderDocument();
    const heading = screen.getByRole("heading", { name: "Evidence" });
    const region = heading.parentElement as HTMLElement;
    expect(within(region).queryByText("k8s.pods.logs")).not.toBeInTheDocument();
  });
});

describe("confidence", () => {
  it("shows the composition, not just the number", () => {
    renderDocument();
    expect(screen.getByText("87%")).toBeInTheDocument();
    expect(screen.getByText(/evidence strength 90% × weight 50%/i)).toBeInTheDocument();
  });
});

describe("provenance", () => {
  it("labels prose a model wrote", () => {
    // F1 in PRODUCTION_READINESS: fix, prevention and next_steps are still
    // model-authored, and the UI has to say so.
    renderDocument();
    expect(screen.getByText(/model-authored/i)).toBeInTheDocument();
  });

  it("does not label sections the backend computed", () => {
    renderDocument({
      composition: { ...COMPOSITION, sections: [COMPOSITION.sections[0]] },
    });
    expect(screen.queryByText(/model-authored/i)).not.toBeInTheDocument();
  });
});
