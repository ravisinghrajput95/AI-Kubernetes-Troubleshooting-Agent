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
    # The catch-all, and only ever reached when no container explained itself.
    # A pod the control plane gave up on usually *does* have a container reason
    # (a failed Job pod terminates with `Error`), but one removed out from
    # under the kubelet may carry nothing else at all — and phase `Failed` is
    # unambiguous, so ignoring it left those invisible.
    "Failed",
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
        # The remaining two image reasons. `InvalidImageName` is a malformed
        # reference the kubelet rejects without contacting a registry;
        # `ErrImageNeverPull` is `imagePullPolicy: Never` with no local image.
        # Both are image faults with different fixes from a failed pull, and
        # both were previously reported as `Pending`.
        "InvalidImageName",
        "ErrImageNeverPull",
        # A ConfigMap, Secret or key the container references does not exist.
        # This is the `notifier` case from the audit: distinct from an image
        # fault, and previously flattened to `Pending` with no signal of its
        # own — the platform could only reach it through a playbook round.
        "CreateContainerConfigError",
        "CreateContainerError",
        "OOMKilled",
        "Error",
        "ContainerCreating",
    }
)

# A pod the kubelet removed rather than one that failed on its own. Reported at
# the *pod* level (`status.reason`), not on any container, so it needs its own
# check — an evicted pod's containers often report nothing useful at all.
EVICTED = "Evicted"

# How much each reason explains, most explanatory first. The candidates found
# across a pod's containers are ranked by this rather than by which field they
# came from, and that ordering does two things a field order cannot.
#
# **`OOMKilled` outranks `CrashLoopBackOff`.** A container being killed for
# memory reports `waiting.reason: CrashLoopBackOff` *and*
# `lastState.terminated.reason: OOMKilled` at the same time. Reading the
# waiting state first — which is otherwise the right instinct — returned the
# loop and discarded the cause, so `POD_OOM_KILLED` could not fire for the
# failure it exists to name. The loop is the symptom; the kill is the finding.
#
# **It also makes repeat investigations agree.** A crash-looping container
# alternates between running and backing off, so a point-in-time read caught
# `Error` at one instant and `CrashLoopBackOff` a minute later — the audit saw
# the same cluster produce two different root causes on consecutive runs.
# Ranking a *set* of candidates is stable where "first field that matched" is
# not; `_restart_candidate` below supplies the missing member of that set.
REASON_PRECEDENCE = (
    "OOMKilled",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
    "ErrImageNeverPull",
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "Error",
    "ContainerCreating",
)

# Restarts before a container counts as looping even when caught mid-run. Two
# rather than one because a single restart is a recoverable blip, and this
# turns into a `CrashLoopBackOff` claim.
RESTART_THRESHOLD = 2

# `phase: Running` with `Ready: False`. The workload is up and serving nothing,
# which is what a Service with no endpoints is usually made of.
NOT_READY = "NotReady"


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

    def _restart_candidate(self, container_status: dict[str, Any]) -> str | None:
        """`CrashLoopBackOff` for a container that has been restarting.

        A crash-looping container spends part of its cycle *running*, and at
        that instant nothing reports the loop: `waiting` is empty and the only
        trace is `lastState.terminated.reason: Error`. Half of the reads
        therefore called it `Error` and half called it `CrashLoopBackOff`, which
        is how the audit saw one unchanged cluster produce two different root
        causes on consecutive investigations.

        The restart count is the fact that does not oscillate, so it supplies
        the loop as a candidate at every instant and precedence picks between
        that and whatever else was observed. A container that exited cleanly is
        excluded — completed init containers and finished jobs restart
        legitimately.
        """
        if container_status.get("restartCount", 0) < RESTART_THRESHOLD:
            return None
        terminated = (container_status.get("lastState") or {}).get("terminated") or {}
        if not terminated or terminated.get("reason") == "Completed":
            return None
        return "CrashLoopBackOff"

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

        # Eviction first: the kubelet removed this pod, and its containers
        # frequently report nothing that would explain why.
        if status.get("reason") == EVICTED:
            return EVICTED

        # Init containers before app containers. An init container that cannot
        # start blocks every container behind it, so its reason is the
        # actionable one — the app containers are merely waiting, and reporting
        # `PodInitializing` would name the symptom. These were not read at all
        # before, so such a pod reported the bare phase.
        candidates = set()
        for container_status in [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]:
            state = container_status.get("state") or {}
            last_state = container_status.get("lastState") or {}
            for reason in (
                (state.get("waiting") or {}).get("reason"),
                (state.get("terminated") or {}).get("reason"),
                (last_state.get("terminated") or {}).get("reason"),
                self._restart_candidate(container_status),
            ):
                if reason in CONTAINER_PROBLEM_REASONS:
                    candidates.add(reason)

        if candidates:
            return min(candidates, key=REASON_PRECEDENCE.index)

        # Only now the phase, which is what `Pending` on an unscheduled pod
        # legitimately is: there are no container statuses to be more specific
        # with, because no kubelet has accepted it yet.
        phase = status.get("phase")
        if phase in PROBLEM_STATUSES:
            return phase

        conditions = status.get("conditions", []) or []
        for condition in conditions:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return condition.get("reason", "Pending")

        # Running, nothing wrong with any container, and still not Ready. The
        # `gateway` case from the audit: a readiness probe that never passes, so
        # the pod serves nothing and its Service has no endpoints — while every
        # container reports healthy. It was detected only through the kubelet's
        # `Unhealthy` event, and Kubernetes expires events after an hour, after
        # which a permanently broken workload looked entirely healthy here.
        #
        # **Deliberately no grace period, because there is deliberately no
        # clock.** `analyse()` is pure so that a stored report can be
        # re-derived from its evidence; reading the wall clock here would make
        # the same evidence produce different output on every run. The cost is
        # that a pod still starting reports too, so this is emitted at MEDIUM
        # and is wired to *corroborate* the probe and endpoint hypotheses
        # rather than to conclude anything on its own.
        if phase == "Running":
            ready = next((c for c in conditions if c.get("type") == "Ready"), None)
            if ready is not None and ready.get("status") == "False":
                return NOT_READY

        return None
