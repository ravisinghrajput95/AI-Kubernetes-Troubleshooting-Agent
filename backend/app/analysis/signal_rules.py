"""Rules that turn collected evidence into deterministic signals.

Every rule is independent and pluggable; the engine simply runs all registered
rules. Adding a new observation means adding a rule, not editing a dispatcher.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.analysis.models import Severity, Signal, SignalType
from app.evidence.models import EvidenceKind, ResourceRef

OOM_LOG_MARKERS = ("oomkilled", "out of memory", "outofmemory", "cannot allocate memory")


@dataclass
class AnalysisInput:
    """Read-only view of an investigation, with evidence provenance lookup."""

    investigation: dict[str, Any]
    _evidence_by_kind: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for entry in self.investigation.get("evidence", []) or []:
            kind = entry.get("kind")
            evidence_id = entry.get("id")
            if kind and evidence_id:
                self._evidence_by_kind.setdefault(kind, []).append(evidence_id)

    def section(self, key: str) -> dict[str, Any]:
        value = self.investigation.get(key)
        return value if isinstance(value, dict) else {}

    def deep(self, kind: str) -> list[dict[str, Any]]:
        """Targeted evidence records of `kind` collected by playbooks.

        Each entry carries its own evidence id, so a signal derived from one
        pod's deep evidence cites that pod's record rather than the kind.
        """
        deep_evidence = self.investigation.get("deep_evidence", {})
        if not isinstance(deep_evidence, dict):
            return []

        return [
            entry
            for entry in deep_evidence.get(kind, [])
            if isinstance(entry, dict) and entry.get("data") is not None
        ]

    def evidence_for(self, kind: str, section: str) -> tuple[str, ...]:
        """Evidence ids backing `kind`.

        Falls back to a section reference when no evidence index is present,
        which keeps provenance non-empty and honest for investigations produced
        before the evidence layer existed.
        """
        ids = self._evidence_by_kind.get(kind)
        if ids:
            return tuple(ids)
        return (f"investigation.{section}",)


class SignalRule(Protocol):
    id: str

    def extract(self, data: AnalysisInput) -> Sequence[Signal]: ...


def _pod_ref(namespace: str, name: str) -> ResourceRef:
    return ResourceRef(kind="Pod", name=name, namespace=namespace)


POD_STATUS_SIGNALS: dict[str, tuple[str, Severity]] = {
    "CrashLoopBackOff": (SignalType.POD_CRASH_LOOP, Severity.CRITICAL),
    "ImagePullBackOff": (SignalType.POD_IMAGE_PULL_FAILURE, Severity.CRITICAL),
    "ErrImagePull": (SignalType.POD_IMAGE_PULL_FAILURE, Severity.CRITICAL),
    # A malformed reference and a `Never` pull policy with no local image. Both
    # are image faults the kubelet rejects without ever reaching a registry, so
    # they share the signal and differ in their remediation.
    "InvalidImageName": (SignalType.POD_IMAGE_PULL_FAILURE, Severity.CRITICAL),
    "ErrImageNeverPull": (SignalType.POD_IMAGE_PULL_FAILURE, Severity.CRITICAL),
    "CreateContainerConfigError": (SignalType.POD_CONFIG_ERROR, Severity.CRITICAL),
    "CreateContainerError": (SignalType.POD_CONFIG_ERROR, Severity.CRITICAL),
    "OOMKilled": (SignalType.POD_OOM_KILLED, Severity.CRITICAL),
    "Pending": (SignalType.POD_PENDING, Severity.HIGH),
    "Unschedulable": (SignalType.POD_PENDING, Severity.HIGH),
    "Error": (SignalType.POD_ERROR, Severity.HIGH),
    "Failed": (SignalType.POD_ERROR, Severity.HIGH),
    # The node reclaimed resources. HIGH rather than CRITICAL because the
    # workload itself did nothing wrong and is usually rescheduled; what needs
    # attention is the node, which `node.unhealthy` covers.
    "Evicted": (SignalType.POD_EVICTED, Severity.HIGH),
    "ContainerCreating": (SignalType.POD_STUCK_CREATING, Severity.MEDIUM),
    # MEDIUM on purpose: with no clock available a pod that is merely still
    # starting looks the same as one that will never be ready. See
    # `pod_inspector._detect_pod_status`.
    "NotReady": (SignalType.POD_NOT_READY, Severity.MEDIUM),
}


class PodStatusRule:
    id = "pod.status"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.PODS, "pods")
        signals = []

        for pod in data.section("pods").get("problematic_pods", []):
            status = pod.get("status", "")
            mapped = POD_STATUS_SIGNALS.get(status)
            if mapped is None:
                continue

            signal_type, severity = mapped
            namespace = pod.get("namespace", "default")
            name = pod.get("name", "unknown")
            signals.append(
                Signal.create(
                    signal_type,
                    severity,
                    f"Pod {namespace}/{name} is in {status}.",
                    _pod_ref(namespace, name),
                    evidence,
                    {"status": status, "namespace": namespace, "pod": name},
                )
            )

        return signals


class PodLogRule:
    id = "pod.logs"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.POD_LOGS, "logs")
        signals = []

        for entry in data.section("logs").get("logs", []):
            lines = entry.get("relevant_lines") or []
            if not lines:
                continue

            namespace = entry.get("namespace", "default")
            name = entry.get("name", "unknown")
            target = _pod_ref(namespace, name)
            sample = [str(line) for line in lines[:5]]

            signals.append(
                Signal.create(
                    SignalType.LOGS_ERROR_PATTERN,
                    Severity.HIGH,
                    f"Pod {namespace}/{name} logs contain {len(lines)} failure line(s).",
                    target,
                    evidence,
                    {"sample_lines": sample, "line_count": len(lines)},
                )
            )

            joined = " ".join(sample).lower()
            if any(marker in joined for marker in OOM_LOG_MARKERS):
                signals.append(
                    Signal.create(
                        SignalType.LOGS_OOM_PATTERN,
                        Severity.CRITICAL,
                        f"Pod {namespace}/{name} logs indicate an out-of-memory condition.",
                        target,
                        evidence,
                        {"sample_lines": sample},
                    )
                )

        return signals


EVENT_REASON_SIGNALS: dict[str, tuple[str, Severity]] = {
    "FailedScheduling": (SignalType.EVENT_SCHEDULING_FAILURE, Severity.HIGH),
    "FailedPull": (SignalType.EVENT_IMAGE_PULL_FAILURE, Severity.HIGH),
    "ErrImagePull": (SignalType.EVENT_IMAGE_PULL_FAILURE, Severity.HIGH),
    "FailedMount": (SignalType.EVENT_MOUNT_FAILURE, Severity.HIGH),
    # Kept apart from `FailedMount`: the volume exists and is bound, but is
    # still attached elsewhere (the "Multi-Attach" case) or the attach itself
    # failed. Telling an operator their claim is unbound when it is bound and
    # busy sends them to the wrong resource entirely.
    "FailedAttachVolume": (SignalType.STORAGE_ATTACH_FAILURE, Severity.HIGH),
    "FailedDetachVolume": (SignalType.STORAGE_ATTACH_FAILURE, Severity.HIGH),
    "Unhealthy": (SignalType.EVENT_PROBE_FAILURE, Severity.MEDIUM),
    "BackOff": (SignalType.EVENT_BACKOFF, Severity.HIGH),
}


class EventRule:
    id = "event.findings"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.EVENTS, "events")
        signals = []

        for finding in data.section("events").get("findings", []):
            reason = finding.get("reason", "")
            signal_type, severity = EVENT_REASON_SIGNALS.get(
                reason, (SignalType.EVENT_WARNING, Severity.LOW)
            )

            namespace = finding.get("namespace", "default")
            kind, _, name = str(finding.get("object", "Object/unknown")).partition("/")
            signals.append(
                Signal.create(
                    signal_type,
                    severity,
                    f"Event {reason or 'Warning'} on {kind}/{name}: "
                    f"{finding.get('message', '')[:180]}",
                    ResourceRef(kind=kind or "Object", name=name or "unknown", namespace=namespace),
                    evidence,
                    {
                        "reason": reason,
                        "message": finding.get("message", ""),
                        "type": finding.get("type", ""),
                    },
                )
            )

        return signals


class DeploymentRule:
    id = "deployment.health"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.DEPLOYMENTS, "deployments")
        signals = []

        for deployment in data.section("deployments").get("unhealthy_deployments", []):
            namespace = deployment.get("namespace", "default")
            name = deployment.get("name", "unknown")
            target = ResourceRef(kind="Deployment", name=name, namespace=namespace)
            desired = deployment.get("desired_replicas", 0)
            available = deployment.get("available_replicas", 0)

            signals.append(
                Signal.create(
                    SignalType.DEPLOYMENT_UNAVAILABLE,
                    Severity.HIGH,
                    f"Deployment {namespace}/{name} has {available}/{desired} replicas available.",
                    target,
                    evidence,
                    {"desired": desired, "available": available},
                )
            )

            reasons = {
                condition.get("reason", "") for condition in deployment.get("conditions", [])
            }
            if reasons & {"ProgressDeadlineExceeded", "FailedCreate"}:
                signals.append(
                    Signal.create(
                        SignalType.DEPLOYMENT_PROGRESS_STALLED,
                        Severity.HIGH,
                        f"Deployment {namespace}/{name} rollout has stalled.",
                        target,
                        evidence,
                        {"reasons": sorted(reasons)},
                    )
                )

        return signals


class NetworkRule:
    id = "network.findings"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.NETWORK, "network")
        signals = []

        for finding in data.section("network").get("findings", []):
            issue = str(finding.get("issue", ""))
            namespace = finding.get("namespace", "default")
            service = finding.get("service", "unknown")
            target = ResourceRef(kind="Service", name=service, namespace=namespace)

            if "no ready endpoints" in issue:
                signal_type, severity = SignalType.NETWORK_NO_ENDPOINTS, Severity.CRITICAL
            elif "no selector" in issue:
                signal_type, severity = SignalType.NETWORK_NO_SELECTOR, Severity.MEDIUM
            elif "DNS" in issue:
                signal_type, severity = SignalType.NETWORK_DNS_MISSING, Severity.CRITICAL
            else:
                continue

            signals.append(
                Signal.create(
                    signal_type,
                    severity,
                    f"Service {namespace}/{service}: {issue}",
                    target,
                    evidence,
                    {"issue": issue},
                )
            )

        return signals


NODE_PRESSURE_TYPES = {"MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}


class NodeRule:
    id = "node.conditions"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.NODES, "nodes")
        signals = []

        for finding in data.section("nodes").get("findings", []):
            node = finding.get("node", "unknown")
            condition_type = finding.get("type", "")
            target = ResourceRef(kind="Node", name=node)

            if condition_type == "Ready":
                signal_type, severity = SignalType.NODE_NOT_READY, Severity.CRITICAL
                summary = f"Node {node} is not Ready ({finding.get('reason', 'unknown')})."
            elif condition_type in NODE_PRESSURE_TYPES:
                signal_type, severity = SignalType.NODE_PRESSURE, Severity.HIGH
                summary = f"Node {node} reports {condition_type}."
            else:
                continue

            signals.append(
                Signal.create(
                    signal_type,
                    severity,
                    summary,
                    target,
                    evidence,
                    {
                        "condition": condition_type,
                        "reason": finding.get("reason", ""),
                        "message": finding.get("message", ""),
                    },
                )
            )

        return signals


class StorageRule:
    id = "storage.findings"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.STORAGE, "storage")
        signals = []

        for finding in data.section("storage").get("findings", []):
            issue = str(finding.get("issue", ""))
            name = finding.get("name", "unknown")

            if "PersistentVolumeClaim" in issue:
                target = ResourceRef(
                    kind="PersistentVolumeClaim",
                    name=name,
                    namespace=finding.get("namespace", "default"),
                )
                signal_type, severity = SignalType.STORAGE_PVC_UNBOUND, Severity.HIGH
                summary = f"PVC {target.namespace}/{name} is {finding.get('phase', 'not bound')}."
            elif "PersistentVolume" in issue:
                target = ResourceRef(kind="PersistentVolume", name=name)
                signal_type, severity = SignalType.STORAGE_PV_UNAVAILABLE, Severity.MEDIUM
                summary = f"PersistentVolume {name} is {finding.get('phase', 'unavailable')}."
            else:
                continue

            signals.append(
                Signal.create(
                    signal_type,
                    severity,
                    summary,
                    target,
                    evidence,
                    {"issue": issue, "phase": finding.get("phase", "")},
                )
            )

        return signals


class WorkloadRule:
    id = "workload.findings"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        evidence = data.evidence_for(EvidenceKind.WORKLOADS, "workloads")
        signals = []

        for finding in data.section("workloads").get("findings", []):
            name = finding.get("name")
            if not name:
                continue

            namespace = finding.get("namespace", "default")
            kind = str(finding.get("kind", "workload")).rstrip("s").capitalize()
            signals.append(
                Signal.create(
                    SignalType.WORKLOAD_DEGRADED,
                    Severity.MEDIUM,
                    f"{kind} {namespace}/{name}: {finding.get('issue', 'is degraded')}",
                    ResourceRef(kind=kind, name=name, namespace=namespace),
                    evidence,
                    {
                        "issue": finding.get("issue", ""),
                        "ready": finding.get("ready"),
                        "desired": finding.get("desired"),
                    },
                )
            )

        return signals


class ResourceLimitsRule:
    """Missing limits is weak alone, but materially supports an OOM hypothesis."""

    id = "container.limits"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        findings = data.section("security").get("findings", [])
        limits = next(
            (item for item in findings if item.get("label") == "Missing Resource Limits"),
            None,
        )
        if limits is None or limits.get("status") != "warning":
            return []

        return [
            Signal.create(
                SignalType.CONTAINER_MISSING_LIMITS,
                Severity.LOW,
                limits.get("detail", "Containers are missing resource limits."),
                ResourceRef.cluster(data.investigation.get("context")),
                data.evidence_for(EvidenceKind.PODS, "security"),
                {"detail": limits.get("detail", "")},
            )
        ]


DEFAULT_SIGNAL_RULES: tuple[SignalRule, ...] = (
    PodStatusRule(),
    PodLogRule(),
    EventRule(),
    DeploymentRule(),
    NetworkRule(),
    NodeRule(),
    StorageRule(),
    WorkloadRule(),
    ResourceLimitsRule(),
)
