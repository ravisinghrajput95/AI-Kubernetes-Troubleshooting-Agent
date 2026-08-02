import json
from typing import Any

from loguru import logger

from app.ai.confidence_engine import ConfidenceEngine
from app.ai.fix_recommendation_engine import FixRecommendationEngine
from app.ai.llm_client import LLMClient
from app.ai.prompt_builder import PromptBuilder
from app.analysis.confidence import CompositeConfidenceScorer
from app.analysis.engine import AnalysisEngine
from app.analysis.grounding import GroundingResult, GroundingValidator
from app.analysis.models import AnalysisResult
from app.kubernetes.command_policy import CommandClass, classify_command
from app.observability import metrics
from app.remediation.planner import RemediationPlanner

# Grounding messages quote the response they rejected, and that quotes cluster
# text. Mapping them onto a closed set is what keeps a metric label bounded and
# free of attacker-controlled content; an unrecognised message is `other`, not
# the message.
_REJECTION_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("no root cause", "empty_root_cause"),
    ("unknown hypothesis", "fabricated_hypothesis"),
    ("citation", "bad_citations"),
    ("cited signal", "irrelevant_citations"),
    ("appears in no collected evidence", "invented_resource"),
    ("no action needed", "contradiction"),
    ("severe signal", "contradiction"),
)


def _rejection_category(reason: str) -> str:
    lowered = (reason or "").lower()
    for needle, category in _REJECTION_CATEGORIES:
        if needle in lowered:
            return category
    return "other"


