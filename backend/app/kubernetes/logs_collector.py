from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor

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


class LogsCollector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def collect(self, problematic_pods: list[dict[str, Any]]) -> dict[str, Any]:
        pod_logs = []

        for pod in problematic_pods[:10]:
            namespace = pod.get("namespace", "default")
            name = pod.get("name")

            if not name:
                continue

            result = self.kubectl.run(
                ["logs", name, "-n", namespace, "--tail=120", "--all-containers=true"],
            )

            pod_logs.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "status": pod.get("status"),
                    "success": result.success,
                    "relevant_lines": self._relevant_lines(result.stdout),
                    "error": result.stderr if not result.success else "",
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

