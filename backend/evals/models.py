"""Evaluation case and result types.

Two things are measured, because they fail in different directions:

- **Reasoning accuracy** — does the deterministic layer reach the right
  hypothesis from a known investigation? Regressions here are usually a signal
  or hypothesis rule that stopped firing.
- **Grounding behaviour** — is model output accepted when it should be and
  rejected when it should not? Regressions here are usually silent: an
  over-strict check routes every investigation to the fallback without failing
  anything.
"""

from dataclasses import dataclass, field
from typing import Any

UNSET = "<unset>"


@dataclass(frozen=True)
class Expectation:
    # `UNSET` means the case does not assert a top hypothesis; explicit `null`
    # means it asserts there must not be one. Collapsing those made every case
    # that only listed `hypotheses_present` demand an empty result.
    top_hypothesis: str | None = UNSET
    hypotheses_present: tuple[str, ...] = ()
    hypotheses_absent: tuple[str, ...] = ()
    signals_present: tuple[str, ...] = ()
    signals_absent: tuple[str, ...] = ()
    min_confidence: int = 0
    max_confidence: int = 100

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Expectation":
        return cls(
            # `.get` returns the stored value when the key exists, so an explicit
            # `null` stays distinct from an absent key.
            top_hypothesis=payload.get("top_hypothesis", UNSET),
            hypotheses_present=tuple(payload.get("hypotheses_present", ())),
            hypotheses_absent=tuple(payload.get("hypotheses_absent", ())),
            signals_present=tuple(payload.get("signals_present", ())),
            signals_absent=tuple(payload.get("signals_absent", ())),
            min_confidence=int(payload.get("min_confidence", 0)),
            max_confidence=int(payload.get("max_confidence", 100)),
        )


@dataclass(frozen=True)
class InvestigationCase:
    """A known investigation with the conclusion it should reach."""

    id: str
    description: str
    investigation: dict[str, Any]
    expect: Expectation

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InvestigationCase":
        return cls(
            id=payload["id"],
            description=payload.get("description", ""),
            investigation=payload["investigation"],
            expect=Expectation.from_dict(payload.get("expect", {})),
        )


@dataclass(frozen=True)
class GroundingCase:
    """A model response with the verdict grounding should reach on it."""

    id: str
    description: str
    investigation_case: str
    response: dict[str, Any]
    expect_valid: bool
    expect_reason_contains: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GroundingCase":
        return cls(
            id=payload["id"],
            description=payload.get("description", ""),
            investigation_case=payload["investigation_case"],
            response=payload["response"],
            expect_valid=bool(payload["expect_valid"]),
            expect_reason_contains=payload.get("expect_reason_contains", ""),
        )


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    investigations: list[CaseResult] = field(default_factory=list)
    grounding: list[CaseResult] = field(default_factory=list)

    @property
    def hypothesis_accuracy(self) -> float:
        return _ratio(sum(1 for r in self.investigations if r.passed), len(self.investigations))

    @property
    def grounding_accuracy(self) -> float:
        return _ratio(sum(1 for r in self.grounding if r.passed), len(self.grounding))

    def false_rejections(self, cases: list[GroundingCase]) -> list[str]:
        """Valid responses that grounding rejected.

        This is the metric that matters most: a false rejection silently
        disables the model path rather than failing anything.
        """
        wanted = {case.id for case in cases if case.expect_valid}
        return [r.case_id for r in self.grounding if r.case_id in wanted and not r.passed]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in (*self.investigations, *self.grounding))

    def summary(self) -> str:
        lines = [
            f"Golden investigations : {_count(self.investigations)}  "
            f"({self.hypothesis_accuracy:.0%} correct)",
            f"Grounding corpus      : {_count(self.grounding)}  "
            f"({self.grounding_accuracy:.0%} correct)",
        ]
        for result in (*self.investigations, *self.grounding):
            if result.passed:
                continue
            lines.append(f"\n  FAIL {result.case_id}")
            lines.extend(f"    - {failure}" for failure in result.failures)
        return "\n".join(lines)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _count(results: list[CaseResult]) -> str:
    return f"{sum(1 for r in results if r.passed)}/{len(results)}"
