import json
from typing import Any

from app.ai.evidence_redactor import EvidenceRedactor


class PromptBuilder:
    def __init__(self) -> None:
        self.redactor = EvidenceRedactor()

    def build_messages(self, investigation: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": self._system_prompt(),
            },
            {
                "role": "user",
                "content": self._user_prompt(investigation),
            },
        ]

    def _system_prompt(self) -> str:
        return (
            "You are a Senior Kubernetes SRE troubleshooting a live incident. "
            "Correlate pod status, logs, events, deployment health, and networking findings. "
            "Do not invent facts. If evidence is missing, say what is missing. "
            "Return only valid JSON with these keys: root_cause, explanation, fix, "
            "kubectl_commands, prevention, confidence, confidence_reasoning, evidence_gaps, next_steps. "
            "confidence must be an integer from 0 to 100. kubectl_commands must be a list of strings. "
            "evidence_gaps and next_steps must be lists of strings. "
            "Make the fix practical, beginner friendly, Kubernetes-specific, and safe to review before execution."
        )

    def _user_prompt(self, investigation: dict[str, Any]) -> str:
        evidence = {
            "scope": investigation.get("scope", {}),
            "pod_status": investigation.get("pods", {}),
            "logs": investigation.get("logs", {}),
            "events": investigation.get("events", {}),
            "deployment_health": investigation.get("deployments", {}),
            "networking_findings": investigation.get("network", {}),
            "node_findings": investigation.get("nodes", {}),
            "storage_findings": investigation.get("storage", {}),
            "extended_workloads": investigation.get("workloads", {}),
        }
        safe_evidence = self.redactor.redact(evidence)

        return (
            "Analyze this Kubernetes troubleshooting evidence and produce the diagnosis JSON.\n\n"
            f"{json.dumps(safe_evidence, indent=2)}"
        )
