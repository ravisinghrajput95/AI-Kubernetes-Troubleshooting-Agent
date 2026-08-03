"""Runs the evaluation corpus against the reasoning pipeline.

Deliberately does not call a language model. Prompt and rule changes have to be
measurable in CI, on every pull request, without an API key, a bill, or
nondeterminism. What is measured is the part that must stay correct regardless
of which model is configured: which hypothesis the deterministic layer reaches,
and which model responses grounding accepts.

Evaluating an actual model is a separate, opt-in concern — see EVALUATION.md.
"""

import json
from pathlib import Path

from app.analysis.engine import AnalysisEngine
from app.analysis.grounding import GroundingValidator
from app.analysis.models import AnalysisResult
from evals.models import (
    UNSET,
    CaseResult,
    EvalReport,
    GroundingCase,
    InvestigationCase,
)

CASES_DIR = Path(__file__).parent / "cases"


def load_investigation_cases(directory: Path | None = None) -> list[InvestigationCase]:
    directory = directory or CASES_DIR / "investigations"
    return [
        InvestigationCase.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]


def load_grounding_cases(directory: Path | None = None) -> list[GroundingCase]:
    directory = directory or CASES_DIR / "grounding"
    return [
        GroundingCase.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]


class EvalRunner:
    def __init__(
        self,
        engine: AnalysisEngine | None = None,
        validator: GroundingValidator | None = None,
    ) -> None:
        self.engine = engine or AnalysisEngine()
        self.validator = validator or GroundingValidator()

    def run(
        self,
        investigations: list[InvestigationCase] | None = None,
        grounding: list[GroundingCase] | None = None,
    ) -> EvalReport:
        investigations = (
            investigations if investigations is not None else load_investigation_cases()
        )
        grounding = grounding if grounding is not None else load_grounding_cases()

        analyses = {case.id: self.engine.analyze(case.investigation) for case in investigations}

        return EvalReport(
            investigations=[
                self._score_investigation(case, analyses[case.id]) for case in investigations
            ],
            grounding=[self._score_grounding(case, analyses) for case in grounding],
        )

    def _score_investigation(
        self,
        case: InvestigationCase,
        analysis: AnalysisResult,
    ) -> CaseResult:
        failures: list[str] = []
        expect = case.expect
        top = analysis.top_hypothesis
        hypothesis_ids = {item.id for item in analysis.hypotheses}
        signal_ids = analysis.signal_ids

        if expect.top_hypothesis is UNSET:
            pass
        elif expect.top_hypothesis is None:
            if top is not None:
                failures.append(f"expected no hypothesis, got '{top.id}'")
        elif top is None:
            failures.append(f"expected top hypothesis '{expect.top_hypothesis}', got none")
        elif top.id != expect.top_hypothesis:
            failures.append(
                f"expected top hypothesis '{expect.top_hypothesis}', got '{top.id}' "
                f"(ranked: {', '.join(item.id for item in analysis.hypotheses[:3])})"
            )

        failures.extend(
            f"expected hypothesis '{item}' to be present"
            for item in expect.hypotheses_present
            if item not in hypothesis_ids
        )
        failures.extend(
            f"hypothesis '{item}' should not have fired"
            for item in expect.hypotheses_absent
            if item in hypothesis_ids
        )
        failures.extend(
            f"expected signal '{item}'" for item in expect.signals_present if item not in signal_ids
        )
        failures.extend(
            f"signal '{item}' should not have fired"
            for item in expect.signals_absent
            if item in signal_ids
        )

        if top is not None:
            if top.confidence < expect.min_confidence:
                failures.append(
                    f"confidence {top.confidence} below expected minimum {expect.min_confidence}"
                )
            if top.confidence > expect.max_confidence:
                failures.append(
                    f"confidence {top.confidence} above expected maximum {expect.max_confidence}"
                )

        # Counted before the pass/fail verdict, so a case that fails on
        # confidence still reports the detections it did make.
        expected_detections = (
            len(expect.hypotheses_present)
            + len(expect.signals_present)
            + (1 if expect.top_hypothesis not in (UNSET, None) else 0)
        )
        found_detections = (
            sum(1 for item in expect.hypotheses_present if item in hypothesis_ids)
            + sum(1 for item in expect.signals_present if item in signal_ids)
            + (
                1
                if expect.top_hypothesis not in (UNSET, None)
                and top is not None
                and top.id == expect.top_hypothesis
                else 0
            )
        )

        return CaseResult(
            case_id=case.id,
            passed=not failures,
            failures=failures,
            expected_detections=expected_detections,
            found_detections=found_detections,
            detail={
                "top_hypothesis": top.id if top else None,
                "confidence": top.confidence if top else None,
                "signal_count": len(analysis.signals),
            },
        )

    def _score_grounding(
        self,
        case: GroundingCase,
        analyses: dict[str, AnalysisResult],
    ) -> CaseResult:
        analysis = analyses.get(case.investigation_case)
        if analysis is None:
            return CaseResult(
                case_id=case.id,
                passed=False,
                failures=[f"unknown investigation case '{case.investigation_case}'"],
            )

        result = self.validator.validate(case.response, analysis)
        failures: list[str] = []

        if result.valid != case.expect_valid:
            verb = "accepted" if result.valid else "rejected"
            expected = "accept" if case.expect_valid else "reject"
            failures.append(f"grounding {verb} the response; expected it to {expected}")
            if result.reason:
                failures.append(f"reason given: {result.reason}")

        if (
            case.expect_reason_contains
            and case.expect_reason_contains.lower() not in result.reason.lower()
        ):
            failures.append(
                f"expected reason to mention '{case.expect_reason_contains}', got '{result.reason}'"
            )

        return CaseResult(
            case_id=case.id,
            passed=not failures,
            failures=failures,
            detail={"valid": result.valid, "reason": result.reason},
        )
