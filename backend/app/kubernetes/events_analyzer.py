from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, items, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest

EVENT_REASONS = {
    "FailedScheduling",
    "BackOff",
    "FailedMount",
    "FailedPull",
    "ErrImagePull",
    "Unhealthy",
}


class EventsAnalyzer:
    id = "k8s.events"
    kind = EvidenceKind.EVENTS
    label = "Retrieved Events"

    def requests(self, scope) -> list[ResourceRequest]:
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="events",
                namespace=scope.namespace,
                all_namespaces=not scope.namespace,
            )
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        result = results[0]
        if not usable(result):
            return failure(result, findings=[])

        events = items(result)
        findings = []
        for event in events:
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
            "total_events": len(events),
        }
