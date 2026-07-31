import { describe, expect, it } from "vitest";

import {
  citationsFor,
  citationsForSignal,
  citedBy,
  evidenceIndex,
  isCommandLine,
  isGap,
  severityTone,
} from "./report";
import type { Diagnosis, EvidenceEntry, InvestigationResponse } from "../types/investigation";

const record = (id: string, status = "ok"): EvidenceEntry =>
  ({ id, kind: "k8s.pods", status, command: "kubectl get pods" }) as EvidenceEntry;

const DIAGNOSIS = {
  root_cause: "Memory limit too low",
  cited_evidence: ["k8s.pods.logs:pod/a", "k8s.events:ns/payments"],
  signals: [
    {
      id: "pod.oom:pod/a",
      summary: "Container OOMKilled",
      severity: "critical",
      evidence_ids: ["k8s.pods.logs:pod/a"],
    },
    {
      id: "pod.restart:pod/a",
      summary: "14 restarts",
      severity: "high",
      evidence_ids: [],
    },
  ],
} as unknown as Diagnosis;

describe("severity tone", () => {
  it("maps the vocabulary the backend actually emits", () => {
    expect(severityTone("Critical")).toBe("critical");
    expect(severityTone("high")).toBe("critical");
    expect(severityTone("Warning")).toBe("warning");
    expect(severityTone("unavailable")).toBe("warning");
    expect(severityTone("Healthy")).toBe("healthy");
    expect(severityTone("ok")).toBe("healthy");
  });

  it("falls back to neutral rather than guessing", () => {
    expect(severityTone("something new")).toBe("neutral");
    expect(severityTone(undefined)).toBe("neutral");
  });
});

describe("gaps", () => {
  it("counts evidence that could not be collected", () => {
    expect(isGap("unavailable")).toBe(true);
    expect(isGap("forbidden")).toBe(true);
    expect(isGap("timeout")).toBe(true);
    expect(isGap("failed")).toBe(true);
  });

  it("does not count evidence that was collected", () => {
    expect(isGap("ok")).toBe(false);
    expect(isGap("empty")).toBe(false);
  });

  it("does not count what never applied", () => {
    // `not_applicable` means the record did not apply to this cluster and is
    // excluded from the coverage ratio. Treating it as a gap would make an
    // undeployed Prometheus look like a failure to look.
    expect(isGap("not_applicable")).toBe(false);
  });
});

describe("citations", () => {
  it("takes the ids the diagnosis actually rested on", () => {
    expect(citationsFor(DIAGNOSIS)).toEqual([
      "k8s.pods.logs:pod/a",
      "k8s.events:ns/payments",
    ]);
  });

  it("returns nothing when a conclusion cited nothing", () => {
    // The absence is informative: the UI renders no chip rather than inventing
    // a plausible-looking one.
    expect(citationsFor({ root_cause: "x" } as Diagnosis)).toEqual([]);
    expect(citationsFor(undefined)).toEqual([]);
  });

  it("finds the evidence behind one signal", () => {
    expect(citationsForSignal(DIAGNOSIS, "pod.oom:pod/a")).toEqual(["k8s.pods.logs:pod/a"]);
    expect(citationsForSignal(DIAGNOSIS, "pod.restart:pod/a")).toEqual([]);
    expect(citationsForSignal(DIAGNOSIS, "does.not.exist")).toEqual([]);
  });
});

describe("what rests on a record", () => {
  it("names the root cause when it cited the record", () => {
    expect(citedBy(DIAGNOSIS, "k8s.events:ns/payments")).toEqual(["Root cause"]);
  });

  it("names every signal that cited it", () => {
    expect(citedBy(DIAGNOSIS, "k8s.pods.logs:pod/a")).toEqual([
      "Root cause",
      "Container OOMKilled",
    ]);
  });

  it("is empty for a record nothing rested on", () => {
    expect(citedBy(DIAGNOSIS, "k8s.nodes:cluster")).toEqual([]);
  });
});

describe("evidence index", () => {
  it("keys records by id", () => {
    const investigation = {
      evidence: [record("a"), record("b")],
    } as unknown as InvestigationResponse["investigation"];

    const index = evidenceIndex(investigation);
    expect(index.get("a")?.kind).toBe("k8s.pods");
    expect(index.size).toBe(2);
  });

  it("survives an investigation with no evidence", () => {
    expect(evidenceIndex(undefined).size).toBe(0);
    expect(evidenceIndex({} as InvestigationResponse["investigation"]).size).toBe(0);
  });
});

describe("command lines", () => {
  it("recognises what an operator will copy", () => {
    expect(isCommandLine("kubectl get pods -A")).toBe(true);
    expect(isCommandLine("  $ kubectl logs web-0")).toBe(true);
  });

  it("leaves prose as prose", () => {
    expect(isCommandLine("The container was OOMKilled.")).toBe(false);
    expect(isCommandLine("Run kubectl to check")).toBe(false);
  });
});
