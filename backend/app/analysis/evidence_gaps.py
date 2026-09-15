"""Which of a hypothesis's missing evidence is still missing.

`SignalPatternRule.missing_evidence` is written once per rule and was printed
as-is — under Lessons Learned as "evidence that would have shortened this
investigation", and in a diagnostic plan as manual "Establish:" steps. A
playbook round collects much of it, and nothing checked. On the QA cluster a
deployment-scoped investigation of `checkout` said it lacked "Container exit
code and termination reason" and told the operator to establish it by hand,
on a page that also showed "Container checkout last terminated with exit code
1 (Error)" — read from the pod spec that round had collected.

The items are prose for people, so the connection to a record is stated here
rather than inferred: only items a collected record fully answers are listed,
and an item absent from `ANSWERED_BY` is always reported as outstanding, which
is the safe direction. Raw evidence kinds used as items map to themselves.
"""

from typing import Any

from app.analysis.models import Hypothesis
from app.evidence.models import EvidenceKind

ANSWERED_BY: dict[str, str] = {
    "Previous container logs (--previous) from before the last restart": (
        EvidenceKind.POD_LOGS_PREVIOUS
    ),
    "Liveness, readiness, and startup probe definitions": EvidenceKind.POD_SPEC,
    "Probe definitions including path, port, and thresholds": EvidenceKind.POD_SPEC,
    "ConfigMap and Secret keys referenced by the container": EvidenceKind.CONFIG_REFS,
    "Container exit code and termination reason": EvidenceKind.POD_SPEC,
    "Container exit code (137 confirms an OOM kill)": EvidenceKind.POD_SPEC,
    "Container memory limit and request values": EvidenceKind.POD_SPEC,
    "Whether the reference is marked optional in the pod spec": EvidenceKind.POD_SPEC,
    "imagePullSecrets referenced by the pod spec": EvidenceKind.POD_SPEC,
    "EndpointSlice contents for the service": EvidenceKind.ENDPOINT_SLICES,
    # The network inspector matches every Service selector against the pod
    # inventory the same investigation collected; the comparison is its output.
    "Service selector versus actual pod labels": EvidenceKind.NETWORK,
    "Readiness state of the pods the selector should match": EvidenceKind.PODS,
    "StorageClass and its provisioner": EvidenceKind.STORAGE_CLASSES,
    "VolumeAttachment state for the target node": EvidenceKind.VOLUME_ATTACHMENTS,
}

# Kinds read per pod: a record of one pod's spec answers nothing about another.
_PER_POD = frozenset(
    {
        EvidenceKind.POD_SPEC,
        EvidenceKind.POD_LOGS_PREVIOUS,
        EvidenceKind.CONFIG_REFS,
        EvidenceKind.RESOURCE_EVENTS,
    }
)
_USABLE = frozenset({"ok", "empty"})


def outstanding(hypothesis: Hypothesis, investigation: dict[str, Any]) -> tuple[str, ...]:
    """The hypothesis's missing evidence minus what was usably collected for it."""
    held = {
        entry.get("id", "")
        for entry in investigation.get("evidence") or []
        if isinstance(entry, dict) and str(entry.get("status", "")) in _USABLE
    }
    held_kinds = {evidence_id.split(":", 1)[0] for evidence_id in held}

    remaining = []
    for item in hypothesis.missing_evidence:
        kind = ANSWERED_BY.get(item) or (item if item in EvidenceKind.__dict__.values() else None)
        if kind is None:
            remaining.append(item)
        elif kind in _PER_POD:
            if f"{kind}:{hypothesis.target.key}" not in held:
                remaining.append(item)
        elif kind not in held_kinds:
            remaining.append(item)
    return tuple(remaining)
