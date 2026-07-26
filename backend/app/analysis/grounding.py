"""Validation that a model's diagnosis is grounded in real signals.

Instructing a model not to invent facts is unenforceable; refusing to accept
output that misrepresents the evidence is enforceable, and that is what happens
here. Two distinct properties are checked:

**Citation integrity** — every cited signal and the selected hypothesis resolve
to real objects.

**Semantic consistency** — the prose does not contradict what it cites. Citation
integrity alone is insufficient: a response can cite a genuine CrashLoopBackOff
signal while concluding "resolved, no action needed", and every id in it would
validate. That was a real gap, verified 2026-07-26.

These checks are deterministic. A second model call would add latency and cost
to every investigation, and would itself need grounding.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.analysis.models import AnalysisResult, Severity

# Language asserting that nothing is wrong. Only meaningful when severe signals
# exist — on a genuinely healthy cluster these are the correct conclusion.
RESOLUTION_PHRASES = (
    "no action needed",
    "no action required",
    "no action is needed",
    "no issues",
    "no problems",
    "nothing is wrong",
    "nothing wrong",
    "all clear",
    "appears healthy",
    "is healthy",
    "are healthy",
    "operating normally",
    "functioning normally",
    "working as expected",
    "no remediation",
    "already resolved",
    "self-resolved",
    "no further action",
)

# `namespace/name` in Kubernetes DNS-1123 form. Deliberately case-sensitive:
# resource names are lowercase, so this does not match quantities such as
# "512Mi/1Gi". The lookahead avoids file paths and version strings.
RESOURCE_REFERENCE = re.compile(r"\b([a-z0-9][a-z0-9-]{0,61})/([a-z0-9][a-z0-9-]{0,61})\b(?![./])")

SEVERE = frozenset({Severity.CRITICAL, Severity.HIGH})


@dataclass(frozen=True)
class GroundingResult:
    valid: bool
    reason: str = ""
    selected_hypothesis: str | None = None
    cited_signals: tuple[str, ...] = ()
    rejected_citations: tuple[str, ...] = field(default=())
    # Which named checks ran and passed, so a reviewer can see what was verified
    # rather than infer it from a boolean.
    checks: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "selected_hypothesis": self.selected_hypothesis,
            "cited_signals": list(self.cited_signals),
            "rejected_citations": list(self.rejected_citations),
            "checks_passed": list(self.checks),
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

        prose = " ".join(str(payload.get(field) or "") for field in ("root_cause", "explanation"))

        semantic_failure = (
            self._contradicts_evidence(prose, analysis)
            or self._citations_ignore_selection(cited, selected_id, analysis)
            or self._invents_resources(prose, analysis)
        )
        if semantic_failure:
            logger.warning(
                "Rejecting semantically inconsistent diagnosis: {reason}", reason=semantic_failure
            )
            return GroundingResult(
                valid=False,
                reason=semantic_failure,
                selected_hypothesis=selected_id,
                cited_signals=cited,
                rejected_citations=rejected,
            )

        return GroundingResult(
            valid=True,
            selected_hypothesis=selected_id,
            cited_signals=cited,
            rejected_citations=rejected,
            checks=(
                "citations_resolve",
                "hypothesis_resolves",
                "no_contradiction",
                "citations_support_selection",
                "no_invented_resources",
            ),
        )

    def _contradicts_evidence(self, prose: str, analysis: AnalysisResult) -> str:
        """Reject a conclusion of "nothing is wrong" over severe evidence.

        This is the gap citation integrity leaves open: every id can resolve
        while the prose asserts the opposite of what those ids say. On a
        genuinely healthy cluster there are no severe signals, so the same
        wording is accepted.
        """
        severe = [signal for signal in analysis.signals if signal.severity in SEVERE]
        if not severe:
            return ""

        lowered = prose.lower()
        for phrase in RESOLUTION_PHRASES:
            if phrase in lowered:
                return (
                    f"Diagnosis claims '{phrase}' while {len(severe)} severe signal(s) "
                    f"are present, including: {severe[0].summary}"
                )
        return ""

    def _citations_ignore_selection(
        self,
        cited: tuple[str, ...],
        selected_id: str | None,
        analysis: AnalysisResult,
    ) -> str:
        """Reject citations that have nothing to do with the chosen hypothesis.

        Citing real but unrelated signals passes an id check while explaining
        nothing, so require at least one citation the hypothesis actually rests
        on.
        """
        if not selected_id or not cited:
            return ""

        hypothesis = analysis.hypothesis(selected_id)
        if hypothesis is None or not hypothesis.supporting_signal_ids:
            return ""

        supporting = set(hypothesis.supporting_signal_ids)
        if supporting & set(cited):
            return ""

        return f"None of the cited signals support the selected hypothesis '{selected_id}'"

    def _invents_resources(self, prose: str, analysis: AnalysisResult) -> str:
        """Reject `namespace/name` references to resources no signal mentions.

        Makes "never invent pod names" enforceable rather than merely
        instructed. Matching is lenient — a reference is accepted if either half
        appears anywhere in the evidence — because a false rejection discards a
        sound diagnosis.
        """
        if not analysis.signals:
            return ""

        known: set[str] = set()
        for signal in analysis.signals:
            target = signal.target
            known.update(
                part for part in (target.name, target.namespace, target.key, signal.summary) if part
            )

        corpus = " ".join(known).lower()

        for match in RESOURCE_REFERENCE.finditer(prose):
            namespace, name = match.groups()
            if name in corpus or namespace in corpus:
                continue
            return (
                f"Diagnosis references '{match.group(0)}', which appears in no collected evidence"
            )
        return ""

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
