"""The built-in playbooks.

Each one targets a failure class from the roadmap's deep-investigation list and
collects exactly the evidence the corresponding hypotheses declare as
`missing_evidence` — the gap the deterministic layer already knew it had.
"""

from collections.abc import Sequence

from app.analysis.models import SignalType
from app.collectors.base import Collector
from app.collectors.observability import (
    NodeMetricsCollector,
    PodLogSearchCollector,
    PodMetricsCollector,
)
from app.collectors.targeted import (
    ConfigReferenceCollector,
    DnsWorkloadCollector,
    EndpointSliceCollector,
    IngressCollector,
    LimitRangeCollector,
    NetworkPolicyCollector,
    PodPreviousLogsCollector,
    PodSpecCollector,
    ResourceEventsCollector,
    ResourceQuotaCollector,
    ServiceAccountCollector,
    StorageClassCollector,
    VolumeAttachmentCollector,
)
from app.evidence.models import ResourceRef
from app.playbooks.base import BasePlaybook, PlaybookContext

POD_KINDS = frozenset({"Pod"})


class CrashLoopPlaybook(BasePlaybook):
    """Why is this container starting and dying repeatedly?

    Collects the previous container's logs (the ones showing the actual crash),
    the pod's probes and exit codes, its scoped events, and whether the
    ConfigMaps and Secrets it references actually exist with the expected keys.
    """

    id = "crashloop"
    title = "CrashLoopBackOff deep investigation"
    triggers = frozenset(
        {
            SignalType.POD_CRASH_LOOP,
            SignalType.EVENT_BACKOFF,
            SignalType.POD_OOM_KILLED,
            SignalType.POD_ERROR,
            SignalType.LOGS_OOM_PATTERN,
        }
    )

    def plan(self, context: PlaybookContext) -> Sequence[Collector]:
        collectors: list[Collector] = []

        for target in self.targets(context, kinds=POD_KINDS):
            collectors.extend(
                [
                    PodSpecCollector(target),
                    PodPreviousLogsCollector(target),
                    ResourceEventsCollector(target),
                    ConfigReferenceCollector(target),
                    # Optional backends: absent ones record why, and the rest
                    # of the investigation is unaffected.
                    PodMetricsCollector(target),
                    PodLogSearchCollector(target),
                ]
            )

        return collectors


class PendingPlaybook(BasePlaybook):
    """Why can this pod not be placed on a node?

    Collects the pod's scheduling constraints and resource requests, the
    scheduler's own events, and the namespace quotas and limit ranges that
    commonly block admission.
    """

    id = "pending"
    title = "Pending and unschedulable deep investigation"
    triggers = frozenset(
        {
            SignalType.POD_PENDING,
            SignalType.EVENT_SCHEDULING_FAILURE,
            SignalType.POD_STUCK_CREATING,
        }
    )

    def plan(self, context: PlaybookContext) -> Sequence[Collector]:
        collectors: list[Collector] = []
        namespaces = set()

        for target in self.targets(context, kinds=POD_KINDS):
            collectors.extend([PodSpecCollector(target), ResourceEventsCollector(target)])
            if target.namespace:
                namespaces.add(target.namespace)

        for namespace in sorted(namespaces):
            scope_ref = ResourceRef(kind="Namespace", name=namespace, namespace=namespace)
            collectors.extend([ResourceQuotaCollector(scope_ref), LimitRangeCollector(scope_ref)])

        for node in self._nodes(context):
            collectors.append(NodeMetricsCollector(node))

        return collectors

    def _nodes(self, context: PlaybookContext) -> list[ResourceRef]:
        """Nodes named by node-level signals, if any were raised."""
        seen: dict[str, ResourceRef] = {}
        for signal in context.analysis.signals:
            if signal.target.kind == "Node":
                seen.setdefault(signal.target.key, signal.target)
        return list(seen.values())[: context.max_targets]


