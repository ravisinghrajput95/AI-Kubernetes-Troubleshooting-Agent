from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor

EVENT_REASONS = {
    "FailedScheduling",
    "BackOff",
    "FailedMount",
    "FailedPull",
    "ErrImagePull",
    "Unhealthy",
}


class EventsAnalyzer:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def analyze(self, namespace: str | None = None) -> dict[str, Any]:
        args = ["get", "events"]
        if namespace:
            args.extend(["-n", namespace])
        else:
            args.append("-A")
        args.extend(["-o", "json"])

        result = self.kubectl.run(args, parse_json=True)
        if not result.success or not isinstance(result.data, dict):
            return {
                "healthy": False,
                "findings": [],
                "error": result.stderr,
                "command": result.to_dict(),
            }

        findings = []
        for event in result.data.get("items", []):
            reason = event.get("reason", "")
            event_type = event.get("type", "")

            if reason in EVENT_REASONS or event_type == "Warning":
                metadata = event.get("metadata", {})
                involved = event.get("involvedObject", {})
                findings.append(
                    {
                        "namespace": metadata.get("namespace", "default"),
                        "reason": reason,
                        "type": event_type,
                        "object": f"{involved.get('kind', 'Object')}/{involved.get('name', 'unknown')}",
                        "message": event.get("message", "")[:500],
                    }
                )

        return {
            "healthy": len(findings) == 0,
            "findings": findings[:50],
            "total_events": len(result.data.get("items", [])),
        }
