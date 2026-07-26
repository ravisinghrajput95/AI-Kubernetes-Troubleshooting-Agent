"""Signal rules over targeted evidence collected by playbooks.

These are what make a deep investigation worth running: an exit code that
confirms an OOM kill, a ConfigMap key the container expects but that does not
exist, a quota that is already fully consumed. Each cites the specific evidence
record for its resource rather than the evidence kind.
"""

from collections.abc import Sequence
from typing import Any

from app.analysis.models import Severity, Signal, SignalType
from app.analysis.signal_rules import AnalysisInput
from app.evidence.models import EvidenceKind, ResourceRef

OOM_EXIT_CODE = 137
SIGTERM_EXIT_CODE = 143
AGGRESSIVE_PROBE_DELAY_SECONDS = 5


def _ref(entry: dict[str, Any]) -> ResourceRef:
    target = entry.get("target", {}) or {}
    return ResourceRef(
        kind=target.get("kind", "Pod"),
        name=target.get("name", "unknown"),
        namespace=target.get("namespace"),
    )


def _provenance(entry: dict[str, Any]) -> tuple[str, ...]:
    evidence_id = entry.get("id")
    return (evidence_id,) if evidence_id else ("investigation.deep_evidence",)


class ContainerTerminationRule:
    """Exit codes and termination reasons from the pod's last container state."""

    id = "deep.container_termination"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.POD_SPEC):
            target = _ref(entry)
            evidence = _provenance(entry)
            spec = entry["data"]

            for container in spec.get("containers", []):
                last_state = container.get("last_state", {}) or {}
                exit_code = last_state.get("exit_code")
                reason = last_state.get("reason", "")
                name = container.get("name", "container")

                if exit_code == OOM_EXIT_CODE or reason == "OOMKilled":
                    signals.append(
                        Signal.create(
                            SignalType.CONTAINER_OOM_EXIT,
                            Severity.CRITICAL,
                            f"Container {name} last terminated with exit code "
                            f"{exit_code} ({reason or 'OOMKilled'}), confirming an "
                            f"out-of-memory kill.",
                            target,
                            evidence,
                            {
                                "container": name,
                                "exit_code": exit_code,
                                "reason": reason,
                                "limits": container.get("limits", {}),
                            },
                        )
                    )
                elif isinstance(exit_code, int) and exit_code not in (0, SIGTERM_EXIT_CODE):
                    signals.append(
                        Signal.create(
                            SignalType.CONTAINER_NONZERO_EXIT,
                            Severity.HIGH,
                            f"Container {name} last terminated with exit code "
                            f"{exit_code} ({reason or 'Error'}).",
                            target,
                            evidence,
                            {"container": name, "exit_code": exit_code, "reason": reason},
                        )
                    )

                if container.get("restart_count", 0) and not container.get("limits", {}).get(
                    "memory"
                ):
                    signals.append(
                        Signal.create(
                            SignalType.CONTAINER_NO_MEMORY_LIMIT,
                            Severity.MEDIUM,
                            f"Container {name} restarts but declares no memory limit, "
                            f"so it can be evicted under node pressure.",
                            target,
                            evidence,
                            {"container": name, "restart_count": container["restart_count"]},
                        )
                    )

        return signals


class ProbeConfigurationRule:
    """Probe timings tight enough to kill a container before it finishes starting."""

    id = "deep.probe_configuration"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.POD_SPEC):
            target = _ref(entry)
            evidence = _provenance(entry)

            for container in entry["data"].get("containers", []):
                probes = container.get("probes", {})
                liveness = probes.get("liveness")
                if not liveness or probes.get("startup"):
                    # A startup probe exists precisely to protect slow starts.
                    continue

                delay = liveness.get("initial_delay_seconds", 0)
                if delay > AGGRESSIVE_PROBE_DELAY_SECONDS:
                    continue

                name = container.get("name", "container")
                signals.append(
                    Signal.create(
                        SignalType.PROBE_AGGRESSIVE,
                        Severity.MEDIUM,
                        f"Container {name} has a liveness probe starting after "
                        f"{delay}s with no startup probe, which can restart a "
                        f"container that is still initialising.",
                        target,
                        evidence,
                        {"container": name, "liveness": liveness},
                    )
                )

        return signals


