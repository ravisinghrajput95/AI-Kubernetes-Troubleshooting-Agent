"""Validation that a model's diagnosis is grounded in real signals.

This is the structural anti-hallucination control. Instructing a model not to
invent facts is unenforceable; refusing to accept output that references signals
which do not exist is enforceable, and that is what happens here.
"""

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.analysis.models import AnalysisResult


@dataclass(frozen=True)
class GroundingResult:
    valid: bool
    reason: str = ""
    selected_hypothesis: str | None = None
    cited_signals: tuple[str, ...] = ()
    rejected_citations: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "selected_hypothesis": self.selected_hypothesis,
            "cited_signals": list(self.cited_signals),
            "rejected_citations": list(self.rejected_citations),
        }


class GroundingValidator:
    """Checks model output against the deterministic analysis.

    Rejection policy, in order of severity:

    - A fabricated hypothesis id is a structural error and rejects the response.
    - Fabricated signal ids are stripped and recorded. If signals existed but
      none of the model's citations survive, the response is rejected: an
      uncitable conclusion is exactly what this layer exists to prevent.
    - An empty root cause rejects the response.

    When signals are absent entirely (a healthy cluster), citations are not
    required — there is nothing to cite.
    """

    def validate(self, payload: dict[str, Any], analysis: AnalysisResult) -> GroundingResult:
        root_cause = str(payload.get("root_cause") or "").strip()
        if not root_cause:
            return GroundingResult(valid=False, reason="Model returned no root cause")

        selected = payload.get("selected_hypothesis")
        selected_id = str(selected).strip() if selected else None
        if selected_id in {"", "null", "none", "None"}:
            selected_id = None

        if selected_id and selected_id not in analysis.hypothesis_ids:
            return GroundingResult(
                valid=False,
                reason=f"Model selected unknown hypothesis '{selected_id}'",
            )

        cited, rejected = self._partition_citations(payload, analysis)

        if analysis.signals and not cited:
            return GroundingResult(
                valid=False,
                reason="Model cited no valid signal despite signals being available",
                selected_hypothesis=selected_id,
                rejected_citations=rejected,
            )

        if rejected:
            logger.warning(
                "Dropped {count} fabricated signal citation(s): {ids}",
                count=len(rejected),
                ids=", ".join(rejected),
            )

        return GroundingResult(
            valid=True,
            selected_hypothesis=selected_id,
            cited_signals=cited,
            rejected_citations=rejected,
        )

    def _partition_citations(
        self,
        payload: dict[str, Any],
        analysis: AnalysisResult,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        raw = payload.get("cited_signals", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return (), ()

        known = analysis.signal_ids
        cited: list[str] = []
        rejected: list[str] = []

        for item in raw:
            citation = str(item).strip()
            if not citation:
                continue
            target = cited if citation in known else rejected
            if citation not in target:
                target.append(citation)

        return tuple(cited), tuple(rejected)
