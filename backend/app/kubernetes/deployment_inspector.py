from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor


class DeploymentInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(
        self,
        namespace: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        args = ["get", "deployments"]
        if deployment_name:
            args = ["get", "deployment", deployment_name]
        if namespace:
            args.extend(["-n", namespace])
        elif not deployment_name:
            args.append("-A")
        args.extend(["-o", "json"])

        result = self.kubectl.run(args, parse_json=True)
        if not result.success or not isinstance(result.data, dict):
            return {
                "healthy": False,
                "unhealthy_deployments": [],
                "error": result.stderr,
                "command": result.to_dict(),
            }

        unhealthy_deployments = []

        deployment_items = result.data.get("items", [result.data] if deployment_name else [])
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
            "total_deployments": len(result.data.get("items", [])),
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
