import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EvidenceInspector } from "./EvidenceInspector";
import type { EvidenceEntry } from "../../types/investigation";

const record = (redacted: boolean) =>
  ({
    id: "k8s.nodes:cluster/_cluster/kind-k8s-agent-dev",
    kind: "k8s.nodes",
    status: "ok",
    source: "kubectl",
    redacted,
  }) as unknown as EvidenceEntry;

describe("the redaction note", () => {
  it("does not claim a record held secrets, or that pattern matching caught them all", () => {
    // `redacted` is set on every record that went through the redactor. The
    // note read "Secrets were scrubbed from this record" on a node list.
    render(<EvidenceInspector evidence={record(true)} onClose={() => {}} />);

    const note = screen.getByText(/redacted at collection/i);
    expect(note.textContent).toMatch(/may not catch everything/i);
    expect(note.textContent).not.toMatch(/secrets were scrubbed/i);
  });

  it("says nothing about redaction for a record that did not go through it", () => {
    render(<EvidenceInspector evidence={record(false)} onClose={() => {}} />);
    expect(screen.queryByText(/redacted at collection/i)).not.toBeInTheDocument();
  });
});