class RootCauseAnalyzer:
    """Produces a diagnosis from an investigation.

    The pipeline is deterministic-first: evidence becomes signals, signals become
    ranked hypotheses, and only then is a model asked to select and explain.
    Model output is accepted only if its citations resolve to real signals;
    otherwise the deterministic ranking stands. Either path yields the same
    diagnosis shape.
    """

    def __init__(self) -> None:
        self.analysis_engine = AnalysisEngine()
        self.prompt_builder = PromptBuilder()
        self.llm_client = LLMClient()
        self.fix_engine = FixRecommendationEngine()
        self.confidence_engine = ConfidenceEngine()
        self.validator = GroundingValidator()
        self.scorer = CompositeConfidenceScorer()
        self.remediation_planner = RemediationPlanner()

    def analyze(self, investigation: dict[str, Any]) -> dict[str, Any]:
        analysis = self.analysis_engine.analyze(investigation)

        messages = self.prompt_builder.build_messages(investigation, analysis)
        llm_result = self.llm_client.complete(messages)

        metrics.llm_call("succeeded" if llm_result["success"] else "failed")

        if llm_result["success"]:
            parsed = self._parse_llm_json(llm_result["content"])
            if parsed is None:
                metrics.grounding_rejected("unparseable")
            else:
                grounding = self.validator.validate(parsed, analysis)
                if grounding.valid:
                    metrics.diagnosis("grounded")
                    return self._normalize(parsed, investigation, analysis, grounding)
                # A fixed category, never `grounding.reason` — that quotes the
                # model's prose, which quotes cluster text, which is
                # attacker-influenced. An unbounded hostile string must not
                # become a label. See `app/observability/metrics.py`.
                metrics.grounding_rejected(_rejection_category(grounding.reason))
                logger.warning(
                    "Rejecting ungrounded model diagnosis: {reason}",
                    reason=grounding.reason,
                )

        metrics.diagnosis("fallback")
        logger.warning("Using deterministic diagnosis fallback")
        return self._fallback(investigation, analysis, llm_result.get("error", ""))

    def _parse_llm_json(self, content: str) -> dict[str, Any] | None:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.removeprefix("json").strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("LLM returned invalid JSON: {error}", error=str(exc))
            return None

        return parsed if isinstance(parsed, dict) else None

    def _normalize(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        analysis: AnalysisResult,
        grounding: GroundingResult,
    ) -> dict[str, Any]:
        fallback = self._fallback(investigation, analysis, "")

        # Commands are NEVER taken from the model. Signals and hypotheses are
        # built from cluster text — log lines, event messages, resource names —
        # which an attacker who can write to the cluster controls. A model that
        # follows an injected instruction would otherwise emit a command string
        # that an operator sees as a recommendation and runs by hand. The
        # platform's read-only executor does not protect against that, because
        # the human is the execution path.
        commands = fallback["kubectl_commands"]

        try:
            ai_confidence = int(diagnosis.get("confidence"))
        except (TypeError, ValueError):
            ai_confidence = fallback["confidence"]

        confidence, components = self.scorer.score(
            analysis,
            completeness=self._completeness(investigation),
            healthy=self._healthy(investigation),
            ai_confidence=ai_confidence,
        )

        cited_signals = grounding.cited_signals or fallback["cited_signals"]

        return {
            **fallback,
            "root_cause": diagnosis.get("root_cause") or fallback["root_cause"],
            "explanation": diagnosis.get("explanation") or fallback["explanation"],
            "fix": diagnosis.get("fix") or fallback["fix"],
            "kubectl_commands": commands,
            "prevention": diagnosis.get("prevention") or fallback["prevention"],
            "evidence_gaps": self._string_list(
                diagnosis.get("evidence_gaps"),
                fallback["evidence_gaps"],
            ),
            "next_steps": self._string_list(diagnosis.get("next_steps"), fallback["next_steps"]),
            "confidence": confidence,
            "confidence_reasoning": self._string_list(
                diagnosis.get("confidence_reasoning"),
                fallback["confidence_reasoning"],
            ),
            "confidence_breakdown": [item.to_dict() for item in components],
            # `remediation`, `remediation_plan`, `remediation_risk` and `patches`
            # are inherited from the fallback unchanged: they are computed from
            # the hypothesis deterministically and must not reflect model output.
            "selected_hypothesis": grounding.selected_hypothesis or fallback["selected_hypothesis"],
            "cited_signals": list(cited_signals),
            "cited_evidence": analysis.evidence_ids_for(tuple(cited_signals)),
            "grounding": grounding.to_dict(),
            "ai_generated": True,
        }

    def _fallback(
        self,
        investigation: dict[str, Any],
        analysis: AnalysisResult,
        error: str,
    ) -> dict[str, Any]:
        recommendation = self.fix_engine.recommend(investigation)
        deterministic_score, reasons = self.confidence_engine.score(investigation)
        top = analysis.top_hypothesis
        safe_commands = self._vetted_commands(recommendation["kubectl_commands"])

        confidence, components = self.scorer.score(
            analysis,
            completeness=self._completeness(investigation),
            healthy=self._healthy(investigation),
            ai_confidence=None,
            deterministic_score=deterministic_score,
        )

        explanation = self._explanation(analysis, error)
        cited_signals = list(top.supporting_signal_ids) if top else []
        fix = top.remediation_hint if top and top.remediation_hint else recommendation["fix"]

        # The remediation plan is derived from the hypothesis, so it can name the
        # actual workload, container and rollback. `remediation_risk` and
        # `remediation_plan` are projected from it to preserve their existing
        # shape for the console and reports.
        plan = self.remediation_planner.plan(analysis, investigation)

        return {
            "root_cause": self._root_cause_summary(investigation, analysis),
            "explanation": explanation,
            "fix": fix,
            "kubectl_commands": safe_commands,
            "prevention": recommendation["prevention"],
            "evidence_gaps": self._evidence_gaps(investigation, analysis),
            "next_steps": recommendation.get("next_steps", self._next_steps(investigation)),
            "confidence": confidence,
            "confidence_reasoning": self._confidence_reasoning(analysis, reasons),
            "confidence_breakdown": [item.to_dict() for item in components],
            "remediation_risk": (
                plan.risk.to_legacy() if plan else self._remediation_risk(investigation)
            ),
            "remediation_plan": (
                plan.to_legacy_plan()
                if plan
                else self._remediation_plan(investigation, recommendation["kubectl_commands"])
            ),
            "remediation": plan.to_dict() if plan else None,
            "patches": [patch.to_dict() for patch in plan.patches] if plan else [],
            "signals": [signal.to_dict() for signal in analysis.signals],
            "hypotheses": [item.to_dict() for item in analysis.hypotheses],
            "selected_hypothesis": top.id if top else None,
            "cited_signals": cited_signals,
            "cited_evidence": analysis.evidence_ids_for(tuple(cited_signals)),
            "grounding": GroundingResult(
                valid=True,
                reason="Deterministic diagnosis; no model output was used.",
                selected_hypothesis=top.id if top else None,
                cited_signals=tuple(cited_signals),
            ).to_dict(),
            "ai_generated": False,
        }

    def _explanation(self, analysis: AnalysisResult, error: str) -> str:
        top = analysis.top_hypothesis
        if top is not None:
            explanation = (
                f"{top.rationale} This conclusion is supported by "
                f"{len(top.supporting_signal_ids)} signal(s) extracted from collected evidence."
            )
        else:
            explanation = (
                "The diagnosis was generated from collected Kubernetes evidence. "
                "No failure pattern matched the observed signals."
            )

        if error:
            explanation = f"{explanation} OpenAI status: {error}"
        return explanation

    def _confidence_reasoning(self, analysis: AnalysisResult, reasons: list[str]) -> list[str]:
        top = analysis.top_hypothesis
        if top is None:
            return reasons

        return [
            f"Hypothesis '{top.title}' matched {len(top.supporting_signal_ids)} signal(s).",
            *reasons,
        ]

    def _completeness(self, investigation: dict[str, Any]) -> int:
        coverage = investigation.get("evidence_coverage", {})
        if isinstance(coverage, dict) and coverage.get("completeness") is not None:
            return int(coverage["completeness"])
        return 100

    def _healthy(self, investigation: dict[str, Any]) -> bool:
        return investigation.get("health", {}).get("status") == "healthy"

    def _vetted_commands(self, commands: list[str]) -> list[str]:
        """Drop any command the platform cannot vouch for.

        Everything surfaced as a recommendation is classified first. An
        unrecognised string never reaches an operator, and a mutating one is
        labelled so it cannot be mistaken for a safe diagnostic.
        """
        vetted: list[str] = []

        for command in commands:
            command_class = classify_command(str(command))
            if command_class is CommandClass.UNRECOGNISED:
                logger.warning(
                    "Dropping unrecognised recommended command: {command}",
                    command=str(command)[:120],
                )
                continue
            if command_class is CommandClass.MUTATING:
                vetted.append(f"{command}   # CHANGES STATE - review before running")
            else:
                vetted.append(str(command))

        return vetted

    def _remediation_risk(self, investigation: dict[str, Any]) -> dict[str, Any]:
        severity = investigation.get("severity", {}).get("severity", "Healthy")
        network_findings = investigation.get("network", {}).get("findings", [])
        unhealthy_deployments = investigation.get("deployments", {}).get(
            "unhealthy_deployments",
            [],
        )

        if severity == "Healthy":
            level = "Low"
            downtime = "No downtime expected"
        elif network_findings:
            level = "Medium"
            downtime = "Service routing may be affected"
        elif len(unhealthy_deployments) > 1:
            level = "Medium"
            downtime = "Rolling restart recommended"
        else:
            level = "Low"
            downtime = "No downtime expected"

        return {
            "level": level,
            "impact": [
                "Restart Required" if severity != "Healthy" else "No Restart Required",
                downtime,
                "Rollback Available",
            ],
        }

    def _remediation_plan(
        self,
        investigation: dict[str, Any],
        commands: list[str],
    ) -> dict[str, Any]:
        severity = investigation.get("severity", {}).get("severity", "Healthy")
        return {
            "requires_approval": severity != "Healthy",
            "dry_run_first": True,
            "pre_checks": [
                "Confirm the selected context and namespace before changing resources.",
                "Capture current rollout/resource state before applying a fix.",
            ],
            "review_commands": commands[:3],
            "rollback_commands": self._rollback_commands(investigation),
        }

    def _rollback_commands(self, investigation: dict[str, Any]) -> list[str]:
        deployments = investigation.get("deployments", {}).get("unhealthy_deployments", [])
        if not deployments:
            return ["kubectl rollout history deployment <deployment-name> -n <namespace>"]

        deployment = deployments[0]
        namespace = deployment.get("namespace", "<namespace>")
        name = deployment.get("name", "<deployment-name>")
        return [
            f"kubectl rollout history deployment {name} -n {namespace}",
            f"kubectl rollout undo deployment {name} -n {namespace}",
            f"kubectl rollout status deployment {name} -n {namespace}",
        ]

    def _evidence_gaps(
        self,
        investigation: dict[str, Any],
        analysis: AnalysisResult,
    ) -> list[str]:
        gaps = []
        if not investigation.get("logs", {}).get("logs"):
            gaps.append("No failing pod logs were collected.")
        if not investigation.get("metrics", {}).get("available"):
            gaps.append("Metrics-server data is unavailable or not permitted.")
        if investigation.get("storage", {}).get("error"):
            gaps.append("Storage resources could not be inspected.")
        if investigation.get("nodes", {}).get("error"):
            gaps.append("Node conditions could not be inspected.")

        limits = investigation.get("collection_limits", {})
        for read in limits.get("reads", []):
            gaps.append(
                f"Only {read.get('retained')} of {read.get('returned')} objects were "
                f"examined for: {read.get('command', '')}. The cluster is larger than "
                f"this investigation looked."
            )

        for degraded in investigation.get("evidence_coverage", {}).get("degraded", []):
            detail = degraded.get("detail")
            if detail and detail not in gaps:
                gaps.append(detail)

        # What the leading hypothesis still needs in order to be confirmed.
        top = analysis.top_hypothesis
        if top is not None:
            gaps.extend(item for item in top.missing_evidence if item not in gaps)

        return gaps or ["No major evidence gaps detected in the collected signals."]

    def _next_steps(self, investigation: dict[str, Any]) -> list[str]:
        if investigation.get("health", {}).get("status") == "healthy":
            return ["Keep monitoring events and rollout health for regressions."]
        return [
            "Review the highest-severity finding and confirm it matches the affected workload.",
            "Run the suggested read-only commands before applying any remediation.",
            "Apply the fix during an approved window if it may restart workloads.",
        ]

    def _string_list(self, value: Any, fallback: list[str]) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value.strip():
            return [value]
        return fallback

    def _root_cause_summary(
        self,
        investigation: dict[str, Any],
        analysis: AnalysisResult,
    ) -> str:
        health = investigation.get("health", {})
        if health.get("status") == "error":
            return health.get(
                "message",
                "Unable to connect to Kubernetes cluster.",
            )
        if health.get("status") == "healthy":
            return "No critical Kubernetes issues detected. Cluster appears healthy."

        top = analysis.top_hypothesis
        if top is not None:
            return f"{top.title} ({top.target.key})."

        problematic_pods = investigation.get("pods", {}).get("problematic_pods", [])
        if problematic_pods:
            pod = problematic_pods[0]
            return (
                f"Pod {pod.get('namespace', 'default')}/{pod.get('name', 'unknown')} "
                f"is unhealthy with status {pod.get('status', 'unknown')}."
            )

        deployment_findings = investigation.get("deployments", {}).get("unhealthy_deployments", [])
        if deployment_findings:
            deployment = deployment_findings[0]
            return (
                f"Deployment {deployment.get('namespace', 'default')}/"
                f"{deployment.get('name', 'unknown')} has unavailable replicas."
            )

        network_findings = investigation.get("network", {}).get("findings", [])
        if network_findings:
            finding = network_findings[0]
            return f"Networking issue detected: {finding.get('issue', 'unknown issue')}."

        event_findings = investigation.get("events", {}).get("findings", [])
        if event_findings:
            event = event_findings[0]
            return f"Kubernetes event {event.get('reason', 'Warning')} indicates a cluster issue."

        return "No clear Kubernetes failure was detected in the collected evidence."
