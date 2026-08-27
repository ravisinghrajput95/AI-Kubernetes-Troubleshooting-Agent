from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EvidenceStatus(StrEnum):
    """Outcome of a single evidence collection attempt.

    Collection failures are represented as evidence with a non-usable status
    rather than as exceptions, so a reasoning layer can cite what is missing
    instead of silently reasoning over a gap.
    """

    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FORBIDDEN = "forbidden"
    TIMEOUT = "timeout"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"

    @property
    def usable(self) -> bool:
        """True when collection succeeded, including a successful empty result."""
        return self in {EvidenceStatus.OK, EvidenceStatus.EMPTY}


class EvidenceSource(StrEnum):
    """Which *subsystem* a fact came from — not which mechanism fetched it.

    `KUBECTL` is badly named and cannot be renamed: it is a wire value
    (`EVIDENCE_SOURCE_KUBECTL` in `proto/agent/v1/evidence.proto`, mapped in
    `app/wire/codec.py`), so changing it breaks every deployed agent for no
    informational gain. Read it as "a Kubernetes API read".

    **An agent-collected record carries `KUBECTL` even though no kubectl ran** —
    the agent uses client-go, and a cluster reached through its agent needs no
    kubectl on the platform at all. That is deliberate, not an oversight:
    collectors sit *above* the provider seam and must not be able to tell which
    provider they have, which is the property `raw_executor()` was removed to
    guarantee. Making this field name the transport would hand every collector
    exactly the knowledge that seam exists to withhold.

    The transport is recorded once per investigation, where it belongs and
    where it is correct: `investigation["cluster_access"]`, as
    `{"provider": "agent" | "kubeconfig", "cluster_id": ...}`. One investigation
    uses one provider, so a per-record copy would add nothing anyway.

    Noticed while verifying the agent path end to end, where an agent-served
    investigation's evidence all read `source: kubectl` and looked wrong.
    """

    KUBECTL = "kubectl"
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    DERIVED = "derived"


class EvidenceKind:
    """Namespaced evidence kind identifiers.

    Kinds are plain strings so new collectors can introduce their own without
    editing a central enum. The constants here cover the built-in collectors.
    """

    PODS = "k8s.pods"
    POD_LOGS = "k8s.pods.logs"
    EVENTS = "k8s.events"
    DEPLOYMENTS = "k8s.deployments"
    NETWORK = "k8s.network"
    NODES = "k8s.nodes"
    NODES_RAW = "k8s.nodes.raw"
    STORAGE = "k8s.storage"
    WORKLOADS = "k8s.workloads"
    METRICS_NODES = "k8s.metrics.nodes"
    METRICS_PODS = "k8s.metrics.pods"

    # Targeted evidence, collected by playbooks against a specific resource.
    POD_SPEC = "k8s.pod.spec"
    POD_LOGS_PREVIOUS = "k8s.pod.logs.previous"
    RESOURCE_EVENTS = "k8s.resource.events"
    CONFIG_REFS = "k8s.pod.config_refs"
    QUOTAS = "k8s.quotas"
    LIMIT_RANGES = "k8s.limitranges"
    STORAGE_CLASSES = "k8s.storageclasses"
    VOLUME_ATTACHMENTS = "k8s.volumeattachments"
    ENDPOINT_SLICES = "k8s.endpointslices"
    NETWORK_POLICIES = "k8s.networkpolicies"
    INGRESSES = "k8s.ingresses"
    DNS_WORKLOAD = "k8s.dns.workload"
    SERVICE_ACCOUNT = "k8s.serviceaccount"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Stable reference to the Kubernetes object a piece of evidence describes."""

    kind: str
    name: str
    namespace: str | None = None
    uid: str | None = None

    @property
    def key(self) -> str:
        scope = self.namespace or "_cluster"
        return f"{self.kind.lower()}/{scope}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
        }

    @classmethod
    def cluster(cls, name: str | None = None) -> "ResourceRef":
        return cls(kind="Cluster", name=name or "current-context")


@dataclass(frozen=True, slots=True)
class Evidence:
    """A single addressable, citable observation about the cluster.

    `id` is deterministic so the same fact collected in two investigations
    carries the same identifier, and so a diagnosis can reference evidence by
    id rather than by copying its payload.
    """

    id: str
    kind: str
    source: EvidenceSource
    status: EvidenceStatus
    target: ResourceRef
    data: Any = None
    detail: str = ""
    command: str | None = None
    collector_id: str = ""
    duration_ms: int = 0
    redacted: bool = False
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def usable(self) -> bool:
        return self.status.usable

    @classmethod
    def create(
        cls,
        kind: str,
        status: EvidenceStatus,
        target: ResourceRef,
        source: EvidenceSource = EvidenceSource.KUBECTL,
        data: Any = None,
        detail: str = "",
        command: str | None = None,
        collector_id: str = "",
        duration_ms: int = 0,
        discriminator: str = "",
    ) -> "Evidence":
        evidence_id = f"{kind}:{target.key}"
        if discriminator:
            evidence_id = f"{evidence_id}#{discriminator}"

        return cls(
            id=evidence_id,
            kind=kind,
            source=source,
            status=status,
            target=target,
            data=data,
            detail=detail,
            command=command,
            collector_id=collector_id,
            duration_ms=duration_ms,
        )

    def to_index_entry(self) -> dict[str, Any]:
        """Serializable citation record, without the payload.

        Payloads are already carried in the investigation body; the index keeps
        reports small while still letting every conclusion cite an evidence id.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "source": str(self.source),
            "status": str(self.status),
            "target": self.target.to_dict(),
            "detail": self.detail,
            "command": self.command,
            "collector_id": self.collector_id,
            "duration_ms": self.duration_ms,
            "redacted": self.redacted,
            "collected_at": self.collected_at.isoformat(),
        }
