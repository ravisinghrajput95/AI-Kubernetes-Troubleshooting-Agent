"""Declarative hypothesis rules.

Rules are data, not branching logic: each states which signal types trigger it,
which strengthen it, and which argue against it. Adding a failure mode means
adding a rule to the tuple below.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.analysis.models import Hypothesis, Signal, SignalType

SUPPORT_BONUS = 10
REFUTE_PENALTY = 20
MAX_CONFIDENCE = 92
MIN_CONFIDENCE = 5


class HypothesisRule(Protocol):
    id: str

    def evaluate(self, signals: Sequence[Signal]) -> Hypothesis | None: ...


@dataclass(frozen=True)
class SignalPatternRule:
    """Hypothesis triggered by the presence of any of a set of signal types."""

    id: str
    title: str
    category: str
    rationale: str
    triggers: frozenset[str]
    supporting: frozenset[str] = frozenset()
    refuting: frozenset[str] = frozenset()
    missing_evidence: tuple[str, ...] = ()
    remediation_hint: str = ""
    base_confidence: int = 50

    def evaluate(self, signals: Sequence[Signal]) -> Hypothesis | None:
        triggering = [signal for signal in signals if signal.type in self.triggers]
        if not triggering:
            return None

        supporting = [signal for signal in signals if signal.type in self.supporting]
        refuting = [signal for signal in signals if signal.type in self.refuting]

        confidence = self.base_confidence
        confidence += SUPPORT_BONUS * len({signal.type for signal in supporting})
        confidence -= REFUTE_PENALTY * len({signal.type for signal in refuting})
        confidence = max(MIN_CONFIDENCE, min(confidence, MAX_CONFIDENCE))

        primary = max(triggering, key=lambda signal: signal.severity.weight)
        severity = max(
            (signal.severity for signal in triggering),
            key=lambda value: value.weight,
        )

        return Hypothesis(
            id=self.id,
            title=self.title,
            category=self.category,
            severity=severity,
            confidence=confidence,
            rationale=self.rationale,
            target=primary.target,
            supporting_signal_ids=tuple(signal.id for signal in [*triggering, *supporting]),
            refuting_signal_ids=tuple(signal.id for signal in refuting),
            missing_evidence=self.missing_evidence,
            remediation_hint=self.remediation_hint,
        )


DEFAULT_HYPOTHESIS_RULES: tuple[HypothesisRule, ...] = (
    SignalPatternRule(
        id="workload.out_of_memory",
        title="Container terminated for exceeding its memory limit",
        category="workload",
        rationale=(
            "An OOM termination was observed. The container requested more memory than "
            "its limit allows, or the limit is set below the application's working set."
        ),
        triggers=frozenset(
            {
                SignalType.POD_OOM_KILLED,
                SignalType.LOGS_OOM_PATTERN,
                SignalType.CONTAINER_OOM_EXIT,
            }
        ),
        supporting=frozenset(
            {
                SignalType.CONTAINER_MISSING_LIMITS,
                SignalType.CONTAINER_NO_MEMORY_LIMIT,
                SignalType.POD_CRASH_LOOP,
                SignalType.NODE_PRESSURE,
                SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT,
                SignalType.METRICS_MEMORY_NEAR_LIMIT,
                SignalType.METRICS_RESTART_RATE,
            }
        ),
        missing_evidence=(
            "Container memory limit and request values",
            "Container exit code (137 confirms an OOM kill)",
            "Memory working-set trend leading up to the termination",
        ),
        remediation_hint="Raise the container memory limit or reduce the workload's memory usage.",
        base_confidence=65,
    ),
    SignalPatternRule(
        id="image.pull_failure",
        title="Container image cannot be pulled",
        category="image",
        rationale=(
            "The kubelet could not pull the container image. This is typically a wrong "
            "image name or tag, a missing imagePullSecret, or an unreachable registry."
        ),
        triggers=frozenset(
            {SignalType.POD_IMAGE_PULL_FAILURE, SignalType.EVENT_IMAGE_PULL_FAILURE}
        ),
        supporting=frozenset(
            {
                SignalType.EVENT_BACKOFF,
                SignalType.DEPLOYMENT_UNAVAILABLE,
                SignalType.IMAGE_NOT_FOUND,
                SignalType.IMAGE_PULL_UNAUTHORIZED,
                SignalType.IMAGE_NO_PULL_SECRET,
            }
        ),
        missing_evidence=(
            "imagePullSecrets referenced by the pod spec",
            "Whether the image tag exists in the registry",
            "Registry authentication and reachability from the node",
        ),
        remediation_hint="Correct the image reference or attach a valid imagePullSecret.",
        base_confidence=70,
    ),
    SignalPatternRule(
        id="workload.application_startup_failure",
        title="Application fails on startup and restarts repeatedly",
        category="workload",
        rationale=(
            "The container starts and exits repeatedly. The cause is usually inside the "
            "application: a missing dependency, bad configuration, or a failing probe."
        ),
        triggers=frozenset({SignalType.POD_CRASH_LOOP, SignalType.EVENT_BACKOFF}),
        supporting=frozenset(
            {
                SignalType.LOGS_ERROR_PATTERN,
                SignalType.EVENT_PROBE_FAILURE,
                SignalType.DEPLOYMENT_UNAVAILABLE,
                SignalType.CONTAINER_NONZERO_EXIT,
                SignalType.LOGS_HISTORICAL_ERRORS,
            }
        ),
        refuting=frozenset(
            {
                SignalType.POD_IMAGE_PULL_FAILURE,
                SignalType.POD_OOM_KILLED,
                SignalType.CONTAINER_OOM_EXIT,
                SignalType.CONFIG_REFERENCE_MISSING,
                SignalType.CONFIG_KEY_MISSING,
            }
        ),
        missing_evidence=(
            "Previous container logs (--previous) from before the last restart",
            "Liveness, readiness, and startup probe definitions",
            "ConfigMap and Secret keys referenced by the container",
            "Container exit code and termination reason",
        ),
        remediation_hint="Fix the failing startup dependency or configuration, then restart.",
        base_confidence=55,
    ),
    SignalPatternRule(
        id="scheduling.unschedulable",
        title="Pods cannot be scheduled onto any node",
        category="scheduling",
        rationale=(
            "The scheduler could not place the pod. Typical causes are insufficient "
            "allocatable capacity, node taints without matching tolerations, or an "
            "unbound volume the pod depends on."
        ),
        triggers=frozenset({SignalType.POD_PENDING, SignalType.EVENT_SCHEDULING_FAILURE}),
        supporting=frozenset(
            {
                SignalType.NODE_PRESSURE,
                SignalType.NODE_NOT_READY,
                SignalType.STORAGE_PVC_UNBOUND,
                SignalType.METRICS_NODE_OVERCOMMITTED,
            }
        ),
        missing_evidence=(
            "Node taints versus pod tolerations",
            "Node allocatable capacity versus pod resource requests",
            "ResourceQuota and LimitRange in the namespace",
            "Node affinity and selector constraints",
        ),
        remediation_hint="Add capacity, relax the constraint, or tolerate the node taint.",
        base_confidence=60,
    ),
    SignalPatternRule(
        id="storage.volume_unavailable",
        title="Pod cannot mount its persistent volume",
        category="storage",
        rationale=(
            "A volume the pod requires is unbound or failed to mount, so the pod cannot "
            "start. The claim, the provisioner, or the CSI driver is the likely cause."
        ),
        triggers=frozenset({SignalType.EVENT_MOUNT_FAILURE, SignalType.STORAGE_PVC_UNBOUND}),
        supporting=frozenset({SignalType.STORAGE_PV_UNAVAILABLE, SignalType.POD_STUCK_CREATING}),
        missing_evidence=(
            "StorageClass and its provisioner",
            "CSI driver pod health",
            "VolumeAttachment state for the target node",
            "Access mode compatibility between claim and volume",
        ),
        remediation_hint="Resolve the claim binding or repair the storage provisioner.",
        base_confidence=60,
    ),
    SignalPatternRule(
        id="network.service_without_endpoints",
        title="Service has no ready endpoints",
        category="network",
        rationale=(
            "The service resolves but routes to no ready pods. Either the selector does "
            "not match any pod labels, or the matching pods are not passing readiness."
        ),
        triggers=frozenset({SignalType.NETWORK_NO_ENDPOINTS}),
        supporting=frozenset(
            {
                SignalType.POD_CRASH_LOOP,
                SignalType.POD_PENDING,
                SignalType.POD_NOT_READY,
                SignalType.EVENT_PROBE_FAILURE,
                SignalType.DEPLOYMENT_UNAVAILABLE,
            }
        ),
        # Refuted by the graph having answered the question. This rule offers
        # two causes — a selector that matches nothing, or pods that are not
        # ready — and `service_backends_failing` rules the first one out by
        # showing the selector matches pods that exist. Leaving both at full
        # confidence would present a symptom and its explanation as competing
        # theories.
        refuting=frozenset({SignalType.SERVICE_BACKENDS_FAILING}),
        missing_evidence=(
            "Service selector versus actual pod labels",
            "EndpointSlice contents for the service",
            "Readiness state of the pods the selector should match",
        ),
        remediation_hint="Align the service selector with pod labels, or fix pod readiness.",
        base_confidence=60,
    ),
    SignalPatternRule(
        id="network.cluster_dns_unavailable",
        title="Cluster DNS is unavailable",
        category="network",
        rationale=(
            "The cluster DNS service is missing. Name resolution will fail across all "
            "workloads, which frequently presents as widespread connection errors."
        ),
        triggers=frozenset({SignalType.NETWORK_DNS_MISSING}),
        supporting=frozenset({SignalType.LOGS_ERROR_PATTERN}),
        missing_evidence=(
            "CoreDNS deployment and pod health in kube-system",
            "kube-dns service and endpoints",
            "Pod resolv.conf contents",
        ),
        remediation_hint="Restore the CoreDNS deployment and its service.",
        base_confidence=65,
    ),
    SignalPatternRule(
        id="node.unhealthy",
        title="Node is unhealthy and affecting workload placement",
        category="node",
        rationale=(
            "One or more nodes are not Ready or are reporting resource pressure, which "
            "evicts or blocks pods scheduled to them."
        ),
        # Eviction triggers as well as the node conditions. A pod evicted under
        # memory pressure is often the first thing an operator sees, and the
        # node condition that caused it may already have cleared by the time
        # anyone looks — leaving the eviction as the only remaining evidence.
        triggers=frozenset(
            {SignalType.NODE_NOT_READY, SignalType.NODE_PRESSURE, SignalType.POD_EVICTED}
        ),
        supporting=frozenset(
            {
                SignalType.POD_PENDING,
                SignalType.EVENT_SCHEDULING_FAILURE,
                SignalType.POD_OOM_KILLED,
            }
        ),
        missing_evidence=(
            "kubelet status and logs on the affected node",
            "Node allocatable capacity versus scheduled requests",
            "Recent node condition transitions",
        ),
        remediation_hint="Recover or cordon the node and reschedule its workloads.",
        base_confidence=60,
    ),
    SignalPatternRule(
        id="rollout.stalled",
        title="Deployment rollout is not progressing",
        category="workload",
        rationale=(
            "The deployment cannot reach its desired replica count. The new pods are "
            "failing to become available, so the rollout is blocked."
        ),
        triggers=frozenset(
            {SignalType.DEPLOYMENT_PROGRESS_STALLED, SignalType.DEPLOYMENT_UNAVAILABLE}
        ),
        supporting=frozenset(
            {
                SignalType.POD_CRASH_LOOP,
                SignalType.POD_IMAGE_PULL_FAILURE,
                SignalType.EVENT_BACKOFF,
            }
        ),
        missing_evidence=(
            "ReplicaSet revision history for the deployment",
            "Rollout status and progress deadline configuration",
        ),
        remediation_hint="Fix the failing pods, or roll back to the last healthy revision.",
        base_confidence=50,
    ),
    # Hypotheses below are only reachable once a playbook has collected
    # targeted evidence; they cannot fire from baseline collection alone.
    SignalPatternRule(
        id="workload.missing_configuration",
        title="Pod references configuration that does not exist",
        category="configuration",
        rationale=(
            "A ConfigMap or Secret the container requires is absent, or exists "
            "without the key the container reads. The container cannot start until "
            "the reference resolves."
        ),
        # `POD_CONFIG_ERROR` is the baseline trigger and the other two are the
        # deep ones. Without it this hypothesis needed a playbook round to
        # exist at all, so a pod plainly reporting
        # `CreateContainerConfigError` produced no configuration hypothesis on
        # the first pass — the deep rules name *which* reference is missing,
        # but the fault is already visible without them.
        triggers=frozenset(
            {
                SignalType.POD_CONFIG_ERROR,
                SignalType.CONFIG_REFERENCE_MISSING,
                SignalType.CONFIG_KEY_MISSING,
            }
        ),
        supporting=frozenset(
            {
                SignalType.POD_CRASH_LOOP,
                SignalType.LOGS_ERROR_PATTERN,
                SignalType.CONTAINER_NONZERO_EXIT,
                SignalType.EVENT_WARNING,
            }
        ),
        missing_evidence=(
            "Whether the reference is marked optional in the pod spec",
            "Which deployment revision introduced the reference",
        ),
        remediation_hint=(
            "Create the missing ConfigMap or Secret key, or correct the reference "
            "in the pod template."
        ),
        base_confidence=80,
    ),
    SignalPatternRule(
        id="scheduling.quota_exhausted",
        title="Namespace quota is preventing admission",
        category="scheduling",
        rationale=(
            "A ResourceQuota in the namespace is fully consumed, so new pods are "
            "rejected before the scheduler considers node capacity."
        ),
        triggers=frozenset({SignalType.QUOTA_EXCEEDED}),
        supporting=frozenset({SignalType.POD_PENDING, SignalType.EVENT_SCHEDULING_FAILURE}),
        missing_evidence=(
            "Which workloads are consuming the quota",
            "Whether the quota or the request should change",
        ),
        remediation_hint="Raise the quota or reduce the namespace's resource requests.",
        base_confidence=75,
    ),
    SignalPatternRule(
        id="storage.volume_attach_blocked",
        title="Volume is bound but cannot be attached to the node",
        category="storage",
        rationale=(
            "The claim is bound, so this is not a provisioning problem. The volume "
            "could not be attached to the node the pod was scheduled onto — most "
            "often a ReadWriteOnce disk still attached to a pod on another node, "
            "which resolves only when the previous pod fully terminates."
        ),
        triggers=frozenset({SignalType.STORAGE_ATTACH_FAILURE}),
        supporting=frozenset(
            {
                SignalType.POD_PENDING,
                SignalType.POD_STUCK_CREATING,
                SignalType.EVENT_MOUNT_FAILURE,
                SignalType.NODE_NOT_READY,
            }
        ),
        missing_evidence=(
            "Which node currently holds the volume attachment",
            "Whether the previous pod using the claim has fully terminated",
            "The claim's access mode and the volume plugin in use",
        ),
        remediation_hint=(
            "Wait for or force detachment from the previous node, or move the "
            "workload to ReadWriteMany if concurrent access is genuinely needed."
        ),
        base_confidence=70,
    ),
    SignalPatternRule(
        id="storage.no_default_storage_class",
        title="Claim cannot bind because no default StorageClass exists",
        category="storage",
        rationale=(
            "The claim does not name a StorageClass and the cluster has no default, "
            "so no provisioner will ever act on it."
        ),
        triggers=frozenset({SignalType.STORAGE_NO_DEFAULT_CLASS}),
        supporting=frozenset({SignalType.STORAGE_PVC_UNBOUND, SignalType.POD_PENDING}),
        missing_evidence=("The storageClassName requested by the claim",),
        remediation_hint=("Mark a StorageClass as default, or set storageClassName on the claim."),
        base_confidence=70,
    ),
    SignalPatternRule(
        id="network.ingress_denied_by_policy",
        title="NetworkPolicy is denying all ingress",
        category="network",
        rationale=(
            "A default-deny NetworkPolicy selects these pods and no rule admits "
            "traffic, so connections are dropped before reaching the container."
        ),
        triggers=frozenset({SignalType.NETWORK_POLICY_DENIES_ALL}),
        supporting=frozenset({SignalType.NETWORK_NO_ENDPOINTS, SignalType.EVENT_PROBE_FAILURE}),
        missing_evidence=(
            "Whether the policy's podSelector matches the affected pods",
            "Which namespaces or pods are expected to reach this service",
        ),
        remediation_hint="Add an ingress rule permitting the expected source traffic.",
        base_confidence=65,
    ),
    SignalPatternRule(
        id="network.dns_workload_degraded",
        title="Cluster DNS pods are not ready",
        category="network",
        rationale=(
            "CoreDNS is deployed but no replica is ready, so name resolution fails "
            "cluster-wide and presents as widespread connection errors."
        ),
        triggers=frozenset({SignalType.DNS_WORKLOAD_UNHEALTHY}),
        supporting=frozenset({SignalType.NETWORK_DNS_MISSING, SignalType.LOGS_ERROR_PATTERN}),
        missing_evidence=(
            "CoreDNS container logs",
            "Node conditions where CoreDNS is scheduled",
        ),
        remediation_hint="Restore CoreDNS replicas to a ready state.",
        base_confidence=75,
    ),
    SignalPatternRule(
        id="probe.failing",
        title="Health probe is failing",
        category="workload",
        rationale=(
            "A liveness or readiness probe is failing. Either the application is genuinely "
            "unhealthy, or the probe's path, port, or timing is misconfigured."
        ),
        # `POD_NOT_READY` triggers as well as `EVENT_PROBE_FAILURE` because
        # Kubernetes expires events after an hour: a permanently failing probe
        # stops producing them, and the pod condition is the only lasting
        # trace. The condition is derived from the pod itself, so it survives
        # as long as the fault does.
        triggers=frozenset({SignalType.EVENT_PROBE_FAILURE, SignalType.POD_NOT_READY}),
        supporting=frozenset(
            {
                SignalType.POD_CRASH_LOOP,
                SignalType.NETWORK_NO_ENDPOINTS,
                SignalType.DEPLOYMENT_UNAVAILABLE,
            }
        ),
        missing_evidence=(
            "Probe definitions including path, port, and thresholds",
            "Direct response from the probe endpoint inside the container",
        ),
        remediation_hint="Correct the probe configuration or fix the unhealthy endpoint.",
        base_confidence=45,
    ),
    # --- graph-aware (M7) ---------------------------------------------------
    #
    # These outrank their single-section equivalents on purpose. "A pod is
    # Pending" and "a claim is unbound" are both true and neither is the root
    # cause; the traversal that joins them is. High base confidence is
    # defensible precisely because a path was walked rather than a coincidence
    # observed — the signal cannot exist unless every edge on it was read from
    # evidence.
    SignalPatternRule(
        id="storage.class_blocking_workloads",
        title="A storage class is blocking several workloads",
        category="storage",
        rationale=(
            "More than one volume claim on the same StorageClass is unbound. The "
            "common factor is the class rather than any single workload, so the "
            "provisioner for that class is the thing to look at."
        ),
        triggers=frozenset({SignalType.STORAGE_CLASS_BLOCKING}),
        supporting=frozenset(
            {
                SignalType.POD_BLOCKED_BY_STORAGE,
                SignalType.STORAGE_PVC_UNBOUND,
                SignalType.POD_PENDING,
            }
        ),
        missing_evidence=("k8s.storageclasses", "k8s.volumeattachments"),
        remediation_hint=(
            "Check the provisioner for that StorageClass, and whether its backing "
            "storage has capacity in the zone the claims are requesting."
        ),
        base_confidence=75,
    ),
    SignalPatternRule(
        id="storage.claim_blocking_pod",
        title="A pod cannot start because the volume it mounts is unbound",
        category="storage",
        rationale=(
            "The pod is Pending and the claim it mounts has not bound. This is one "
            "incident rather than two findings: nothing about the workload needs "
            "changing, and the pod will start when the volume does."
        ),
        triggers=frozenset({SignalType.POD_BLOCKED_BY_STORAGE}),
        supporting=frozenset({SignalType.STORAGE_PVC_UNBOUND, SignalType.POD_PENDING}),
        refuting=frozenset({SignalType.EVENT_SCHEDULING_FAILURE}),
        missing_evidence=("k8s.storageclasses", "k8s.resource.events"),
        remediation_hint=("Bind or reprovision the claim. The workload needs no change."),
        base_confidence=70,
    ),
    SignalPatternRule(
        id="node.hosting_failures",
        title="Failures are concentrated on one node",
        category="infrastructure",
        rationale=(
            "Several failing pods share a node while others elsewhere are healthy. "
            "That points at the node — kubelet, disk, network or an image cache — "
            "rather than at each workload independently."
        ),
        triggers=frozenset({SignalType.NODE_CARRYING_FAILURES}),
        supporting=frozenset(
            {SignalType.NODE_NOT_READY, SignalType.NODE_PRESSURE, SignalType.POD_PENDING}
        ),
        missing_evidence=("k8s.nodes", "k8s.resource.events"),
        remediation_hint=(
            "Inspect that node before the workloads: cordon and drain it if its "
            "conditions confirm the fault."
        ),
        base_confidence=60,
    ),
    SignalPatternRule(
        id="network.backends_all_failing",
        title="A service has no healthy backend",
        category="network",
        rationale=(
            "Every pod the service selects is failing, so the missing endpoints are "
            "a consequence rather than a cause. The service and its selector are "
            "correct; the workload behind it is not."
        ),
        triggers=frozenset({SignalType.SERVICE_BACKENDS_FAILING}),
        supporting=frozenset(
            {
                SignalType.NETWORK_NO_ENDPOINTS,
                SignalType.POD_CRASH_LOOP,
                SignalType.POD_IMAGE_PULL_FAILURE,
            }
        ),
        missing_evidence=("k8s.endpointslices", "k8s.pod.spec"),
        remediation_hint=("Fix the pods the service selects. The service itself needs no change."),
        base_confidence=65,
    ),
)


def rank(hypotheses: Sequence[Hypothesis]) -> tuple[Hypothesis, ...]:
    """Order hypotheses by severity, then confidence, then breadth of support."""
    return tuple(
        sorted(
            hypotheses,
            key=lambda item: (
                item.severity.weight,
                item.confidence,
                len(item.supporting_signal_ids),
                item.id,
            ),
            reverse=True,
        )
    )
