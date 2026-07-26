from typing import Any


class ConfidenceEngine:
    def score(self, investigation: dict[str, Any]) -> tuple[int, list[str]]:
        score = 35
        reasons = []

        problematic_pods = investigation.get("pods", {}).get("problematic_pods", [])
        if problematic_pods:
            score += 20
            reasons.append("Problematic pod state was detected")

        logs = investigation.get("logs", {}).get("logs", [])
        if any(item.get("relevant_lines") for item in logs):
            score += 20
            reasons.append("Pod logs contain relevant failure lines")

        events = investigation.get("events", {}).get("findings", [])
        if events:
            score += 15
            reasons.append("Kubernetes warning events support the finding")

        deployments = investigation.get("deployments", {}).get("unhealthy_deployments", [])
        if deployments:
            score += 10
            reasons.append("Deployment health confirms rollout or replica issues")

        network = investigation.get("network", {}).get("findings", [])
        if network:
            score += 10
            reasons.append("Networking findings provide additional signal")

        if not reasons:
            reasons.append("No strong failure signal was found in the collected evidence")

        return min(score, 95), reasons
