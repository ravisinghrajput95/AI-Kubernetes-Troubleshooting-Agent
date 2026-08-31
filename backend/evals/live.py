"""Run the golden corpus against a **real** model.

`python -m evals` measures the half of reasoning that must hold regardless of
which model is configured: what the deterministic layer concludes, and what
grounding accepts. It calls no model, so it runs on every pull request without
a key or a bill.

This answers the other half, and it is a question the deterministic corpus
cannot even see:

    **How often does the configured model produce an answer grounding will
    accept?**

`docs/EVALUATION.md` names the failure mode already: an over-strict grounding
check *does not fail loudly*. Every investigation quietly routes to the
deterministic fallback, `ai_generated` goes to false everywhere, and the whole
reasoning layer is off while 20/20 golden cases still pass. Nothing in CI could
notice, because nothing in CI ever called a model. A prompt edit that degrades
a real model's answers has exactly the same signature.

So the number this reports is the **grounded rate**: of the cases where the
model actually answered, how many survived grounding. A floor on it is the
gate.

    OPENAI_API_KEY=... python -m evals.live
    ANTHROPIC_API_KEY=... LLM_PROVIDER=anthropic python -m evals.live --min-grounded 0.8

## It must not be able to pass without calling anything

Every harness in this repository that could report success without doing its
work eventually did. So:

- **No key is exit 2, never exit 0.** A CI job that runs this without a
  configured provider fails rather than going green on a skip. Whether the job
  *runs* is the workflow's decision, gated on the secret; whether it *passed*
  is this program's, and it will not say yes to nothing.
- **A floor on the number of cases that reached the model.** Every call
  failing — a wrong key, a rate limit, an outage — is not a passing run with
  zero rejections. It is refused.
- **The corpus is the same one.** Not a second set of cases written to be easy;
  the same files `python -m evals` scores, so a regression in either shows up
  against the same ground truth.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

from evals.runner import load_investigation_cases

# The corpus is small and every case is one model call. A floor well below its
# size still refuses a run where nothing worked, which is the property that
# matters; it is not an attempt to require every case to succeed, because a
# single provider hiccup should not fail a build.
MIN_ANSWERED_FRACTION = 0.8


@dataclass
class LiveCase:
    id: str
    answered: bool = False
    grounded: bool = False
    agreed: bool = False
    expected: str | None = None
    selected: str | None = None
    rejection: str = ""
    seconds: float = 0.0


@dataclass
class LiveReport:
    provider: str
    model: str
    cases: list[LiveCase] = field(default_factory=list)

    @property
    def answered(self) -> list[LiveCase]:
        return [case for case in self.cases if case.answered]

    @property
    def grounded(self) -> list[LiveCase]:
        return [case for case in self.cases if case.grounded]

    @property
    def grounded_rate(self) -> float:
        answered = self.answered
        return len(self.grounded) / len(answered) if answered else 0.0

    @property
    def agreement_rate(self) -> float:
        """Of the grounded answers, how many chose the deterministic layer's pick.

        Reported, **not gated**. The model is asked to select and explain, and a
        defensible disagreement is not a defect — the fallback exists precisely
        because the two can differ. A drop here is a prompt smell worth reading,
        not a build break, and treating it as one would train people to edit the
        corpus rather than the prompt.
        """
        with_expectation = [case for case in self.grounded if case.expected]
        if not with_expectation:
            return 0.0
        return sum(case.agreed for case in with_expectation) / len(with_expectation)

    def summary(self) -> str:
        lines = [
            f"Provider              : {self.provider} ({self.model})",
            f"Cases                 : {len(self.cases)}",
            f"Answered by the model : {len(self.answered)}/{len(self.cases)}",
            f"Survived grounding    : {len(self.grounded)}/{len(self.answered)}"
            f"  ({100 * self.grounded_rate:.0f}%)",
            f"Agreed with the rules : {100 * self.agreement_rate:.0f}%  (reported, not gated)",
        ]
        rejections = Counter(case.rejection for case in self.answered if not case.grounded)
        if rejections:
            lines.append("Grounding rejections:")
            lines += [f"  {count:>3}x  {reason}" for reason, count in rejections.most_common()]
        unanswered = [case for case in self.cases if not case.answered]
        if unanswered:
            lines.append(f"No answer for: {', '.join(case.id for case in unanswered)}")
        return "\n".join(lines)


class _Observed:
    """The two seams the score has to be read from, because the diagnosis
    cannot supply either.

    A rejected diagnosis and a failed call are indistinguishable in the result:
    both come back `ai_generated: false`, and both carry the *deterministic
    fallback's own* grounding block — valid, with the reason "no model output
    was used". The model's actual rejection reason is logged and turned into a
    metric category, and then it is gone.

    They need opposite responses, though. One is an outage; the other is the
    reasoning regression this whole program exists to catch. So the call is
    observed where it is made and the verdict where it is reached, and neither
    is inferred from the payload. The first version of this inferred both, and
    scored a provider outage as twenty perfectly grounded answers.
    """

    def __init__(self, client, validator) -> None:
        self._client = client
        self._validator = validator
        self.answered = False
        self.error = ""
        self.verdict = None

    # -- the LLMClient half --------------------------------------------------
    def complete(self, messages):
        completion = self._client.complete(messages)
        self.answered = bool(completion.success)
        self.error = "" if completion.success else str(completion.error or "the call failed")
        return completion

    # -- the GroundingValidator half -----------------------------------------
    def validate(self, response, analysis):
        self.verdict = self._validator.validate(response, analysis)
        return self.verdict

    def reset(self) -> None:
        self.answered = False
        self.error = ""
        self.verdict = None


def run(limit: int = 0) -> LiveReport:
    from app.ai.llm_client import LLMClient
    from app.ai.providers.factory import resolve_name
    from app.ai.root_cause_analyzer import RootCauseAnalyzer
    from app.core.config import settings

    cases = load_investigation_cases()
    if limit:
        cases = cases[:limit]

    analyzer = RootCauseAnalyzer()
    observed = _Observed(LLMClient(), analyzer.validator)
    analyzer.llm_client = observed
    analyzer.validator = observed

    provider = resolve_name(settings)
    model = settings.anthropic_model if provider == "anthropic" else settings.openai_model
    report = LiveReport(provider=provider, model=model)

    for case in cases:
        result = LiveCase(id=case.id, expected=case.expect.top_hypothesis or None)
        observed.reset()
        started = time.perf_counter()
        diagnosis = analyzer.analyze(case.investigation)
        result.seconds = time.perf_counter() - started

        result.answered = observed.answered
        result.grounded = bool(diagnosis.get("ai_generated"))
        if not result.answered:
            result.rejection = observed.error
        elif not result.grounded:
            verdict = observed.verdict
            result.rejection = (
                str(verdict.reason)
                if verdict is not None and verdict.reason
                else "the response could not be parsed as JSON"
            )
        result.selected = diagnosis.get("selected_hypothesis")
        result.agreed = bool(result.expected) and result.selected == result.expected
        report.cases.append(result)

    return report


def configured() -> tuple[bool, str]:
    """Whether a model can actually be called, and why not when it cannot."""
    from app.ai.providers.factory import resolve_name
    from app.core.config import settings

    name = resolve_name(settings)
    if name == "anthropic" and settings.anthropic_api_key:
        return True, ""
    if name in ("openai", "compatible") and (settings.openai_api_key or settings.llm_base_url):
        return True, ""
    return False, (
        "No model is configured. Set OPENAI_API_KEY, or ANTHROPIC_API_KEY with "
        "LLM_PROVIDER=anthropic, or LLM_BASE_URL for an OpenAI-compatible endpoint."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a real model against the golden corpus.")
    parser.add_argument(
        "--min-grounded",
        type=float,
        default=0.8,
        help="Fail below this share of answered cases surviving grounding.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Score only the first N cases.")
    args = parser.parse_args(argv)

    ready, why = configured()
    if not ready:
        # Exit 2, deliberately. A live suite that exits 0 when it called nothing
        # is indistinguishable from one that ran and passed, and that is the
        # shape of every quiet harness failure this project has had.
        print(f"REFUSED: {why}", file=sys.stderr)
        return 2

    report = run(limit=args.limit)
    print(report.summary())

    if not report.cases:
        print("\nREFUSED: the corpus is empty.", file=sys.stderr)
        return 2

    answered = len(report.answered) / len(report.cases)
    if answered < MIN_ANSWERED_FRACTION:
        print(
            f"\nREFUSED: only {len(report.answered)} of {len(report.cases)} cases reached the "
            f"model. That is a broken key, a rate limit or an outage — not a result.",
            file=sys.stderr,
        )
        return 2

    if report.grounded_rate < args.min_grounded:
        print(
            f"\nFAILED: {100 * report.grounded_rate:.0f}% of answers survived grounding, "
            f"below the {100 * args.min_grounded:.0f}% floor.\n"
            f"Either the prompt has drifted or grounding has tightened. Both route real "
            f"investigations to the deterministic fallback, and neither fails any other check.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
