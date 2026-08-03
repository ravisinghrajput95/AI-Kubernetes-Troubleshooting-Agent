from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.evidence.models import ResourceRef


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def weight(self) -> int:
        return _SEVERITY_WEIGHTS[self]


_SEVERITY_WEIGHTS = {
    Severity.CRITICAL: 5,
    Severity.HIGH: 4,
    Severity.MEDIUM: 3,
    Severity.LOW: 2,
    Severity.INFO: 1,
}


class SignalType:
    """Namespaced signal type identifiers, grouped by domain prefix."""

    POD_CRASH_LOOP = "pod.crash_loop"
    POD_IMAGE_PULL_FAILURE = "pod.image_pull_failure"
    POD_OOM_KILLED = "pod.oom_killed"
    POD_PENDING = "pod.pending"
    POD_STUCK_CREATING = "pod.stuck_creating"
    POD_ERROR = "pod.error"
    # A ConfigMap, Secret or key the container references does not exist. The
    # deep equivalents (`CONFIG_REFERENCE_MISSING`) say *which* one and need a
    # playbook round; this is the same fault visible in baseline collection.
    POD_CONFIG_ERROR = "pod.config_error"
    # The kubelet removed the pod, usually under node pressure. Not a failure
    # of the workload, which is why it reads differently from `POD_ERROR`.
    POD_EVICTED = "pod.evicted"
    # Running, every container healthy, and never Ready. Emitted at MEDIUM and
    # used as corroboration rather than as a conclusion — see
    # `pod_inspector._detect_pod_status` for why it carries no grace period.
    POD_NOT_READY = "pod.not_ready"

    LOGS_ERROR_PATTERN = "logs.error_pattern"
    LOGS_OOM_PATTERN = "logs.oom_pattern"

    EVENT_SCHEDULING_FAILURE = "event.scheduling_failure"
    EVENT_IMAGE_PULL_FAILURE = "event.image_pull_failure"
    EVENT_MOUNT_FAILURE = "event.mount_failure"
    EVENT_PROBE_FAILURE = "event.probe_failure"
    EVENT_BACKOFF = "event.backoff"
    EVENT_WARNING = "event.warning"

    DEPLOYMENT_UNAVAILABLE = "deployment.unavailable_replicas"
    DEPLOYMENT_PROGRESS_STALLED = "deployment.progress_stalled"

    NETWORK_NO_ENDPOINTS = "network.no_endpoints"
    NETWORK_NO_SELECTOR = "network.no_selector"
    NETWORK_DNS_MISSING = "network.dns_missing"

    NODE_NOT_READY = "node.not_ready"
    NODE_PRESSURE = "node.pressure"

    STORAGE_PVC_UNBOUND = "storage.pvc_unbound"
    STORAGE_PV_UNAVAILABLE = "storage.pv_unavailable"
    # The claim is bound and the volume still will not attach — most often a
    # ReadWriteOnce disk still held by a pod on another node. A different fault
    # from `STORAGE_PVC_UNBOUND` with a different fix, and it was previously
    # recorded as a generic warning.
    STORAGE_ATTACH_FAILURE = "storage.attach_failure"

    WORKLOAD_DEGRADED = "workload.degraded"

    CONTAINER_MISSING_LIMITS = "container.missing_limits"

    # Graph signals (M7). These are not observations of one section — each is
    # a path through the dependency graph, and none is reachable without it.
    POD_BLOCKED_BY_STORAGE = "graph.pod_blocked_by_storage"
    STORAGE_CLASS_BLOCKING = "graph.storage_class_blocking"
    NODE_CARRYING_FAILURES = "graph.node_carrying_failures"
    SERVICE_BACKENDS_FAILING = "graph.service_backends_failing"

    # Signals that only become available once a playbook has collected
    # targeted evidence.
    CONTAINER_OOM_EXIT = "container.oom_exit_code"
    CONTAINER_NONZERO_EXIT = "container.nonzero_exit_code"
    CONTAINER_NO_MEMORY_LIMIT = "container.no_memory_limit"
    CONFIG_REFERENCE_MISSING = "config.reference_missing"
    CONFIG_KEY_MISSING = "config.key_missing"
    PROBE_AGGRESSIVE = "probe.aggressive_timing"
    SCHEDULING_INSUFFICIENT_RESOURCES = "scheduling.insufficient_resources"
    SCHEDULING_TAINT_BLOCKED = "scheduling.taint_blocked"
    QUOTA_EXCEEDED = "quota.exceeded"
    IMAGE_PULL_UNAUTHORIZED = "image.pull_unauthorized"
    IMAGE_NOT_FOUND = "image.not_found"
    IMAGE_NO_PULL_SECRET = "image.no_pull_secret"
    STORAGE_NO_DEFAULT_CLASS = "storage.no_default_class"
    STORAGE_WAIT_FOR_CONSUMER = "storage.wait_for_first_consumer"
    NETWORK_POLICY_DENIES_ALL = "network.policy_denies_all"
    DNS_WORKLOAD_UNHEALTHY = "network.dns_workload_unhealthy"

    # Signals from optional observability backends.
    METRICS_MEMORY_NEAR_LIMIT = "metrics.memory_near_limit"
    METRICS_MEMORY_PEAKED_AT_LIMIT = "metrics.memory_peaked_at_limit"
    METRICS_CPU_THROTTLED = "metrics.cpu_throttled"
    METRICS_RESTART_RATE = "metrics.restart_rate"
    METRICS_NODE_OVERCOMMITTED = "metrics.node_overcommitted"
    LOGS_HISTORICAL_ERRORS = "logs.historical_errors"


