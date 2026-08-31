/**
 * The three panel invariants that have each been violated once already.
 *
 * `CLAUDE.md` names two of them as load-bearing, and both are of a shape no
 * type check and no build can catch — a panel that renders something plausible
 * where the backend reported nothing looks completely correct until you
 * compare it against the payload:
 *
 * - **Never display evidence the backend did not report.** `ConfidenceEvidence`
 *   shipped a hardcoded `["Events", "Pod Logs", …]` fallback. In a product
 *   whose premise is that nothing is asserted without evidence, placeholder
 *   content is a correctness bug, not a cosmetic one.
 * - **Progress is real.** The old `progressSteps` array advanced on a 900 ms
 *   timer with no relationship to backend work.
 * - **A cheaper path has to be visible.** F18's collection cache and the
 *   polling fallback are both routes that produce a correct-looking answer by
 *   a different means, and both are surfaced deliberately.
 *
 * The components are otherwise well served by `src/lib`, which is where the
 * logic lives; what is tested here is only what rendering can get wrong.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConfidenceBreakdown } from "./ConfidenceBreakdown";
import { EvidenceExplorer } from "./EvidenceExplorer";
import { LiveTimeline } from "./LiveTimeline";
import type {
  ConfidenceComponent,
  Diagnosis,
  EvidenceEntry,
  JobEvent,
} from "../types/investigation";

const DIAGNOSIS: Diagnosis = {
  root_cause: "The container's memory limit is below its working set.",
  explanation: "",
  fix: "",
  kubectl_commands: [],
  prevention: "",
  confidence: 82,
  confidence_reasoning: [],
  ai_generated: false,
};

const COMPONENTS: ConfidenceComponent[] = [
  {
    component: "Evidence strength",
    weight: 50,
    score: 90,
    contribution: 45,
    detail: "Three signals, all citing usable evidence.",
  },
  {
    component: "Evidence completeness",
    weight: 20,
    score: 85,
    contribution: 17,
    detail: "51 of 60 reads were usable.",
  },
];

const event = (partial: Partial<JobEvent>): JobEvent => ({
  type: "progress",
  message: "Collecting pods",
  at: "2026-08-31T10:00:00Z",
  time: "10:00:00",
  ...partial,
});

const evidence = (partial: Partial<EvidenceEntry> = {}): EvidenceEntry =>
  ({
    id: "k8s.pods:namespace/payments",
    kind: "k8s.pods",
    status: "ok",
    command: "kubectl get pods -n payments -o json",
    ...partial,
  }) as EvidenceEntry;

describe("nothing is displayed that the backend did not report", () => {
  it("says so rather than inventing a breakdown", () => {
    const { container } = render(<ConfidenceBreakdown diagnosis={DIAGNOSIS} />);

    expect(screen.getByText(/no confidence breakdown was reported/i)).toBeTruthy();
    // The regression, stated as the thing it must not do: not "the words
    // 'evidence strength' are absent" — the panel's own subtitle names the
    // three weights and always will. What must be absent is a *row*, and a row
    // is identifiable by the score × weight arithmetic only a real component
    // has. Asserting on the prose would fail on a subtitle edit and pass on a
    // reinstated placeholder, which is backwards.
    expect(container.textContent).not.toMatch(/\d+% × \d+% =/);
    expect(screen.queryByText(/pod logs/i)).toBeNull();
  });

  it("renders the components it was given, and only those", () => {
    render(
      <ConfidenceBreakdown diagnosis={{ ...DIAGNOSIS, confidence_breakdown: COMPONENTS }} />,
    );

    expect(screen.getByText("Evidence strength")).toBeTruthy();
    expect(screen.getByText("Evidence completeness")).toBeTruthy();
    // The third weight in the composition is AI confidence. The backend did not
    // report it here, so it must not appear.
    expect(screen.queryByText(/ai confidence/i)).toBeNull();
  });

  it("names the citations grounding threw away", () => {
    render(
      <ConfidenceBreakdown
        diagnosis={{
          ...DIAGNOSIS,
          confidence_breakdown: COMPONENTS,
          grounding: {
            valid: true,
            reason: "",
            selected_hypothesis: "workload.out_of_memory",
            cited_signals: ["pod.oom:pod/prod/web-0"],
            rejected_citations: ["signal.invented:pod/prod/ghost"],
          },
        }}
      />,
    );

    expect(screen.getByText(/1 fabricated citation/i)).toBeTruthy();
    expect(screen.getByText("signal.invented:pod/prod/ghost")).toBeTruthy();
  });

  it("flags a total that does not match the reported score", () => {
    // 45 + 17 = 62, not 82. Silently showing 82 above components that add to
    // 62 is the decomposition failing to decompose.
    render(
      <ConfidenceBreakdown diagnosis={{ ...DIAGNOSIS, confidence_breakdown: COMPONENTS }} />,
    );

    expect(screen.getByText(/capped or clamped/i)).toBeTruthy();
  });
});

describe("progress is the backend's, not the panel's", () => {
  const noop = () => undefined;

  it("shows no steps before any event has arrived", () => {
    render(
      <LiveTimeline phase="running" transport="stream" timeline={[]} onCancel={noop} />,
    );

    expect(screen.getByText(/no progress yet/i)).toBeTruthy();
  });

  it("renders exactly the events it was given, in order", () => {
    const timeline = [
      event({ type: "queued", message: "Queued", time: "10:00:00" }),
      event({ message: "Collecting pods", time: "10:00:01" }),
      event({ type: "completed", message: "Done", time: "10:00:02" }),
    ];
    const { container } = render(
      <LiveTimeline phase="succeeded" transport="stream" timeline={timeline} onCancel={noop} />,
    );

    const rows = container.querySelectorAll("li");
    expect(rows.length).toBe(3);
    expect([...rows].map((row) => row.textContent)).toEqual([
      "10:00:00Queued",
      "10:00:01Collecting pods",
      "10:00:02Done",
    ]);
  });

  it("marks the polling fallback, because a degraded transport must be visible", () => {
    const timeline = [event({})];
    const { rerender } = render(
      <LiveTimeline phase="running" transport="poll" timeline={timeline} onCancel={noop} />,
    );
    expect(screen.getByText("polling")).toBeTruthy();

    rerender(
      <LiveTimeline phase="running" transport="stream" timeline={timeline} onCancel={noop} />,
    );
    expect(screen.queryByText("polling")).toBeNull();
  });

  it("offers cancel only while there is something to cancel", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <LiveTimeline phase="running" transport="stream" timeline={[]} onCancel={onCancel} />,
    );
    expect(screen.getByRole("button", { name: /cancel/i })).toBeTruthy();

    rerender(
      <LiveTimeline phase="succeeded" transport="stream" timeline={[]} onCancel={onCancel} />,
    );
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });
});

describe("a reused read is visible, and an absent one says nothing", () => {
  it("reports reuse when the backend reported hits", () => {
    render(
      <EvidenceExplorer
        investigation={{
          evidence: [evidence()],
          collection_cache: {
            enabled: true,
            ttl_seconds: 60,
            hits: 57,
            misses: 13,
            oldest_evidence_seconds: 42,
          },
        }}
      />,
    );

    expect(screen.getByText(/57 of 70 reads reused/i)).toBeTruthy();
    // The age is the honest headline: it is what any conclusion here rests on.
    expect(screen.getByText(/42s old/i)).toBeTruthy();
  });

  it("says nothing at all on a fully live investigation", () => {
    // Not "0 reused" on every cold run — a section with nothing behind it is
    // omitted rather than padded, which is the same rule the reports follow.
    render(
      <EvidenceExplorer
        investigation={{
          evidence: [evidence()],
          collection_cache: {
            enabled: true,
            ttl_seconds: 60,
            hits: 0,
            misses: 70,
            oldest_evidence_seconds: null,
          },
        }}
      />,
    );

    expect(screen.queryByText(/reads reused/i)).toBeNull();
  });

  it("shows an empty state rather than an empty frame when nothing was collected", () => {
    render(<EvidenceExplorer investigation={{ evidence: [] }} />);
    expect(screen.getByText(/no evidence has been collected yet/i)).toBeTruthy();
  });
});
