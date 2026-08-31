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

Evaluating an actual model — does *this* provider, at *this* model, produce
groundable output — is a different question, and it is answered by
`python -m evals.live`.

## Scoring a real model

```bash
OPENAI_API_KEY=... python -m evals.live
ANTHROPIC_API_KEY=... LLM_PROVIDER=anthropic python -m evals.live --min-grounded 0.8
```

Same corpus, same ground truth — not a second, friendlier set of cases, which
would measure the corpus. What it adds is the number the offline run cannot
see: **of the cases where the model actually answered, how many survived
grounding.**

That is the failure this document already names two sections above. An
over-strict grounding check does not fail loudly; every investigation quietly
routes to the deterministic fallback, `ai_generated` goes false everywhere, and
the reasoning layer is off while 20/20 golden cases still pass. A prompt edit
that degrades a real model's answers has exactly the same signature. Nothing in
CI could see either, because nothing in CI had ever called a model.

**Agreement with the deterministic ranking is reported and not gated.** The
model is asked to select and explain; a defensible disagreement is not a
defect, and the fallback exists precisely because the two can differ. Gating it
would train people to edit the corpus instead of the prompt.

### It cannot pass without calling anything

The easiest live suite to write is one that skips when the key is absent and
goes green — which is indistinguishable from one that ran. So:

- **No configured model is exit 2, never exit 0.** The workflow decides whether
  the job *runs*, on the secret being present; this program decides whether it
  *passed*, and it will not say yes to nothing.
- **A floor on how many cases reached the model.** Every call failing produces
  zero grounding rejections, which reads as a clean sheet. It is refused
  instead.
- **The guards are unit-tested against a local HTTP stub** speaking the
  chat-completions shape (`tests/test_live_evals.py`), reached through
  `LLM_BASE_URL` — a real provider, real httpx, real parsing, real grounding.
  So the gate itself is exercised on every CI run, key or no key, and a broken
  gate fails even when there is no model to score.

Two details are load-bearing, and both were wrong first:

- **"The model answered" is read at the call, not from the diagnosis.** A
  failed call and a rejected answer both come back `ai_generated: false`, and
  both carry the *deterministic fallback's own* grounding block — valid, reason
  "no model output was used". They need opposite responses: one is an outage,
  the other the regression this exists to catch. The first version inferred
  both from the payload and scored a total provider outage as twenty perfectly
  grounded answers.
- **The rejection reason is read at the validator, for the same reason.** The
  real reason is logged and turned into a metric category, and then it is gone;
  the diagnosis never carries it.

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
