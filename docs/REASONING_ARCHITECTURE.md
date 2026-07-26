# Reasoning Architecture

How the platform reaches a conclusion, and why that conclusion can be trusted.

This layer sits on top of the evidence foundation described in
[EVIDENCE_ARCHITECTURE.md](EVIDENCE_ARCHITECTURE.md).

## The pipeline

```
Evidence  →  Signals  →  Hypotheses  →  Model selects & explains  →  Grounding check  →  Diagnosis
(collected)  (rules)     (rules)         (may be rejected)           (enforced)
```

Everything left of the model call is deterministic. The model's role is narrow
by construction: it selects among hypotheses that were already derived from
evidence, and explains the choice. It cannot introduce a cause that no rule
proposed, and it cannot cite an observation that no collector made.

## Signals

A signal is an evidence-backed observation extracted by a rule.

```python
Signal.create(
    SignalType.POD_CRASH_LOOP,
    Severity.CRITICAL,
    "Pod prod/web-0 is in CrashLoopBackOff.",
    ResourceRef(kind="Pod", name="web-0", namespace="prod"),
    evidence_ids=("k8s.pods:cluster/_cluster/prod-east",),
)
# id == "pod.crash_loop:pod/prod/web-0"
```

Two properties are enforced rather than encouraged:

- **Provenance is mandatory.** `Signal.__post_init__` raises if `evidence_ids`
  is empty. A signal that cannot name where it came from is a defect.
- **Ids are deterministic.** The same observation always produces the same id,
  so duplicate extraction is idempotent and a diagnosis can reference a signal
  stably.

Signals are never produced by a language model.

## Hypotheses

Hypothesis rules are data, not control flow:

```python
SignalPatternRule(
    id="workload.out_of_memory",
    title="Container terminated for exceeding its memory limit",
    triggers=frozenset({SignalType.POD_OOM_KILLED, SignalType.LOGS_OOM_PATTERN}),
    supporting=frozenset({SignalType.CONTAINER_MISSING_LIMITS, SignalType.POD_CRASH_LOOP}),
    missing_evidence=(
        "Container memory limit and request values",
        "Container exit code (137 confirms an OOM kill)",
    ),
    base_confidence=65,
)
```

- `triggers` — any one of these signal types instantiates the hypothesis.
- `supporting` — each distinct type present adds 10 confidence.
- `refuting` — each distinct type present subtracts 20.
- `missing_evidence` — what would confirm or refute it.

`missing_evidence` is not documentation. It flows into the diagnosis as
`evidence_gaps`, and it is the specification for deeper collection: the gaps a
crash-loop hypothesis reports today (previous-container logs, probe definitions,
referenced ConfigMap and Secret keys, exit codes) are exactly the evidence a
crash-loop playbook should collect.

Ranking is by severity, then confidence, then breadth of support.

## Grounding: the enforcement

Telling a model not to invent facts is unenforceable. Refusing output that
references things which do not exist is enforceable.

`GroundingValidator` checks every response against the deterministic analysis:

| Condition | Outcome |
|---|---|
| Fabricated hypothesis id | **Reject** — structural error |
| Some fabricated signal ids | Strip them, record in `rejected_citations` |
| Signals existed, no valid citation survives | **Reject** — conclusion is uncitable |
| Empty root cause | **Reject** |
| No signals at all (healthy cluster) | Citations not required |

A rejected response is discarded and the deterministic ranking stands. The
outcome is always recorded in the diagnosis under `grounding`, so a reviewer can
see whether the model was used, and what it tried to claim.

This is verified by mutation: disabling the validator causes exactly the
hallucination-rejection tests to fail.

## Confidence

Confidence combines three independent inputs rather than trusting any one:

| Path | Evidence strength | AI confidence | Evidence completeness |
|---|---|---|---|
| Grounded model answer | 0.50 | 0.30 | 0.20 |
| Deterministic | 0.70 | — | 0.30 (capped at 95) |

Evidence strength comes from the top hypothesis's support. Completeness comes
from `EvidenceStore.coverage()`. The weighting means a confident-sounding model
answer over half-collected evidence cannot outrank a well-supported
deterministic one — which is the failure mode that matters in production.

`confidence_breakdown` exposes each component's weight, score, and contribution,
so a stated confidence can always be decomposed.

## Diagnosis output

All pre-existing keys are unchanged. Added, additive:

| Key | Meaning |
|---|---|
| `signals` | Every extracted signal with provenance |
| `hypotheses` | Ranked candidates with support and gaps |
| `selected_hypothesis` | Chosen hypothesis id, or null |
| `cited_signals` | Validated signal citations |
| `cited_evidence` | Evidence ids behind those signals |
| `confidence_breakdown` | Weighted scoring components |
| `grounding` | Validation outcome and rejected citations |

`cited_signals → cited_evidence → executed command` is a complete audit chain
from conclusion back to the command that produced the underlying fact.

## Adding a failure mode

1. Add a `SignalType` constant.
2. Add or extend a rule in `signal_rules.py` to emit it, passing the evidence ids.
3. Append a `SignalPatternRule` to `DEFAULT_HYPOTHESIS_RULES`.

No orchestrator, dispatcher, or prompt changes are needed — the prompt is
generated from whatever signals and hypotheses exist.
