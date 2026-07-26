from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor

PROBLEM_STATUSES = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "Pending",
    "Error",
    "OOMKilled",
}


class PodInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(self, namespace: str | None = None, pod_name: str | None = None) -> dict[str, Any]:
        args = ["get", "pods"]
        if pod_name:
            args = ["get", "pod", pod_name]
        if namespace:
            args.extend(["-n", namespace])
        elif not pod_name:
            args.append("-A")
        args.extend(["-o", "json"])

        result = self.kubectl.run(args, parse_json=True)
        if not result.success or not isinstance(result.data, dict):
            return {
                "healthy": False,
                "problematic_pods": [],
                "error": result.stderr,
                "command": result.to_dict(),
            }

        problematic_pods = []
        pod_inventory = []
        running_pods = 0

        pod_items = result.data.get("items", [result.data] if pod_name else [])
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
            "total_pods": len(result.data.get("items", [])),
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
