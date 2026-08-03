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

# Container-level reasons, which the API server reports *separately* from the
# pod phase. This distinction is the whole of the bug below: `kubectl get pods`
# prints one merged STATUS column, so `ImagePullBackOff` looks like a pod-level
# status, while the API returns `phase: Pending` plus
# `containerStatuses[].state.waiting.reason: ImagePullBackOff`. Reading the
# phase first discarded the only actionable half.
#
# Deliberately exactly the container reasons `POD_STATUS_SIGNALS` can turn into
# a signal — a reason returned from here that nothing maps produces a pod
# visible in `problematic_pods` and no signal, which is a worse kind of silence
# than not detecting it at all.
CONTAINER_PROBLEM_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "OOMKilled",
        "Error",
        "ContainerCreating",
    }
)


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
        """The most specific reason this pod is unhealthy, or `None`.

        **Container reasons are read before the phase, and the order is the
        point.** A pod that cannot pull its image is `phase: Pending` *and*
        `waiting.reason: ImagePullBackOff`. Both are true; only the second is
        useful. This checked the phase first and returned `Pending`, so the
        reason was never reached — and because `POD_IMAGE_PULL_FAILURE` is one
        of only two triggers for the image hypothesis, a textbook
        ImagePullBackOff produced no hypothesis and no remediation at all.

        It survived a full suite because every fixture supplied `status` as the
        single merged string `kubectl` *prints*, not the phase-plus-reason the
        API server returns — so the fakes encoded the same misunderstanding as
        the code and could not fail. Found against a real cluster; see
        `docs/QA_AUDIT_2026-08-03.md`. The regression fixtures are captured
        API-server payloads for exactly that reason.

        Only pods whose phase happened *not* to be a problem status ever
        reached the loop, which is why crash-loop detection (`phase: Running`)
        looked healthy and hid the hole.
        """
        status = pod.get("status", {})

        for container_status in status.get("containerStatuses", []) or []:
            state = container_status.get("state") or {}
            last_state = container_status.get("lastState") or {}
            # Current state before historical: a container waiting on a bad
            # image now matters more than one that exited an hour ago.
            for reason in (
                (state.get("waiting") or {}).get("reason"),
                (state.get("terminated") or {}).get("reason"),
                (last_state.get("terminated") or {}).get("reason"),
            ):
                if reason in CONTAINER_PROBLEM_REASONS:
                    return reason

        # Only now the phase, which is what `Pending` on an unscheduled pod
        # legitimately is: there are no container statuses to be more specific
        # with, because no kubelet has accepted it yet.
        phase = status.get("phase")
        if phase in PROBLEM_STATUSES:
            return phase

        for condition in status.get("conditions", []):
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return condition.get("reason", "Pending")

        return None
