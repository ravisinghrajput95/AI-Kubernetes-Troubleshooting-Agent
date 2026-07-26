from collections.abc import Sequence
from typing import Any

from loguru import logger

from app.analysis.models import AnalysisResult, Hypothesis
from app.remediation.context import RemediationContext
from app.remediation.models import (
    ChangeKind,
    RemediationPlan,
    RemediationRisk,
    RemediationStep,
    RiskLevel,
)
from app.remediation.rules import DEFAULT_REMEDIATION_RULES, RemediationRule


class RemediationPlanner:
    """Turns the leading hypothesis into a reviewable remediation plan.

    A hypothesis with no specific rule still produces a plan — a diagnostic one
    built from what the hypothesis says it is missing. Returning nothing would
    leave an operator with a diagnosis and no next step.
    """

    def __init__(self, rules: Sequence[RemediationRule] | None = None) -> None:
        source = rules if rules is not None else DEFAULT_REMEDIATION_RULES
        self._rules = {rule.hypothesis_id: rule for rule in source}

    def plan(
        self,
        analysis: AnalysisResult,
        investigation: dict[str, Any],
        hypothesis: Hypothesis | None = None,
    ) -> RemediationPlan | None:
        target = hypothesis or analysis.top_hypothesis
        if target is None:
            return None

        context = RemediationContext(
            hypothesis=target, analysis=analysis, investigation=investigation
        )
        rule = self._rules.get(target.id)

        if rule is None:
            return self._diagnostic_plan(context)

        try:
            return rule.build(context)
        except Exception as exc:
            logger.opt(exception=exc).error("Remediation rule for {id} failed", id=target.id)
            return self._diagnostic_plan(context)

    def _diagnostic_plan(self, context: RemediationContext) -> RemediationPlan:
        """Read-only next steps when no specific remediation is known.

        Deliberately proposes no change: inventing a fix for a failure mode the
        platform does not model would be worse than saying so.
        """
        hypothesis = context.hypothesis
        target = context.target
        flag = f" -n {target.namespace}" if target.namespace else ""

        investigate = [
            RemediationStep(f"Establish: {item}", None, manual=True)
            for item in hypothesis.missing_evidence
        ] or [
            RemediationStep(
                "Review the collected evidence for the affected resource.",
                None,
                manual=True,
            )
        ]

        return RemediationPlan(
            id="diagnostic-only",
            hypothesis_id=hypothesis.id,
            title=f"Investigate {hypothesis.title.lower()}",
            summary=(
                f"{hypothesis.rationale} No automated remediation is defined for this "
                f"failure mode, so these are read-only steps to confirm the cause before "
                f"any change is made."
            ),
            target=target,
            risk=RemediationRisk(
                level=RiskLevel.LOW,
                change_kind=ChangeKind.NONE,
                restart_required=False,
                estimated_downtime="None; no change is proposed",
                blast_radius="None",
                reversible=True,
            ),
            requires_approval=False,
            preconditions=(
                RemediationStep(
                    "Inspect the affected resource.",
                    f"kubectl describe {target.kind.lower()} {target.name}{flag}",
                ),
            ),
            remediation=tuple(investigate),
            verification=(
                RemediationStep(
                    "Re-run the investigation once the cause is confirmed.",
                    None,
                    manual=True,
                ),
            ),
            signal_ids=hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=((hypothesis.remediation_hint,) if hypothesis.remediation_hint else ()),
        )
