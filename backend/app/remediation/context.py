from dataclasses import dataclass
from typing import Any

from app.analysis.models import AnalysisResult, Hypothesis, Signal
from app.evidence.models import EvidenceKind, ResourceRef


@dataclass(frozen=True)
class RemediationContext:
    """What a remediation rule sees.

    Rules read the hypothesis, the signals that support it, and the targeted
    evidence a playbook collected — so a plan can name the actual container,
    its current limits, and the workload that owns it.
    """

    hypothesis: Hypothesis
    analysis: AnalysisResult
    investigation: dict[str, Any]

    @property
    def target(self) -> ResourceRef:
        return self.hypothesis.target

    @property
    def namespace(self) -> str | None:
        return self.hypothesis.target.namespace

    def supporting(self, *signal_types: str) -> list[Signal]:
        wanted = set(signal_types)
        return [
            signal
            for signal_id in self.hypothesis.supporting_signal_ids
            if (signal := self.analysis.signal(signal_id)) and signal.type in wanted
        ]

    def first(self, *signal_types: str) -> Signal | None:
        matches = self.supporting(*signal_types)
        return matches[0] if matches else None

    def deep(self, kind: str) -> list[dict[str, Any]]:
        deep_evidence = self.investigation.get("deep_evidence", {})
        if not isinstance(deep_evidence, dict):
            return []
        return [
            entry
            for entry in deep_evidence.get(kind, [])
            if isinstance(entry, dict) and isinstance(entry.get("data"), dict)
        ]

    def pod_spec(self) -> dict[str, Any] | None:
        """Targeted pod evidence for this hypothesis's target, if collected."""
        for entry in self.deep(EvidenceKind.POD_SPEC):
            target = entry.get("target", {})
            if target.get("name") == self.target.name:
                return entry["data"]
        return None

    def workload_ref(self) -> ResourceRef:
        """The resource an operator would actually change.

        Falls back to the hypothesis target when ownership is unknown, so a plan
        is always addressed to something concrete.
        """
        spec = self.pod_spec()
        owner = (spec or {}).get("owner", {})
        if owner.get("workload_name"):
            return ResourceRef(
                kind=owner.get("workload_kind", "Deployment"),
                name=owner["workload_name"],
                namespace=self.namespace,
            )
        return self.target

    def container(self) -> dict[str, Any] | None:
        """The container implicated by the supporting signals, else the first."""
        spec = self.pod_spec()
        if not spec:
            return None

        containers = spec.get("containers", [])
        named = {
            signal.attributes.get("container")
            for signal in self.analysis.signals
            if signal.attributes.get("container")
        }
        for container in containers:
            if container.get("name") in named:
                return container
        return containers[0] if containers else None

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(self.analysis.evidence_ids_for(self.hypothesis.supporting_signal_ids))

    def workload_derived(self) -> bool:
        """True when the workload name was inferred rather than read directly."""
        spec = self.pod_spec() or {}
        return bool(spec.get("owner", {}).get("workload_derived"))