class ConfigReferenceRule:
    """ConfigMaps and Secrets a container needs but cannot resolve."""

    id = "deep.config_references"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.CONFIG_REFS):
            target = _ref(entry)
            evidence = _provenance(entry)

            for reference in entry["data"].get("references", []):
                kind = reference.get("kind", "ConfigMap")
                name = reference.get("name", "")

                if not reference.get("exists"):
                    signals.append(
                        Signal.create(
                            SignalType.CONFIG_REFERENCE_MISSING,
                            Severity.CRITICAL,
                            f"{kind} '{name}' is referenced by the pod but does not "
                            f"exist in the namespace.",
                            target,
                            evidence,
                            {"kind": kind, "name": name, "detail": reference.get("detail", "")},
                        )
                    )
                    continue

                missing = reference.get("missing_keys") or []
                if missing:
                    signals.append(
                        Signal.create(
                            SignalType.CONFIG_KEY_MISSING,
                            Severity.CRITICAL,
                            f"{kind} '{name}' exists but is missing the key(s) the "
                            f"pod requires: {', '.join(missing)}.",
                            target,
                            evidence,
                            {
                                "kind": kind,
                                "name": name,
                                "missing_keys": missing,
                                "available_keys": reference.get("available_keys", []),
                            },
                        )
                    )

        return signals


class SchedulingEventRule:
    """Scheduler messages naming why a pod could not be placed."""

    id = "deep.scheduling_events"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.RESOURCE_EVENTS):
            target = _ref(entry)
            evidence = _provenance(entry)

            for event in entry["data"].get("events", []):
                if event.get("reason") != "FailedScheduling":
                    continue

                message = event.get("message", "")
                lowered = message.lower()

                if "insufficient" in lowered:
                    signals.append(
                        Signal.create(
                            SignalType.SCHEDULING_INSUFFICIENT_RESOURCES,
                            Severity.HIGH,
                            f"Scheduler reports insufficient capacity: {message[:200]}",
                            target,
                            evidence,
                            {"message": message},
                        )
                    )
                elif "taint" in lowered:
                    signals.append(
                        Signal.create(
                            SignalType.SCHEDULING_TAINT_BLOCKED,
                            Severity.HIGH,
                            f"Scheduler blocked by node taints: {message[:200]}",
                            target,
                            evidence,
                            {"message": message},
                        )
                    )

        return signals


class ImagePullEventRule:
    """Distinguishes an authentication failure from a missing image."""

    id = "deep.image_pull_events"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.RESOURCE_EVENTS):
            target = _ref(entry)
            evidence = _provenance(entry)

            for event in entry["data"].get("events", []):
                if event.get("reason") not in {"Failed", "FailedPull", "ErrImagePull"}:
                    continue

                message = event.get("message", "")
                lowered = message.lower()

                if "unauthorized" in lowered or "authentication" in lowered or "403" in lowered:
                    signals.append(
                        Signal.create(
                            SignalType.IMAGE_PULL_UNAUTHORIZED,
                            Severity.CRITICAL,
                            f"Registry rejected the pull as unauthorized: {message[:200]}",
                            target,
                            evidence,
                            {"message": message},
                        )
                    )
                elif "not found" in lowered or "manifest unknown" in lowered or "404" in lowered:
                    signals.append(
                        Signal.create(
                            SignalType.IMAGE_NOT_FOUND,
                            Severity.CRITICAL,
                            f"Image or tag does not exist in the registry: {message[:200]}",
                            target,
                            evidence,
                            {"message": message},
                        )
                    )

        return signals


PULL_FAILURE_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}


class ImagePullSecretRule:
    """A private-registry pull attempted with no credentials configured.

    Gated on the container actually failing to pull. Most pods legitimately have
    no imagePullSecrets, so reporting their absence unconditionally would bury
    real findings in noise.
    """

    id = "deep.image_pull_secrets"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        service_account_secrets = {
            entry.get("target", {}).get("namespace"): entry["data"].get("image_pull_secrets", [])
            for entry in data.deep(EvidenceKind.SERVICE_ACCOUNT)
        }

        signals = []
        for entry in data.deep(EvidenceKind.POD_SPEC):
            spec = entry["data"]
            if spec.get("image_pull_secrets"):
                continue

            if not self._pull_failing(spec):
                continue

            namespace = entry.get("target", {}).get("namespace")
            if service_account_secrets.get(namespace):
                continue

            signals.append(
                Signal.create(
                    SignalType.IMAGE_NO_PULL_SECRET,
                    Severity.HIGH,
                    "The pod declares no imagePullSecrets and its service account "
                    "supplies none, so private registries cannot be authenticated.",
                    _ref(entry),
                    _provenance(entry),
                    {
                        "images": [
                            container.get("image", "") for container in spec.get("containers", [])
                        ]
                    },
                )
            )

        return signals

    def _pull_failing(self, spec: dict[str, Any]) -> bool:
        return any(
            (container.get("current_state", {}) or {}).get("reason") in PULL_FAILURE_REASONS
            for container in spec.get("containers", [])
        )


