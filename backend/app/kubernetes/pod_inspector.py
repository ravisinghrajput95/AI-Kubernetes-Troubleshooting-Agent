from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest

PROBLEM_STATUSES = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "Pending",
    "Error",
    "OOMKilled",
}


class PodInspector:
    id = "k8s.pods"
    kind = EvidenceKind.PODS
    label = "Retrieved Pods"

    def requests(self, scope) -> list[ResourceRequest]:
        # The same three cases the argv builder had: one named pod, one
        # namespace, or the whole cluster.
        pod_name = scope.resource_name if scope.targets("pod") else None
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="pod" if pod_name else "pods",
                name=pod_name,
                namespace=scope.namespace,
                all_namespaces=not scope.namespace and not pod_name,
            )
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        result = results[0]
        if not usable(result):
            return failure(result, problematic_pods=[])

        data: dict[str, Any] = result.data  # type: ignore[assignment]
        listed = data.get("items")

        problematic_pods = []
        pod_inventory = []
        running_pods = 0

        # A named read returns the object itself; a list read returns `items`.
        pod_items = listed if isinstance(listed, list) else [data]
        for pod in pod_items:
            metadata = pod.get("metadata", {})
            if pod.get("status", {}).get("phase") == "Running":
                running_pods += 1
            pod_status = self._detect_pod_status(pod)
            if pod_status:
                problematic_pods.append(
                    {
                        "name": metadata.get("name", "unknown"),
                        "namespace": metadata.get("namespace", "default"),
                        "status": pod_status,
                    }
                )
            pod_inventory.append(self._pod_summary(pod))

        return {
            "healthy": len(problematic_pods) == 0,
            "problematic_pods": problematic_pods,
            "pod_inventory": pod_inventory,
            # Counts the list, so a single-pod read reports 0 — preserved
            # verbatim from the pre-M5 inspector rather than quietly corrected,
            # because the differential suite compares this field.
            "total_pods": len(listed) if isinstance(listed, list) else 0,
            "running_pods": running_pods,
        }

    def _pod_summary(self, pod: dict[str, Any]) -> dict[str, Any]:
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status = pod.get("status", {})

        containers = []
        for container in spec.get("containers", []):
            resources = container.get("resources", {})
            containers.append(
                {
                    "name": container.get("name", "container"),
                    "image": container.get("image", ""),
                    "security_context": container.get("securityContext", {}),
                    "resources": resources,
                    "has_limits": bool(resources.get("limits")),
                    "has_requests": bool(resources.get("requests")),
                }
            )

        return {
            "name": metadata.get("name", "unknown"),
            "namespace": metadata.get("namespace", "default"),
            "node": spec.get("nodeName", "Pending"),
            "phase": status.get("phase", "Unknown"),
            # Carried so a Service's selector can be matched against the pods
            # this investigation already collected, rather than re-queried.
            "labels": metadata.get("labels", {}) or {},
            "containers": containers,
        }

    def _detect_pod_status(self, pod: dict[str, Any]) -> str | None:
        status = pod.get("status", {})
        phase = status.get("phase")

        if phase in PROBLEM_STATUSES:
            return phase

        for container_status in status.get("containerStatuses", []):
            state = container_status.get("state", {})
            last_state = container_status.get("lastState", {})
            waiting_reason = state.get("waiting", {}).get("reason")
            terminated_reason = state.get("terminated", {}).get("reason")
            last_terminated_reason = last_state.get("terminated", {}).get("reason")

            if waiting_reason in PROBLEM_STATUSES:
                return waiting_reason
            if terminated_reason in PROBLEM_STATUSES:
                return terminated_reason
            if last_terminated_reason in PROBLEM_STATUSES:
                return last_terminated_reason
            if waiting_reason == "ContainerCreating":
                return "ContainerCreating"

        for condition in status.get("conditions", []):
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return condition.get("reason", "Pending")

        return None
