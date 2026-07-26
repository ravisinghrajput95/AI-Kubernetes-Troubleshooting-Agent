import json
from typing import Any

from loguru import logger

from app.ai.confidence_engine import ConfidenceEngine
from app.ai.fix_recommendation_engine import FixRecommendationEngine
from app.ai.llm_client import LLMClient
from app.ai.prompt_builder import PromptBuilder


class RootCauseAnalyzer:
    def __init__(self) -> None:
        self.prompt_builder = PromptBuilder()
        self.llm_client = LLMClient()
        self.fix_engine = FixRecommendationEngine()
        self.confidence_engine = ConfidenceEngine()

    def analyze(self, investigation: dict[str, Any]) -> dict[str, Any]:
        messages = self.prompt_builder.build_messages(investigation)
        llm_result = self.llm_client.complete(messages)

        if llm_result["success"]:
            parsed = self._parse_llm_json(llm_result["content"])
            if parsed:
                return self._normalize(parsed, investigation, ai_generated=True)

        logger.warning("Using deterministic diagnosis fallback")
        return self._fallback(investigation, llm_result.get("error", ""))

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
        ai_generated: bool,
    ) -> dict[str, Any]:
        fallback = self._fallback(investigation, "")

        commands = diagnosis.get("kubectl_commands", fallback["kubectl_commands"])
        if isinstance(commands, str):
            commands = [commands]

        confidence = diagnosis.get("confidence", fallback["confidence"])
        try:
            confidence = int(confidence)
        except (TypeError, ValueError):
            confidence = fallback["confidence"]

        return {
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
            "confidence": max(0, min(confidence, 100)),
            "confidence_reasoning": diagnosis.get("confidence_reasoning")
            or fallback["confidence_reasoning"],
            "remediation_risk": self._remediation_risk(investigation),
            "remediation_plan": self._remediation_plan(investigation, commands),
            "ai_generated": ai_generated,
        }

    def _fallback(self, investigation: dict[str, Any], error: str) -> dict[str, Any]:
        recommendation = self.fix_engine.recommend(investigation)
        confidence, reasons = self.confidence_engine.score(investigation)
        root_cause = self._root_cause_summary(investigation)

        explanation = (
            "The diagnosis was generated from collected Kubernetes evidence. "
            "Configure OPENAI_API_KEY to enable full LLM reasoning."
        )
        if error:
            explanation = f"{explanation} OpenAI status: {error}"

        return {
            "root_cause": root_cause,
            "explanation": explanation,
            "fix": recommendation["fix"],
            "kubectl_commands": recommendation["kubectl_commands"],
            "prevention": recommendation["prevention"],
            "evidence_gaps": self._evidence_gaps(investigation),
            "next_steps": recommendation.get("next_steps", self._next_steps(investigation)),
            "confidence": confidence,
            "confidence_reasoning": reasons,
            "remediation_risk": self._remediation_risk(investigation),
            "remediation_plan": self._remediation_plan(
                investigation,
                recommendation["kubectl_commands"],
            ),
            "ai_generated": False,
        }

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

    def _evidence_gaps(self, investigation: dict[str, Any]) -> list[str]:
        gaps = []
        if not investigation.get("logs", {}).get("logs"):
            gaps.append("No failing pod logs were collected.")
        if not investigation.get("metrics", {}).get("available"):
            gaps.append("Metrics-server data is unavailable or not permitted.")
        if investigation.get("storage", {}).get("error"):
            gaps.append("Storage resources could not be inspected.")
        if investigation.get("nodes", {}).get("error"):
            gaps.append("Node conditions could not be inspected.")
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

    def _root_cause_summary(self, investigation: dict[str, Any]) -> str:
        health = investigation.get("health", {})
        if health.get("status") == "error":
            return health.get(
                "message",
                "Unable to connect to Kubernetes cluster.",
            )
        if health.get("status") == "healthy":
            return "No critical Kubernetes issues detected. Cluster appears healthy."

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
