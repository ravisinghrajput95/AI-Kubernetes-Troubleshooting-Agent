"""The platform generates changes; it must never be able to apply them.

These tests pin that as a structural property rather than a convention: the
read-only command policy that guards every cluster read would *reject* the
remediation commands, so there is no path by which the platform could execute
its own recommendations.
"""

import pytest

from app.analysis.models import AnalysisResult, Hypothesis, Severity, Signal, SignalType
from app.evidence.models import ResourceRef
from app.kubernetes.command_policy import UnsafeKubectlCommand, assert_read_only
from app.remediation.planner import RemediationPlanner
from app.remediation.rules import DEFAULT_REMEDIATION_RULES

PLANNER = RemediationPlanner()
POD = ResourceRef(kind="Pod", name="web-0", namespace="prod")

POD_SPEC = {
    "pod": "web-0",
    "namespace": "prod",
    "owner": {
        "kind": "ReplicaSet",
        "name": "web-abc12",
        "workload_kind": "Deployment",
        "workload_name": "web",
        "workload_derived": True,
    },
    "containers": [
        {
            "name": "web",
            "image": "reg/web:1",
            "restart_count": 3,
            "limits": {"memory": "512Mi"},
            "last_state": {"reason": "OOMKilled", "exit_code": 137},
            "probes": {"liveness": {"initial_delay_seconds": 1, "path": "/h", "port": 8080}},
        }
    ],
}

TARGETS = {
    "storage.no_default_storage_class": ResourceRef(kind="Cluster", name="test"),
    "node.unhealthy": ResourceRef(kind="Node", name="node-1"),
    "network.ingress_denied_by_policy": ResourceRef(
        kind="NetworkPolicy", name="deny-all", namespace="prod"
    ),
    "network.service_without_endpoints": ResourceRef(kind="Service", name="api", namespace="prod"),
    "scheduling.quota_exhausted": ResourceRef(
        kind="ResourceQuota", name="team-quota", namespace="prod"
    ),
}

ALL_SIGNALS = [
    Signal.create(
        signal_type,
        Severity.CRITICAL,
        "observed",
        POD,
        (f"k8s.pod.spec:{POD.key}",),
        {
            "container": "web",
            "kind": "ConfigMap",
            "name": "web-config",
            "missing_keys": ["DB_HOST"],
            "quota": "team-quota",
            "exhausted": ["requests.memory"],
            "classes": ["slow"],
        },
    )
    for signal_type in (
        SignalType.CONTAINER_OOM_EXIT,
        SignalType.CONFIG_KEY_MISSING,
        SignalType.IMAGE_PULL_UNAUTHORIZED,
        SignalType.QUOTA_EXCEEDED,
        SignalType.STORAGE_NO_DEFAULT_CLASS,
        SignalType.NETWORK_POLICY_DENIES_ALL,
        SignalType.EVENT_PROBE_FAILURE,
    )
]

RULE_IDS = [rule.hypothesis_id for rule in DEFAULT_REMEDIATION_RULES]


def build_plan(hypothesis_id):
    target = TARGETS.get(hypothesis_id, POD)
    analysis = AnalysisResult(
        signals=tuple(ALL_SIGNALS),
        hypotheses=(
            Hypothesis(
                id=hypothesis_id,
                title="t",
                category="c",
                severity=Severity.CRITICAL,
                confidence=80,
                rationale="r",
                target=target,
                supporting_signal_ids=tuple(s.id for s in ALL_SIGNALS),
                missing_evidence=("x",),
                remediation_hint="h",
            ),
        ),
    )
    return PLANNER.plan(
        analysis,
        {
            "deep_evidence": {
                "k8s.pod.spec": [
                    {
                        "id": f"k8s.pod.spec:{POD.key}",
                        "target": POD.to_dict(),
                        "status": "ok",
                        "data": POD_SPEC,
                    }
                ]
            }
        },
    )


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_every_rule_builds_a_complete_plan(hypothesis_id):
    plan = build_plan(hypothesis_id)

    assert plan.title and plan.summary
    assert plan.risk.estimated_downtime
    assert plan.risk.blast_radius
    assert plan.remediation, "a plan must propose at least one action"
    assert plan.verification, "a plan must say how to confirm it worked"
    assert plan.required_permissions, "a plan must state the access it needs"


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_every_mutating_command_is_rejected_by_the_read_only_policy(hypothesis_id):
    """The commands are real changes, and the executor structurally cannot run them."""
    plan = build_plan(hypothesis_id)

    mutating = [
        step.command for step in plan.remediation if step.command and _is_mutating(step.command)
    ]

    for command in mutating:
        args = _kubectl_args(command)
        if args is None:
            continue
        with pytest.raises(UnsafeKubectlCommand):
            assert_read_only(args)


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_precondition_and_verification_commands_are_read_only(hypothesis_id):
    """Everything the operator runs *before* changing anything must be safe."""
    plan = build_plan(hypothesis_id)

    for step in (*plan.preconditions, *plan.verification):
        if not step.command:
            continue
        args = _kubectl_args(step.command)
        if args is None:
            continue
        assert_read_only(args)


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_changes_require_approval(hypothesis_id):
    plan = build_plan(hypothesis_id)
    assert plan.requires_approval is True


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_patch_apply_commands_are_never_executed_only_described(hypothesis_id):
    plan = build_plan(hypothesis_id)

    for patch in plan.patches:
        assert isinstance(patch.content, str)
        if patch.apply_command:
            # A server-side dry run is always offered before the real apply.
            assert "--dry-run" in patch.apply_command


def _kubectl_args(command: str) -> list[str] | None:
    """First kubectl invocation in a possibly multi-line step, as argv."""
    for line in command.splitlines():
        stripped = line.strip()
        if stripped.startswith("kubectl "):
            return stripped.split()[1:]
    return None


MUTATING_VERBS = (
    "patch",
    "apply",
    "edit",
    "create",
    "delete",
    "drain",
    "cordon",
    "uncordon",
    "rollout",
    "scale",
    "replace",
)


def _is_mutating(command: str) -> bool:
    args = _kubectl_args(command)
    return bool(args) and args[0] in MUTATING_VERBS


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_a_rollback_never_applies_a_file_no_step_wrote(hypothesis_id):
    """Read off a live report: the service plan's rollback was
    `kubectl apply -f checkout-svc-before.yaml`, and no step anywhere in the
    plan wrote that file — so the undo an operator would reach for mid-incident
    failed with "the path does not exist"."""
    plan = build_plan(hypothesis_id)
    written = {
        step.command.rsplit(">", 1)[1].strip()
        for step in (*plan.preconditions, *plan.remediation)
        if step.command and ">" in step.command
    }
    for step in plan.rollback:
        for word in (step.command or "").split():
            if word.endswith("-before.yaml"):
                assert word in written, f"{hypothesis_id} rolls back from {word}, never written"


@pytest.mark.parametrize("hypothesis_id", RULE_IDS)
def test_the_access_check_asks_about_the_change_not_the_read(hypothesis_id):
    """Every plan printed `kubectl auth can-i get …` for access it said was
    `get, patch`: the check answers yes for any read-only operator, who then
    finds out at the edit step."""
    plan = build_plan(hypothesis_id)
    for permission in plan.required_permissions:
        changes = [verb for verb in permission.verbs if verb not in {"get", "list", "watch"}]
        if changes:
            assert f"can-i {changes[0]} " in permission.check_command, permission.check_command
