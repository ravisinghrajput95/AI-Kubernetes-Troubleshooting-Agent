from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor


class NodeInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(self) -> dict[str, Any]:
        result = self.kubectl.run(["get", "nodes", "-o", "json"], parse_json=True)
        if not result.success or not isinstance(result.data, dict):
            return {
                "healthy": False,
                "findings": [],
                "error": result.stderr,
                "command": result.to_dict(),
            }

        findings = []
        inventory = []
        for node in result.data.get("items", []):
            metadata = node.get("metadata", {})
            status = node.get("status", {})
            node_name = metadata.get("name", "unknown")
            conditions = status.get("conditions", [])
            taints = node.get("spec", {}).get("taints", [])

            inventory.append(
                {
                    "name": node_name,
                    "taints": [
                        f"{item.get('key')}={item.get('value', '')}:{item.get('effect')}"
                        for item in taints
                    ],
                    "capacity": status.get("capacity", {}),
                    "allocatable": status.get("allocatable", {}),
                }
            )

            for condition in conditions:
                condition_type = condition.get("type", "")
                condition_status = condition.get("status", "")
                if condition_type == "Ready" and condition_status != "True":
                    findings.append(self._finding(node_name, condition))
                if condition_type != "Ready" and condition_status == "True":
                    findings.append(self._finding(node_name, condition))

        return {
            "healthy": len(findings) == 0,
            "findings": findings,
            "inventory": inventory,
            "total_nodes": len(result.data.get("items", [])),
        }

    def _finding(self, node_name: str, condition: dict[str, Any]) -> dict[str, str]:
        return {
            "node": node_name,
            "type": condition.get("type", ""),
            "status": condition.get("status", ""),
            "reason": condition.get("reason", ""),
            "message": condition.get("message", "")[:500],
        }