class ImagePullPlaybook(BasePlaybook):
    """Why can the kubelet not pull this image?

    Collects the pull error from the pod's events, the image reference and
    imagePullSecrets from its spec, and the service account that supplies
    fallback credentials.
    """

    id = "imagepull"
    title = "Image pull failure deep investigation"
    triggers = frozenset({SignalType.POD_IMAGE_PULL_FAILURE, SignalType.EVENT_IMAGE_PULL_FAILURE})

    def plan(self, context: PlaybookContext) -> Sequence[Collector]:
        collectors: list[Collector] = []

        for target in self.targets(context, kinds=POD_KINDS):
            collectors.extend(
                [
                    PodSpecCollector(target),
                    ResourceEventsCollector(target),
                    ConfigReferenceCollector(target),
                ]
            )
            # Pull credentials may come from the pod's service account rather
            # than the pod spec; the default account is the common case.
            collectors.append(
                ServiceAccountCollector(
                    ResourceRef(
                        kind="ServiceAccount",
                        name="default",
                        namespace=target.namespace,
                    )
                )
            )

        return self._deduplicate(collectors)

    def _deduplicate(self, collectors: list[Collector]) -> list[Collector]:
        seen: dict[str, Collector] = {}
        for collector in collectors:
            seen.setdefault(collector.id, collector)
        return list(seen.values())


class NetworkPlaybook(BasePlaybook):
    """Why is traffic not reaching these pods?

    Collects endpoint slices, network policies and ingresses for the affected
    namespaces, plus cluster DNS health when name resolution is implicated.
    """

    id = "network"
    title = "Service and connectivity deep investigation"
    triggers = frozenset(
        {
            SignalType.NETWORK_NO_ENDPOINTS,
            SignalType.NETWORK_NO_SELECTOR,
            SignalType.NETWORK_DNS_MISSING,
            SignalType.EVENT_PROBE_FAILURE,
        }
    )

    def plan(self, context: PlaybookContext) -> Sequence[Collector]:
        collectors: list[Collector] = []
        namespaces = {target.namespace for target in self.targets(context) if target.namespace}

        for namespace in sorted(namespaces):
            scope_ref = ResourceRef(kind="Namespace", name=namespace, namespace=namespace)
            collectors.extend(
                [
                    EndpointSliceCollector(scope_ref),
                    NetworkPolicyCollector(scope_ref),
                    IngressCollector(scope_ref),
                ]
            )

        if self._dns_implicated(context):
            collectors.append(DnsWorkloadCollector(ResourceRef.cluster(context.scope.context)))

        return collectors

    def _dns_implicated(self, context: PlaybookContext) -> bool:
        return any(
            signal.type == SignalType.NETWORK_DNS_MISSING for signal in context.analysis.signals
        )


class StoragePlaybook(BasePlaybook):
    """Why can this volume not be bound or mounted?

    Collects the storage classes and their binding modes, outstanding volume
    attachments, and the events on the affected claim.
    """

    id = "storage"
    title = "Volume and storage deep investigation"
    triggers = frozenset(
        {
            SignalType.STORAGE_PVC_UNBOUND,
            SignalType.STORAGE_PV_UNAVAILABLE,
            SignalType.EVENT_MOUNT_FAILURE,
        }
    )

    def plan(self, context: PlaybookContext) -> Sequence[Collector]:
        cluster_ref = ResourceRef.cluster(context.scope.context)
        collectors: list[Collector] = [
            StorageClassCollector(cluster_ref),
            VolumeAttachmentCollector(cluster_ref),
        ]

        for target in self.targets(context):
            if target.kind in {"PersistentVolumeClaim", "Pod"}:
                collectors.append(ResourceEventsCollector(target))

        return collectors


DEFAULT_PLAYBOOKS = (
    CrashLoopPlaybook(),
    PendingPlaybook(),
    ImagePullPlaybook(),
    NetworkPlaybook(),
    StoragePlaybook(),
)
