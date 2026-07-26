# Evaluation

Signal rules, hypothesis rules, the prompt and the grounding checks can all be
changed without breaking a single unit test while quietly making the platform
worse at its job. This is the regression gate for that.

```bash
cd backend
python -m evals          # report
python -m pytest tests/test_evals.py   # the same corpus, as a CI gate
```

## What is measured

**Reasoning accuracy** — given a known investigation, does the deterministic
layer reach the right hypothesis? A regression here is usually a signal or
hypothesis rule that stopped firing, or a ranking change that promoted the wrong
candidate.

**Grounding behaviour** — is model output accepted when it should be, and
rejected when it should not? These fail in opposite directions, so both are
measured:

| Failure | How it shows up |
|---|---|
| Too lenient | A fabricated or contradictory diagnosis reaches an operator |
| **Too strict** | **Nothing fails. Every investigation silently routes to the deterministic fallback and the model is effectively off.** |

The second is the dangerous one, because nothing announces it. `false_rejections`
exists specifically to catch it, and the corpus is required to contain cases
that *must* be accepted — a corpus of only-rejections would pass while the model
path was dead.

## Why no model is called

The corpus runs on every pull request, without an API key, a bill, or
nondeterminism. What it measures is the part that must stay correct regardless of
which model is configured: what the deterministic layer concludes, and what
grounding will accept.

Evaluating an actual model — does *this* provider, at *this* temperature, produce
groundable output — is a different question and is not answered here. That needs
a live opt-in suite in the style of `frontend/src/services/http.integration.test.ts`,
and is tracked in `docs/PRODUCTION_READINESS.md`.

## Adding a case

Cases are JSON so a failure mode can be contributed without writing Python.

`evals/cases/investigations/<id>.json`:

```json
{
  "id": "oom-confirmed-by-exit-code",
  "description": "Exit code 137 confirms an OOM kill, which should refute the startup-failure reading.",
  "investigation": { "...": "an investigation payload" },
  "expect": {
    "top_hypothesis": "workload.out_of_memory",
    "hypotheses_absent": ["workload.application_startup_failure"],
    "signals_present": ["container.oom_exit_code:pod/prod/web-0"],
    "min_confidence": 70
  }
}
```

Every field of `expect` is optional. Omitting `top_hypothesis` asserts nothing
about it; setting it to `null` asserts there must be no hypothesis at all — the
healthy-cluster case relies on that distinction.

`evals/cases/grounding/<id>.json` pairs a model response with the verdict
grounding should reach, referencing an investigation case by id:

```json
{
  "id": "reject-contradicts-evidence",
  "investigation_case": "crashloop-missing-configmap-key",
  "expect_valid": false,
  "expect_reason_contains": "severe signal",
  "response": { "root_cause": "Resolved - no action needed.", "cited_signals": ["..."] }
}
```

**When you fix a bug in the reasoning layer, add the case that would have caught
it.** That is what stops the corpus decaying into a set of cases that only ever
described how the code already behaved.

## Reading a failure

The report names what changed about the conclusion, not just that an assertion
failed:

```
  FAIL oom-confirmed-by-exit-code
    - expected top hypothesis 'workload.out_of_memory', got
      'workload.application_startup_failure' (ranked: workload.application_startup_failure)
    - confidence 35 below expected minimum 70
```

That is the diagnostic difference between this and the unit tests: it tells you
the platform got *worse at reasoning*, and how.
