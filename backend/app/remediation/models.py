"""Remediation domain model.

A remediation plan is derived from a *hypothesis*, not from raw investigation
heuristics: the hypothesis already knows what is wrong and on which resource, so
the plan can be specific about risk, blast radius, and rollback.

Nothing here executes anything. Commands are strings for a human to review; the
kubectl executor's read-only policy makes running them structurally impossible
from inside the platform.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.evidence.models import ResourceRef


class RiskLevel(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"

    @property
    def weight(self) -> int:
        return _RISK_WEIGHTS[self]


_RISK_WEIGHTS = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class ChangeKind(StrEnum):
    """What class of change the remediation makes."""

    NONE = "none"
    CONFIG = "configuration"
    RESOURCE_LIMITS = "resource-limits"
    IMAGE = "image"
    SCALE = "scale"
    ROLLOUT = "rollout"
    NETWORK_POLICY = "network-policy"
    STORAGE = "storage"
    QUOTA = "quota"
    INFRASTRUCTURE = "infrastructure"


class PatchFormat(StrEnum):
    KUBECTL = "kubectl"
    YAML_MANIFEST = "yaml"
    HELM_VALUES = "helm-values"
    KUSTOMIZE = "kustomize"


@dataclass(frozen=True, slots=True)
class RemediationStep:
    """One reviewable action.

    `manual` marks a step that cannot be a command — an approval, a decision, or
    something needing information the platform does not have.
    """

    description: str
    command: str | None = None
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "command": self.command,
            "manual": self.manual,
        }


@dataclass(frozen=True, slots=True)
class Permission:
    """RBAC a operator needs to carry out a step."""

    verbs: tuple[str, ...]
    resources: tuple[str, ...]
    namespace: str | None = None

    @property
    def check_command(self) -> str:
        """`kubectl auth can-i`, so permission can be confirmed before starting."""
        scope = f" -n {self.namespace}" if self.namespace else " --all-namespaces"
        return f"kubectl auth can-i {self.verbs[0]} {self.resources[0]}{scope}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verbs": list(self.verbs),
            "resources": list(self.resources),
            "namespace": self.namespace,
            "check_command": self.check_command,
        }


@dataclass(frozen=True, slots=True)
class Patch:
    """A generated change artifact. Never applied by the platform."""

    format: PatchFormat
    filename: str
    content: str
    description: str
    apply_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": str(self.format),
            "filename": self.filename,
            "content": self.content,
            "description": self.description,
            "apply_command": self.apply_command,
        }


@dataclass(frozen=True, slots=True)
class RemediationRisk:
    level: RiskLevel
    change_kind: ChangeKind
    restart_required: bool
    estimated_downtime: str
    blast_radius: str
    reversible: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": str(self.level),
            "change_kind": str(self.change_kind),
            "restart_required": self.restart_required,
            "estimated_downtime": self.estimated_downtime,
            "blast_radius": self.blast_radius,
            "reversible": self.reversible,
            "notes": list(self.notes),
        }

    def to_legacy(self) -> dict[str, Any]:
        """The `remediation_risk` shape the existing UI and reports consume."""
        return {
            "level": str(self.level),
            "impact": [
                "Restart Required" if self.restart_required else "No Restart Required",
                self.estimated_downtime,
                "Rollback Available" if self.reversible else "Rollback Not Automatic",
            ],
        }


@dataclass(frozen=True)
class RemediationPlan:
    """An evidence-backed, reviewable plan for one hypothesis."""

    id: str
    hypothesis_id: str
    title: str
    summary: str
    target: ResourceRef
    risk: RemediationRisk
    requires_approval: bool
    preconditions: tuple[RemediationStep, ...] = ()
    remediation: tuple[RemediationStep, ...] = ()
    verification: tuple[RemediationStep, ...] = ()
    rollback: tuple[RemediationStep, ...] = ()
    required_permissions: tuple[Permission, ...] = ()
    patches: tuple[Patch, ...] = ()
    signal_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    caveats: tuple[str, ...] = field(default=())

    @property
    def commands(self) -> list[str]:
        return [
            step.command
            for step in (*self.preconditions, *self.remediation, *self.verification)
            if step.command
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hypothesis_id": self.hypothesis_id,
            "title": self.title,
            "summary": self.summary,
            "target": self.target.to_dict(),
            "risk": self.risk.to_dict(),
            "requires_approval": self.requires_approval,
            "preconditions": [step.to_dict() for step in self.preconditions],
            "remediation": [step.to_dict() for step in self.remediation],
            "verification": [step.to_dict() for step in self.verification],
            "rollback": [step.to_dict() for step in self.rollback],
            "required_permissions": [item.to_dict() for item in self.required_permissions],
            "patches": [patch.to_dict() for patch in self.patches],
            "signal_ids": list(self.signal_ids),
            "evidence_ids": list(self.evidence_ids),
            "caveats": list(self.caveats),
        }

    def to_legacy_plan(self) -> dict[str, Any]:
        """The `remediation_plan` shape the existing UI and reports consume."""
        return {
            "requires_approval": self.requires_approval,
            "dry_run_first": True,
            "pre_checks": [step.description for step in self.preconditions],
            "review_commands": [step.command for step in self.remediation if step.command][:3],
            "rollback_commands": [step.command for step in self.rollback if step.command]
            or ["kubectl rollout history deployment <deployment-name> -n <namespace>"],
        }