@dataclass(frozen=True, slots=True)
class Signal:
    """A deterministic, evidence-backed observation.

    Signals are produced only by rules over collected evidence — never by a
    language model. `evidence_ids` is mandatory: a signal that cannot name the
    evidence it came from is a bug, not a weak signal.
    """

    id: str
    type: str
    severity: Severity
    summary: str
    target: ResourceRef
    evidence_ids: tuple[str, ...]
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise ValueError(f"Signal {self.id} was created without evidence provenance")

    @property
    def domain(self) -> str:
        return self.type.split(".", 1)[0]

    @classmethod
    def create(
        cls,
        signal_type: str,
        severity: Severity,
        summary: str,
        target: ResourceRef,
        evidence_ids: tuple[str, ...],
        attributes: dict[str, Any] | None = None,
    ) -> "Signal":
        return cls(
            id=f"{signal_type}:{target.key}",
            type=signal_type,
            severity=severity,
            summary=summary,
            target=target,
            evidence_ids=tuple(evidence_ids),
            attributes=attributes or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "domain": self.domain,
            "severity": str(self.severity),
            "summary": self.summary,
            "target": self.target.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "attributes": self.attributes,
        }


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A candidate root cause, scored from the signals that support it."""

    id: str
    title: str
    category: str
    severity: Severity
    confidence: int
    rationale: str
    target: ResourceRef
    supporting_signal_ids: tuple[str, ...]
    refuting_signal_ids: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    remediation_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "severity": str(self.severity),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "target": self.target.to_dict(),
            "supporting_signals": list(self.supporting_signal_ids),
            "refuting_signals": list(self.refuting_signal_ids),
            "missing_evidence": list(self.missing_evidence),
            "remediation_hint": self.remediation_hint,
        }


@dataclass(frozen=True)
class AnalysisResult:
    """Signals and ranked hypotheses derived from one investigation."""

    signals: tuple[Signal, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()

    @property
    def signal_ids(self) -> frozenset[str]:
        return frozenset(signal.id for signal in self.signals)

    @property
    def hypothesis_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.hypotheses)

    @property
    def top_hypothesis(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    def signal(self, signal_id: str) -> Signal | None:
        return next((item for item in self.signals if item.id == signal_id), None)

    def hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        return next((item for item in self.hypotheses if item.id == hypothesis_id), None)

    def by_type(self, signal_type: str) -> tuple[Signal, ...]:
        return tuple(item for item in self.signals if item.type == signal_type)

    def evidence_ids_for(self, signal_ids: tuple[str, ...]) -> list[str]:
        """Evidence backing a set of signals, de-duplicated and ordered."""
        seen: list[str] = []
        for signal_id in signal_ids:
            signal = self.signal(signal_id)
            if signal is None:
                continue
            for evidence_id in signal.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": [signal.to_dict() for signal in self.signals],
            "hypotheses": [item.to_dict() for item in self.hypotheses],
        }
