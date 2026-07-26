"""Collectors parameterised by a specific resource.

Playbooks emit these against the targets their triggering signals name. Two
conventions matter here:

- Evidence is a **derived summary**, not a raw object dump. One `get pod -o
  json` already contains probes, exit codes, restart counts, limits and config
  references; extracting them keeps reports small and avoids parsing `describe`
  text, which is not stable across kubectl versions.
- Secret contents are never read. Referenced Secrets go through `describe`,
  which by design prints key names and sizes but no values.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from app.collectors.base import BaseCollector, CollectionContext
from app.evidence.models import (
    Evidence,
    EvidenceKind,
    EvidenceSource,
    EvidenceStatus,
    ResourceRef,
)
from app.kubernetes.errors import classify_error
from app.providers.base import OutputFormat, ProviderResult, ReadVerb, ResourceRequest


class TargetedCollector(BaseCollector):
    """Base for collectors bound to one resource."""

    kind: str = ""
    prefix: str = ""

    def __init__(self, target: ResourceRef) -> None:
        self.target = target
        self.id = f"{self.prefix}:{target.key}"
        self.provides = frozenset({self.kind})
        self.requires = frozenset()
        self.optional_requires = frozenset()

    def _evidence(
        self,
        context: CollectionContext,
        status: EvidenceStatus,
        data: Any = None,
        detail: str = "",
        command: str | None = None,
    ) -> Evidence:
        return Evidence.create(
            kind=self.kind,
            status=status,
            target=self.target,
            source=EvidenceSource.KUBECTL,
            data=data,
            detail=detail,
            command=command,
            collector_id=self.id,
        )

    async def _fetch(self, context: CollectionContext, request: ResourceRequest) -> ProviderResult:
        """Describe the evidence needed; the provider decides how to obtain it."""
        return await context.fetch(request)

    def _get(
        self,
        resource: str,
        *,
        name: str | None = None,
        namespaced: bool = True,
        **kwargs,
    ) -> ResourceRequest:
        return ResourceRequest(
            verb=ReadVerb.GET,
            resource=resource,
            name=name,
            namespace=self.target.namespace if namespaced else None,
            **kwargs,
        )


def _probe_summary(container: dict[str, Any]) -> dict[str, Any]:
    probes = {}
    for name in ("livenessProbe", "readinessProbe", "startupProbe"):
        probe = container.get(name)
        if not probe:
            continue
        handler = next(
            (key for key in ("httpGet", "tcpSocket", "exec", "grpc") if key in probe),
            "unknown",
        )
        probes[name.replace("Probe", "")] = {
            "handler": handler,
            "initial_delay_seconds": probe.get("initialDelaySeconds", 0),
            "period_seconds": probe.get("periodSeconds", 10),
            "timeout_seconds": probe.get("timeoutSeconds", 1),
            "failure_threshold": probe.get("failureThreshold", 3),
            "path": probe.get(handler, {}).get("path")
            if isinstance(probe.get(handler), dict)
            else None,
            "port": probe.get(handler, {}).get("port")
            if isinstance(probe.get(handler), dict)
            else None,
        }
    return probes


def _config_references(container: dict[str, Any]) -> list[dict[str, Any]]:
    """ConfigMap and Secret references made by a container's environment."""
    references = []

    for entry in container.get("env", []):
        source = entry.get("valueFrom", {})
        for field, ref_kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
            ref = source.get(field)
            if ref:
                references.append(
                    {
                        "kind": ref_kind,
                        "name": ref.get("name", ""),
                        "key": ref.get("key", ""),
                        "optional": bool(ref.get("optional", False)),
                        "source": f"env:{entry.get('name', '')}",
                    }
                )

    for entry in container.get("envFrom", []):
        for field, ref_kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
            ref = entry.get(field)
            if ref:
                references.append(
                    {
                        "kind": ref_kind,
                        "name": ref.get("name", ""),
                        "key": "",
                        "optional": bool(ref.get("optional", False)),
                        "source": "envFrom",
                    }
                )

    return references


def _volume_summary(spec: dict[str, Any]) -> list[dict[str, Any]]:
    volumes = []
    for volume in spec.get("volumes", []):
        entry: dict[str, Any] = {"name": volume.get("name", "")}
        if "persistentVolumeClaim" in volume:
            entry["type"] = "PersistentVolumeClaim"
            entry["claim"] = volume["persistentVolumeClaim"].get("claimName", "")
        elif "configMap" in volume:
            entry["type"] = "ConfigMap"
            entry["name_ref"] = volume["configMap"].get("name", "")
            entry["optional"] = bool(volume["configMap"].get("optional", False))
        elif "secret" in volume:
            entry["type"] = "Secret"
            entry["name_ref"] = volume["secret"].get("secretName", "")
            entry["optional"] = bool(volume["secret"].get("optional", False))
        else:
            entry["type"] = next(iter(set(volume) - {"name"}), "unknown")
        volumes.append(entry)
    return volumes


