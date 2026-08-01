from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, items, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest


class NodeInspector:
    id = "k8s.nodes"
    kind = EvidenceKind.NODES
    label = "Checked Nodes"

    def requests(self, scope) -> list[ResourceRequest]:
        # Nodes are cluster-scoped; a namespaced investigation still needs them.
        return [ResourceRequest(verb=ReadVerb.GET, resource="nodes")]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        result = results[0]
        if not usable(result):
            return failure(result, findings=[])

        nodes = items(result)
        findings = []
        inventory = []
        for node in nodes:
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
            "total_nodes": len(nodes),
        }

    def _finding(self, node_name: str, condition: dict[str, Any]) -> dict[str, str]:
        return {
            "node": node_name,
            "type": condition.get("type", ""),
            "status": condition.get("status", ""),
            "reason": condition.get("reason", ""),
            "message": condition.get("message", "")[:500],
        }
