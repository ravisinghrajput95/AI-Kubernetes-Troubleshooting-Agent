"""Composite confidence scoring.

Confidence combines three independent inputs rather than a single number:

- deterministic evidence strength (how well the signals support a hypothesis)
- the model's own stated confidence, when a grounded model answer was accepted
- evidence completeness (how much of the intended evidence was actually collected)

A confident-sounding model answer over half-collected evidence should not
outrank a well-supported deterministic one, and this weighting is what enforces
that.
"""

from dataclasses import dataclass
from typing import Any

from app.analysis.models import AnalysisResult

DETERMINISTIC_CAP = 95

AI_WEIGHTS = {"evidence": 0.5, "ai": 0.3, "completeness": 0.2}
DETERMINISTIC_WEIGHTS = {"evidence": 0.7, "completeness": 0.3}

HEALTHY_EVIDENCE_SCORE = 70
UNSUPPORTED_EVIDENCE_SCORE = 30


@dataclass(frozen=True)
class ConfidenceComponent:
    name: str
    weight: float
    score: int
    detail: str

    @property
    def contribution(self) -> int:
        return round(self.weight * self.score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.name,
            "weight": round(self.weight * 100),
            "score": self.score,
            "contribution": self.contribution,
            "detail": self.detail,
        }


class CompositeConfidenceScorer:
    def score(
        self,
        analysis: AnalysisResult,
        completeness: int,
        healthy: bool,
        ai_confidence: int | None = None,
        deterministic_score: int | None = None,
    ) -> tuple[int, list[ConfidenceComponent]]:
        evidence_score, evidence_detail = self._evidence_score(
            analysis, healthy, deterministic_score
        )
        completeness = max(0, min(int(completeness), 100))

        if ai_confidence is None:
            components = [
                ConfidenceComponent(
                    "Evidence Strength",
                    DETERMINISTIC_WEIGHTS["evidence"],
                    evidence_score,
                    evidence_detail,
                ),
                ConfidenceComponent(
                    "Evidence Completeness",
                    DETERMINISTIC_WEIGHTS["completeness"],
                    completeness,
                    f"{completeness}% of attempted evidence was collected successfully.",
                ),
            ]
            total = min(sum(item.contribution for item in components), DETERMINISTIC_CAP)
            return total, components

        ai_confidence = max(0, min(int(ai_confidence), 100))
        components = [
            ConfidenceComponent(
                "Evidence Strength",
                AI_WEIGHTS["evidence"],
                evidence_score,
                evidence_detail,
            ),
            ConfidenceComponent(
                "AI Reasoning",
                AI_WEIGHTS["ai"],
                ai_confidence,
                "Model confidence, accepted only after signal citations were validated.",
            ),
            ConfidenceComponent(
                "Evidence Completeness",
                AI_WEIGHTS["completeness"],
                completeness,
                f"{completeness}% of attempted evidence was collected successfully.",
            ),
        ]
        return min(sum(item.contribution for item in components), 100), components

    def _evidence_score(
        self,
        analysis: AnalysisResult,
        healthy: bool,
        deterministic_score: int | None,
    ) -> tuple[int, str]:
        top = analysis.top_hypothesis
        if top is not None:
            return (
                top.confidence,
                f"Top hypothesis '{top.id}' is supported by "
                f"{len(top.supporting_signal_ids)} signal(s).",
            )

        if healthy:
            return (
                HEALTHY_EVIDENCE_SCORE,
                "No failure signals were detected; the absence of signals is itself evidence.",
            )

        if deterministic_score is not None:
            return (
                deterministic_score,
                "No hypothesis matched; scored from raw evidence heuristics.",
            )

        return (
            UNSUPPORTED_EVIDENCE_SCORE,
            "Issues were reported but no hypothesis matched the observed signals.",
        )
