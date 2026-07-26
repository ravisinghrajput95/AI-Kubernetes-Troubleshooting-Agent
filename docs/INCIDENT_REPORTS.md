# Incident Reports

One composition, three renderings.

## Why a composition layer

The PDF, Markdown and JSON writers each used to assemble their own view of an
investigation. That is how two reports about the same incident end up
disagreeing, and it meant every new field had to be wired into three places.

`IncidentReportComposer` builds a structured `IncidentReport`; the writers
render it. Adding a section is one change, and the formats cannot drift.

```
investigation + diagnosis  →  IncidentReportComposer  →  IncidentReport
                                                            ├── PDF
                                                            ├── Markdown
                                                            └── JSON (`report` key)
```

## Outline

| Section | Answers |
|---|---|
| Executive Summary | What happened, how confident, on what evidence |
| Impact | What was affected, and what remediation would affect |
| Investigation Timeline | What the platform did, and when |
| Root Cause | Why — with the alternatives and what refuted them |
| Evidence | What was observed, what was cited, what was missing |
| Confidence Assessment | How the number was composed |
| Resolution | The reviewed change, its risk, and rollback |
| Verification | How to confirm it worked, and the access needed |
| Lessons Learned | What would have shortened this investigation |
| Preventive Actions | What to change so it does not recur |
| Appendix | Every command the platform ran |

## Sections are omitted, never padded

A section with nothing behind it is dropped. An investigation with no
remediation plan produces no Resolution section rather than a heading over
filler.

This is the same rule the console follows: an incident report containing
invented content is worse than a short one, because it is the artifact people
trust after the incident is over.

## Details worth keeping

**Root Cause names the alternatives.** The report lists competing hypotheses
with their confidence, marks the selected one, and states which the evidence
argued *against*. A report that only presents the winning theory hides the
reasoning that makes it credible.

**Evidence separates "did not apply" from "failed to collect".** The report
states how many records applied, how many were collected, and lists each gap
with its reason — including optional backends that were not deployed.

**Confidence is decomposed.** Each weighted component appears with its score and
contribution, and any citations rejected from the model response are named.

**Resolution states it was not applied.** Every rendering of a remediation plan
carries the note that no change was made by the platform.

**The Appendix states that every command was read-only.** This is the audit
trail; it is worth saying plainly what it does and does not contain.

**Operational noise is filtered.** The analyzer appends things like
`OpenAI status: …` to its explanation for the console. The composer strips that
from the Root Cause — the Executive Summary already reports the diagnosis
source, and an executive-facing document should not carry an API-key message.

## Rendering notes

The PDF is still emitted by the hand-rolled writer in `history_service.py`
(base-14 fonts, no PDF dependency), so section bodies are flattened to lines via
`ReportSection.as_lines()`. Tables carry `headers`, which Markdown renders
properly and the PDF flattens.

The JSON report gains a `report` key holding the full composition, so a consumer
gets the structured document rather than having to rebuild it from `diagnosis`
and `investigation`.

`POST /investigations/{id}/regenerate` re-renders all three from the stored JSON
without re-querying the cluster, so improving the composer improves every
historical report.
