from collections.abc import Sequence
from typing import Any

from app.providers.base import ProviderResult, ReadVerb, ResourceRequest

LOG_FAILURE_KEYWORDS = (
    "exception",
    "error",
    "failed",
    "failure",
    "connection refused",
    "connection timed out",
    "missing",
    "environment variable",
    "env var",
    "imagepull",
    "back-off",
    "startup",
)

# Reading every pod's logs on a large broken cluster is its own outage. The
# limit predates M5 and is unchanged; it now bounds a batch rather than a loop.
MAX_PODS = 10


class LogsCollector:
    """Log reads for the pods the pod inspector flagged.

    Unlike the inspectors this fans out over a variable number of targets, so
    its `requests` takes the pods rather than the scope. The pairing is still
    positional: request *i* is `pods[i]`.
    """

    def requests(self, problematic_pods: list[dict[str, Any]]) -> list[ResourceRequest]:
        return [
            ResourceRequest(
                verb=ReadVerb.LOGS,
                name=pod["name"],
                namespace=pod.get("namespace", "default"),
                options={"tail": 120, "all_containers": True},
            )
            for pod in self.targets(problematic_pods)
        ]

    def targets(self, problematic_pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pods that will actually be read, in request order."""
        return [pod for pod in problematic_pods[:MAX_PODS] if pod.get("name")]

    def analyse(
        self,
        problematic_pods: list[dict[str, Any]],
        results: Sequence[ProviderResult],
    ) -> dict[str, Any]:
        pod_logs = []

        for pod, result in zip(self.targets(problematic_pods), results, strict=True):
            pod_logs.append(
                {
                    "name": pod["name"],
                    "namespace": pod.get("namespace", "default"),
                    "status": pod.get("status"),
                    "success": result.success,
                    "relevant_lines": self._relevant_lines(result.text),
                    "error": result.error if not result.success else "",
                }
            )

        return {
            "checked_pods": len(pod_logs),
            "logs": pod_logs,
        }

    def _relevant_lines(self, logs: str) -> list[str]:
        lines = []

        for line in logs.splitlines():
            if any(keyword in line.lower() for keyword in LOG_FAILURE_KEYWORDS):
                lines.append(line[:500])

        if lines:
            return lines[:25]

        return [line[:500] for line in logs.splitlines()[-20:]]