class PodSpecCollector(TargetedCollector):
    """Structured summary of a pod: probes, states, limits, and references."""

    kind = EvidenceKind.POD_SPEC
    prefix = "k8s.pod.spec"
    label = "Inspected Pod Specification"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(context, self._get("pod", name=self.target.name))

        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                self._evidence(context, status, detail=detail, command=result.equivalent_command)
            ]

        return [
            self._evidence(
                context,
                EvidenceStatus.OK,
                data=self._summarize(result.data),
                command=result.equivalent_command,
            )
        ]

    def _summarize(self, pod: dict[str, Any]) -> dict[str, Any]:
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        states = {item.get("name"): item for item in status.get("containerStatuses", [])}

        containers = []
        for container in spec.get("containers", []):
            name = container.get("name", "")
            state = states.get(name, {})
            resources = container.get("resources", {})
            containers.append(
                {
                    "name": name,
                    "image": container.get("image", ""),
                    "restart_count": state.get("restartCount", 0),
                    "ready": state.get("ready", False),
                    "probes": _probe_summary(container),
                    "limits": resources.get("limits", {}),
                    "requests": resources.get("requests", {}),
                    "last_state": self._terminated(state.get("lastState", {})),
                    "current_state": self._current(state.get("state", {})),
                    "config_refs": _config_references(container),
                }
            )

        return {
            "pod": pod.get("metadata", {}).get("name", ""),
            "namespace": pod.get("metadata", {}).get("namespace", ""),
            "owner": self._owner(pod.get("metadata", {})),
            "node": spec.get("nodeName", ""),
            "phase": status.get("phase", ""),
            "labels": pod.get("metadata", {}).get("labels", {}),
            "service_account": spec.get("serviceAccountName", "default"),
            "image_pull_secrets": [
                item.get("name", "") for item in spec.get("imagePullSecrets", [])
            ],
            "tolerations": [
                {
                    "key": item.get("key", ""),
                    "operator": item.get("operator", "Equal"),
                    "value": item.get("value", ""),
                    "effect": item.get("effect", ""),
                }
                for item in spec.get("tolerations", [])
            ],
            "node_selector": spec.get("nodeSelector", {}),
            "has_affinity": bool(spec.get("affinity")),
            "priority_class": spec.get("priorityClassName", ""),
            "volumes": _volume_summary(spec),
            "containers": containers,
        }

    def _owner(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Controller that owns the pod, and the workload to remediate.

        A pod's direct owner is usually a ReplicaSet, but the resource an
        operator changes is the Deployment above it. Kubernetes names a
        ReplicaSet `<deployment>-<pod-template-hash>`, so the Deployment name is
        derived by dropping that final segment. The derivation is marked as such
        and the ReplicaSet name is kept, so it can be verified rather than
        trusted.
        """
        owners = metadata.get("ownerReferences", [])
        if not owners:
            return {}

        owner = owners[0]
        kind = owner.get("kind", "")
        name = owner.get("name", "")
        result: dict[str, Any] = {"kind": kind, "name": name}

        if kind == "ReplicaSet" and "-" in name:
            result["workload_kind"] = "Deployment"
            result["workload_name"] = name.rsplit("-", 1)[0]
            result["workload_derived"] = True
        elif kind in {"StatefulSet", "DaemonSet", "Job"}:
            result["workload_kind"] = kind
            result["workload_name"] = name
            result["workload_derived"] = False

        return result

    def _terminated(self, last_state: dict[str, Any]) -> dict[str, Any]:
        terminated = last_state.get("terminated")
        if not terminated:
            return {}
        return {
            "reason": terminated.get("reason", ""),
            "exit_code": terminated.get("exitCode"),
            "signal": terminated.get("signal"),
            "finished_at": terminated.get("finishedAt", ""),
        }

    def _current(self, state: dict[str, Any]) -> dict[str, Any]:
        for name in ("waiting", "running", "terminated"):
            if name in state:
                detail = state[name] or {}
                return {
                    "phase": name,
                    "reason": detail.get("reason", ""),
                    "message": detail.get("message", "")[:400],
                }
        return {}


class PodPreviousLogsCollector(TargetedCollector):
    """Logs from the container instance that existed before the last restart."""

    kind = EvidenceKind.POD_LOGS_PREVIOUS
    prefix = "k8s.pod.logs.previous"
    label = "Read Previous Container Logs"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(
            context,
            ResourceRequest(
                verb=ReadVerb.LOGS,
                name=self.target.name,
                namespace=self.target.namespace,
                output=OutputFormat.TEXT,
                options={"previous": True, "tail": 200, "all_containers": True},
            ),
        )
        command = result.equivalent_command

        if not result.success:
            # A pod that has never restarted has no previous instance; that is a
            # normal answer, not a failure of the platform.
            lowered = result.error.lower()
            if "not found" in lowered or "previous terminated" in lowered:
                return [
                    self._evidence(
                        context,
                        EvidenceStatus.EMPTY,
                        data={"lines": []},
                        detail="No previous container instance exists for this pod.",
                        command=command,
                    )
                ]
            status, detail = classify_error(result.error)
            return [self._evidence(context, status, detail=detail, command=command)]

        lines = [line[:500] for line in result.text.splitlines() if line.strip()]
        return [
            self._evidence(
                context,
                EvidenceStatus.OK if lines else EvidenceStatus.EMPTY,
                data={"lines": lines[-120:], "line_count": len(lines)},
                command=command,
            )
        ]


class ResourceEventsCollector(TargetedCollector):
    """Events scoped to one object, which carry scheduler and kubelet detail."""

    kind = EvidenceKind.RESOURCE_EVENTS
    prefix = "k8s.resource.events"
    label = "Read Resource Events"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(
            context,
            self._get(
                "events",
                field_selector=f"involvedObject.name={self.target.name}",
            ),
        )

        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                self._evidence(context, status, detail=detail, command=result.equivalent_command)
            ]

        events = [
            {
                "reason": item.get("reason", ""),
                "type": item.get("type", ""),
                "message": item.get("message", "")[:500],
                "count": item.get("count", 1),
                "last_seen": item.get("lastTimestamp", ""),
            }
            for item in result.data.get("items", [])
        ]

        return [
            self._evidence(
                context,
                EvidenceStatus.OK if events else EvidenceStatus.EMPTY,
                data={"events": events[:30]},
                command=result.equivalent_command,
            )
        ]


class ConfigReferenceCollector(TargetedCollector):
    """Resolves the ConfigMaps and Secrets a pod references.

    Answers two diagnostic questions without ever reading a value: does the
    referenced object exist, and does it contain the referenced key?
    """

    kind = EvidenceKind.CONFIG_REFS
    prefix = "k8s.pod.config_refs"
    label = "Resolved Config References"
    requires = frozenset({EvidenceKind.POD_SPEC})

    def __init__(self, target: ResourceRef) -> None:
        super().__init__(target)
        self.requires = frozenset({EvidenceKind.POD_SPEC})

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        spec = self._pod_spec(context)
        if spec is None:
            return [
                self._evidence(
                    context,
                    EvidenceStatus.NOT_APPLICABLE,
                    detail="Pod specification was not collected, so references cannot be resolved.",
                )
            ]

        wanted: dict[tuple[str, str], list[str]] = {}
        for container in spec.get("containers", []):
            for reference in container.get("config_refs", []):
                name = reference.get("name")
                if not name:
                    continue
                key = (reference.get("kind", ""), name)
                wanted.setdefault(key, [])
                if reference.get("key"):
                    wanted[key].append(reference["key"])

        for volume in spec.get("volumes", []):
            name = volume.get("name_ref")
            if volume.get("type") in {"ConfigMap", "Secret"} and name:
                wanted.setdefault((volume["type"], name), [])

        if not wanted:
            return [
                self._evidence(
                    context,
                    EvidenceStatus.EMPTY,
                    data={"references": []},
                    detail="The pod does not reference any ConfigMap or Secret.",
                )
            ]

        resolved = await asyncio.gather(
            *(
                self._resolve(context, ref_kind, name, sorted(set(keys)))
                for (ref_kind, name), keys in wanted.items()
            )
        )

        return [self._evidence(context, EvidenceStatus.OK, data={"references": list(resolved)})]

    def _pod_spec(self, context: CollectionContext) -> dict[str, Any] | None:
        for evidence in context.store.by_kind(EvidenceKind.POD_SPEC):
            if evidence.usable and evidence.target.key == self.target.key:
                return evidence.data
        return None

    async def _resolve(
        self,
        context: CollectionContext,
        ref_kind: str,
        name: str,
        keys: list[str],
    ) -> dict[str, Any]:
        if ref_kind == "Secret":
            available, exists, detail = await self._secret_keys(context, name)
        else:
            available, exists, detail = await self._configmap_keys(context, name)

        missing = [key for key in keys if exists and key not in available] if available else []

        return {
            "kind": ref_kind,
            "name": name,
            "exists": exists,
            "available_keys": available,
            "required_keys": keys,
            "missing_keys": missing,
            "detail": detail,
        }

    async def _configmap_keys(
        self,
        context: CollectionContext,
        name: str,
    ) -> tuple[list[str], bool, str]:
        result = await self._fetch(context, self._get("configmap", name=name))

        if not result.success or not isinstance(result.data, dict):
            if "not found" in result.error.lower():
                return [], False, "ConfigMap does not exist."
            return [], False, classify_error(result.error)[1]

        # Key names only; ConfigMap values can carry connection strings.
        keys = sorted(result.data.get("data", {}))
        keys.extend(sorted(result.data.get("binaryData", {})))
        return keys, True, ""

    async def _secret_keys(
        self,
        context: CollectionContext,
        name: str,
    ) -> tuple[list[str], bool, str]:
        # `describe` never prints secret values, so no value ever enters memory.
        result = await self._fetch(
            context,
            ResourceRequest(
                verb=ReadVerb.DESCRIBE,
                resource="secret",
                name=name,
                namespace=self.target.namespace,
                output=OutputFormat.TEXT,
            ),
        )

        if not result.success:
            if "not found" in result.error.lower():
                return [], False, "Secret does not exist."
            return [], False, classify_error(result.error)[1]

        return self._parse_described_keys(result.text), True, ""

    def _parse_described_keys(self, output: str) -> list[str]:
        keys = []
        in_data = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Data"):
                in_data = True
                continue
            if not in_data or not stripped or stripped.startswith("="):
                continue
            name, separator, remainder = stripped.partition(":")
            if separator and "bytes" in remainder:
                keys.append(name.strip())
        return sorted(keys)


class NamespacedListCollector(TargetedCollector):
    """Lists one resource type, cluster-wide or within the target namespace."""

    resource: str = ""

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(
            context,
            self._get(self.resource, all_namespaces=not self.target.namespace),
        )
        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                self._evidence(context, status, detail=detail, command=result.equivalent_command)
            ]

        items = [self.summarize(item) for item in result.data.get("items", [])]
        return [
            self._evidence(
                context,
                EvidenceStatus.OK if items else EvidenceStatus.EMPTY,
                data={"items": items},
                command=result.equivalent_command,
            )
        ]

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        return {"name": metadata.get("name", ""), "namespace": metadata.get("namespace", "")}


class ResourceQuotaCollector(NamespacedListCollector):
    kind = EvidenceKind.QUOTAS
    prefix = "k8s.quotas"
    resource = "resourcequotas"
    label = "Checked Resource Quotas"

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        status = item.get("status", {})
        return {
            **super().summarize(item),
            "hard": status.get("hard", {}),
            "used": status.get("used", {}),
        }


class LimitRangeCollector(NamespacedListCollector):
    kind = EvidenceKind.LIMIT_RANGES
    prefix = "k8s.limitranges"
    resource = "limitranges"
    label = "Checked Limit Ranges"

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            **super().summarize(item),
            "limits": item.get("spec", {}).get("limits", []),
        }


class StorageClassCollector(NamespacedListCollector):
    kind = EvidenceKind.STORAGE_CLASSES
    prefix = "k8s.storageclasses"
    resource = "storageclasses"
    label = "Checked Storage Classes"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        # StorageClasses are cluster-scoped.
        self.target = ResourceRef.cluster(context.scope.context)
        return await super().collect(context)

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        annotations = metadata.get("annotations", {})
        return {
            "name": metadata.get("name", ""),
            "provisioner": item.get("provisioner", ""),
            "volume_binding_mode": item.get("volumeBindingMode", "Immediate"),
            "is_default": annotations.get("storageclass.kubernetes.io/is-default-class", "false")
            == "true",
        }


class VolumeAttachmentCollector(NamespacedListCollector):
    kind = EvidenceKind.VOLUME_ATTACHMENTS
    prefix = "k8s.volumeattachments"
    resource = "volumeattachments"
    label = "Checked Volume Attachments"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        self.target = ResourceRef.cluster(context.scope.context)
        return await super().collect(context)

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        spec = item.get("spec", {})
        status = item.get("status", {})
        return {
            "name": item.get("metadata", {}).get("name", ""),
            "node": spec.get("nodeName", ""),
            "attacher": spec.get("attacher", ""),
            "attached": bool(status.get("attached", False)),
            "error": (status.get("attachError", {}) or {}).get("message", "")[:300],
        }


class EndpointSliceCollector(NamespacedListCollector):
    kind = EvidenceKind.ENDPOINT_SLICES
    prefix = "k8s.endpointslices"
    resource = "endpointslices"
    label = "Checked Endpoint Slices"

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        endpoints = item.get("endpoints", [])
        ready = sum(1 for entry in endpoints if (entry.get("conditions", {}) or {}).get("ready"))
        return {
            **super().summarize(item),
            "service": item.get("metadata", {})
            .get("labels", {})
            .get("kubernetes.io/service-name", ""),
            "address_count": len(endpoints),
            "ready_count": ready,
        }


class NetworkPolicyCollector(NamespacedListCollector):
    kind = EvidenceKind.NETWORK_POLICIES
    prefix = "k8s.networkpolicies"
    resource = "networkpolicies"
    label = "Checked Network Policies"

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        spec = item.get("spec", {})
        return {
            **super().summarize(item),
            "policy_types": spec.get("policyTypes", []),
            "pod_selector": spec.get("podSelector", {}),
            "denies_all_ingress": spec.get("podSelector") == {}
            and "Ingress" in spec.get("policyTypes", [])
            and not spec.get("ingress"),
        }


class IngressCollector(NamespacedListCollector):
    kind = EvidenceKind.INGRESSES
    prefix = "k8s.ingresses"
    resource = "ingresses"
    label = "Checked Ingresses"

    def summarize(self, item: dict[str, Any]) -> dict[str, Any]:
        spec = item.get("spec", {})
        backends = []
        for rule in spec.get("rules", []):
            for path in (rule.get("http", {}) or {}).get("paths", []):
                service = (path.get("backend", {}).get("service", {}) or {}).get("name", "")
                if service:
                    backends.append(service)
        return {
            **super().summarize(item),
            "ingress_class": spec.get("ingressClassName", ""),
            "backend_services": backends,
        }


class DnsWorkloadCollector(TargetedCollector):
    """Health of the cluster DNS deployment itself."""

    kind = EvidenceKind.DNS_WORKLOAD
    prefix = "k8s.dns.workload"
    label = "Checked Cluster DNS"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(
            context,
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="pods",
                namespace="kube-system",
                label_selector="k8s-app=kube-dns",
            ),
        )

        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                self._evidence(context, status, detail=detail, command=result.equivalent_command)
            ]

        pods = [
            {
                "name": item.get("metadata", {}).get("name", ""),
                "phase": item.get("status", {}).get("phase", ""),
                "ready": all(
                    entry.get("ready", False)
                    for entry in item.get("status", {}).get("containerStatuses", [])
                ),
                "restarts": sum(
                    entry.get("restartCount", 0)
                    for entry in item.get("status", {}).get("containerStatuses", [])
                ),
            }
            for item in result.data.get("items", [])
        ]

        return [
            self._evidence(
                context,
                EvidenceStatus.OK if pods else EvidenceStatus.EMPTY,
                data={"pods": pods, "ready_count": sum(1 for pod in pods if pod["ready"])},
                detail="" if pods else "No CoreDNS pods matched k8s-app=kube-dns.",
                command=result.equivalent_command,
            )
        ]


class ServiceAccountCollector(TargetedCollector):
    """Service account backing a pod, for image pull credential resolution."""

    kind = EvidenceKind.SERVICE_ACCOUNT
    prefix = "k8s.serviceaccount"
    label = "Checked Service Account"

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]:
        result = await self._fetch(context, self._get("serviceaccount", name=self.target.name))

        if not result.success or not isinstance(result.data, dict):
            status, detail = classify_error(result.error)
            return [
                self._evidence(context, status, detail=detail, command=result.equivalent_command)
            ]

        return [
            self._evidence(
                context,
                EvidenceStatus.OK,
                data={
                    "name": result.data.get("metadata", {}).get("name", ""),
                    "image_pull_secrets": [
                        item.get("name", "") for item in result.data.get("imagePullSecrets", [])
                    ],
                },
                command=result.equivalent_command,
            )
        ]
