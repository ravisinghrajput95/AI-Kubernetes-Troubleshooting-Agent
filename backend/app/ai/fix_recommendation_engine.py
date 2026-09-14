"""Prevention, follow-up steps and commands for the *selected* diagnosis.

This engine used to read `str(investigation).lower()` for the first matching
substring ("imagepullbackoff", "crashloopbackoff", ...) and aim every command
and follow-up at the first unhealthy deployment in the payload. Both were
decided by payload order rather than by the diagnosis, so in a namespace with
several concurrent faults the report said:

    Pod references configuration that does not exist (pod/payments/notifier-…)
    Preventive actions: Pin valid image tags and verify registry credentials.
    Follow-up: Confirm payments/checkout is the affected workload.
    Commands: kubectl edit deployment checkout -n payments

— prevention for an image fault nobody diagnosed, and a mutating command
against an unrelated deployment, returned verbatim through MCP to an
autonomous agent. It is F31 in prose: the console's remediation panel was
fixed to follow the hypothesis, and this was the same heuristic one layer
down. Found by reading the text of a live investigation, not by a test — every
test here had a single fault, where "the first workload" and "the diagnosed
workload" are the same thing.

So nothing here names a resource the diagnosis did not. Commands come from the
remediation plan, which `app/remediation/` already keys on the hypothesis and
target; prevention is keyed on the hypothesis's category; and with no
hypothesis at all the guidance is generic rather than guessed from text.
"""

from typing import Any

from app.analysis.models import Hypothesis
from app.remediation.models import RemediationPlan

# Keyed on `Hypothesis.category`, a closed set declared by the rules in
# `app/analysis/hypothesis_rules.py`. `tests/test_fix_recommendation.py`
# asserts every category a rule declares has an entry here, so a new category
# cannot silently fall through to the generic text.
PREVENTION_BY_CATEGORY: dict[str, str] = {
    "workload": (
        "Add startup and readiness probes, set memory limits from the observed working "
        "set, and smoke-test a rollout before it takes traffic."
    ),
    "image": "Pin image tags that exist and verify registry credentials before rollout.",
    "configuration": (
        "Deploy the ConfigMaps and Secrets a workload references before the workload "
        "itself, and validate those references in CI."
    ),
    "scheduling": (
        "Set realistic resource requests and watch namespace quotas and node capacity "
        "before deploying."
    ),
    "storage": (
        "Define StorageClasses and claims alongside the workloads that mount them, and "
        "check that a default StorageClass exists in every cluster."
    ),
    "network": (
        "Keep Service selectors, pod labels and NetworkPolicies defined together in "
        "reviewable manifests."
    ),
    "node": "Alert on node conditions and keep spare capacity to reschedule onto.",
    "infrastructure": (
        "Alert on node conditions and spread replicas so one node cannot take a workload down."
    ),
}

GENERIC_PREVENTION = (
    "Add health checks, clear rollout alerts, and deployment validation for Kubernetes manifests."
)

# Read-only and cluster-wide: with nothing diagnosed there is no resource the
# platform can responsibly point a command at.
GENERIC_COMMANDS = [
    "kubectl get pods -A",
    "kubectl get events -A --sort-by=.lastTimestamp",
    "kubectl get deployments -A",
]


class FixRecommendationEngine:
    def recommend(
        self,
        investigation: dict[str, Any],
        hypothesis: Hypothesis | None = None,
        plan: RemediationPlan | None = None,
    ) -> dict[str, Any]:
        healthy = investigation.get("health", {}).get("status") == "healthy"

        if hypothesis is None:
            return {
                "fix": (
                    "No failure pattern matched the collected evidence. Review the "
                    "warning events and failing workloads directly."
                )
                if not healthy
                else "No action is needed.",
                "kubectl_commands": list(GENERIC_COMMANDS),
                "prevention": GENERIC_PREVENTION,
                "next_steps": ["Keep monitoring events and rollout health for regressions."]
                if healthy
                else [
                    "Review warning events and failing workload conditions first.",
                    "Collect additional logs or metrics for any workload with unclear evidence.",
                ],
            }

        return {
            "fix": hypothesis.remediation_hint or (plan.summary if plan else ""),
            "kubectl_commands": plan.commands if plan else list(GENERIC_COMMANDS),
            "prevention": PREVENTION_BY_CATEGORY.get(hypothesis.category, GENERIC_PREVENTION),
            "next_steps": self._next_steps(hypothesis, plan),
        }

    def _next_steps(self, hypothesis: Hypothesis, plan: RemediationPlan | None) -> list[str]:
        # The plan's target, not the hypothesis's: a rule redirects a pod to the
        # Deployment that owns it, and that is the object an operator changes.
        # Unless the plan could not name it: "confirm ConfigMap <name> is the
        # affected resource" asks the operator to confirm a placeholder.
        target = plan.target if plan and not plan.target.name.startswith("<") else hypothesis.target
        where = f"{target.namespace}/{target.name}" if target.namespace else target.name
        return [
            f"Confirm {target.kind} {where} is the affected resource before changing it.",
            "Run the read-only checks in the plan and keep their output for rollback review.",
            "Apply the change through source-controlled manifests where possible.",
        ]
