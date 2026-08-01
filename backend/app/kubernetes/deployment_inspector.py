from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest


class DeploymentInspector:
    id = "k8s.deployments"
    kind = EvidenceKind.DEPLOYMENTS
    label = "Validated Deployments"

    def requests(self, scope) -> list[ResourceRequest]:
        name = scope.resource_name if scope.targets("deployment") else None
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="deployment" if name else "deployments",
                name=name,
                namespace=scope.namespace,
                all_namespaces=not scope.namespace and not name,
            )
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        result = results[0]
        if not usable(result):
            return failure(result, unhealthy_deployments=[])

        data: dict[str, Any] = result.data  # type: ignore[assignment]
        listed = data.get("items")

        unhealthy_deployments = []

        deployment_items = listed if isinstance(listed, list) else [data]
        for deployment in deployment_items:
            status = deployment.get("status", {})
            metadata = deployment.get("metadata", {})
            spec = deployment.get("spec", {})

            desired = spec.get("replicas", 0)
            available = status.get("availableReplicas", 0)
            unavailable = status.get("unavailableReplicas", 0)
            conditions = status.get("conditions", [])
            condition_findings = self._condition_findings(conditions)

            if unavailable > 0 or available < desired or condition_findings:
                unhealthy_deployments.append(
                    {
                        "name": metadata.get("name", "unknown"),
                        "namespace": metadata.get("namespace", "default"),
                        "desired_replicas": desired,
                        "available_replicas": available,
                        "unavailable_replicas": unavailable,
                        "conditions": condition_findings,
                    }
                )

        return {
            "healthy": len(unhealthy_deployments) == 0,
            "unhealthy_deployments": unhealthy_deployments,
            # As with pods: counts the list, so a single-deployment read reports
            # 0. Preserved rather than corrected, because parity is the point.
            "total_deployments": len(listed) if isinstance(listed, list) else 0,
        }

    def _condition_findings(self, conditions: list[dict[str, Any]]) -> list[dict[str, str]]:
        findings = []

        for condition in conditions:
            condition_type = condition.get("type", "")
            status = condition.get("status", "")
            reason = condition.get("reason", "")

            if (
                (condition_type == "ReplicaFailure" and status == "True")
                or (condition_type == "Progressing" and status == "False")
                or reason in {"ProgressDeadlineExceeded", "FailedCreate"}
            ):
                findings.append(self._condition_summary(condition))

        return findings

    def _condition_summary(self, condition: dict[str, Any]) -> dict[str, str]:
        return {
            "type": condition.get("type", ""),
            "status": condition.get("status", ""),
            "reason": condition.get("reason", ""),
            "message": condition.get("message", "")[:500],
        }