class QuotaRule:
    """Namespace quotas that are already fully consumed."""

    id = "deep.quotas"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.QUOTAS):
            target = _ref(entry)
            evidence = _provenance(entry)

            for quota in entry["data"].get("items", []):
                hard = quota.get("hard", {})
                used = quota.get("used", {})
                exhausted = [
                    resource
                    for resource, limit in hard.items()
                    if self._normalize(used.get(resource)) >= self._normalize(limit) > 0
                ]

                if exhausted:
                    signals.append(
                        Signal.create(
                            SignalType.QUOTA_EXCEEDED,
                            Severity.HIGH,
                            f"ResourceQuota '{quota.get('name', '')}' is fully consumed "
                            f"for: {', '.join(sorted(exhausted))}.",
                            target,
                            evidence,
                            {"quota": quota.get("name", ""), "exhausted": sorted(exhausted)},
                        )
                    )

        return signals

    def _normalize(self, value: Any) -> float:
        """Best-effort numeric comparison of Kubernetes quantity strings."""
        if value is None:
            return 0.0
        text = str(value).strip()
        multipliers = {
            "m": 0.001,
            "k": 1e3,
            "M": 1e6,
            "G": 1e9,
            "T": 1e12,
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
        }
        for suffix in sorted(multipliers, key=len, reverse=True):
            if text.endswith(suffix):
                try:
                    return float(text[: -len(suffix)]) * multipliers[suffix]
                except ValueError:
                    return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0


class StorageClassRule:
    """Storage class conditions that explain a claim that never binds."""

    id = "deep.storage_classes"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.STORAGE_CLASSES):
            target = _ref(entry)
            evidence = _provenance(entry)
            classes = entry["data"].get("items", [])

            if classes and not any(item.get("is_default") for item in classes):
                signals.append(
                    Signal.create(
                        SignalType.STORAGE_NO_DEFAULT_CLASS,
                        Severity.HIGH,
                        "No default StorageClass is set, so a claim without an "
                        "explicit storageClassName will never bind.",
                        target,
                        evidence,
                        {"classes": [item.get("name", "") for item in classes]},
                    )
                )

            waiting = [
                item.get("name", "")
                for item in classes
                if item.get("volume_binding_mode") == "WaitForFirstConsumer"
            ]
            if waiting:
                signals.append(
                    Signal.create(
                        SignalType.STORAGE_WAIT_FOR_CONSUMER,
                        Severity.INFO,
                        f"StorageClass(es) {', '.join(waiting)} use "
                        f"WaitForFirstConsumer, so a claim stays Pending until a pod "
                        f"that uses it is scheduled.",
                        target,
                        evidence,
                        {"classes": waiting},
                    )
                )

        return signals


class NetworkPolicyRule:
    """Default-deny policies that would silently drop traffic."""

    id = "deep.network_policies"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.NETWORK_POLICIES):
            # Each signal targets the policy it names, not the namespace the
            # evidence was collected against.
            evidence = _provenance(entry)

            for policy in entry["data"].get("items", []):
                if not policy.get("denies_all_ingress"):
                    continue

                signals.append(
                    Signal.create(
                        SignalType.NETWORK_POLICY_DENIES_ALL,
                        Severity.HIGH,
                        f"NetworkPolicy '{policy.get('name', '')}' denies all ingress "
                        f"in namespace {policy.get('namespace', '')}.",
                        ResourceRef(
                            kind="NetworkPolicy",
                            name=policy.get("name", "unknown"),
                            namespace=policy.get("namespace"),
                        ),
                        evidence,
                        {"policy": policy.get("name", "")},
                    )
                )

        return signals


class DnsWorkloadRule:
    """Health of the CoreDNS pods themselves."""

    id = "deep.dns_workload"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(EvidenceKind.DNS_WORKLOAD):
            payload = entry["data"]
            pods = payload.get("pods", [])
            ready = payload.get("ready_count", 0)

            if pods and ready:
                continue

            signals.append(
                Signal.create(
                    SignalType.DNS_WORKLOAD_UNHEALTHY,
                    Severity.CRITICAL,
                    "No CoreDNS pod is ready; cluster name resolution is unavailable."
                    if pods
                    else "No CoreDNS pods are running in kube-system.",
                    _ref(entry),
                    _provenance(entry),
                    {"pod_count": len(pods), "ready_count": ready},
                )
            )

        return signals


DEEP_SIGNAL_RULES = (
    ContainerTerminationRule(),
    ProbeConfigurationRule(),
    ConfigReferenceRule(),
    SchedulingEventRule(),
    ImagePullEventRule(),
    ImagePullSecretRule(),
    QuotaRule(),
    StorageClassRule(),
    NetworkPolicyRule(),
    DnsWorkloadRule(),
)
