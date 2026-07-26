"""Remediation planning and patch generation."""

import json
from typing import ClassVar

import pytest
import yaml

from app.analysis.models import AnalysisResult, Hypothesis, Severity, Signal, SignalType
from app.evidence.models import ResourceRef
from app.remediation.models import ChangeKind, PatchFormat, RiskLevel
from app.remediation.planner import RemediationPlanner
from app.remediation.rules import double_quantity

PLANNER = RemediationPlanner()

POD = ResourceRef(kind="Pod", name="web-0", namespace="prod")


def signal(signal_type, severity=Severity.CRITICAL, target=POD, **attributes):
    return Signal.create(
        signal_type,
        severity,
        f"{signal_type} on {target.name}",
        target,
        (f"k8s.pod.spec:{target.key}",),
        attributes,
    )


def hypothesis(hypothesis_id, title="Something is wrong", target=POD, supporting=()):
    return Hypothesis(
        id=hypothesis_id,
        title=title,
        category="workload",
        severity=Severity.CRITICAL,
        confidence=80,
        rationale="Because the evidence says so.",
        target=target,
        supporting_signal_ids=tuple(item.id for item in supporting),
        missing_evidence=("Something that would confirm it",),
        remediation_hint="Do the sensible thing.",
    )


def investigation(pod_spec=None):
    payload = {"context": "test", "health": {"status": "issues_found"}}
    if pod_spec is not None:
        payload["deep_evidence"] = {
            "k8s.pod.spec": [
                {
                    "id": f"k8s.pod.spec:{POD.key}",
                    "target": POD.to_dict(),
                    "status": "ok",
                    "data": pod_spec,
                }
            ]
        }
    return payload


OOM_POD_SPEC = {
    "pod": "web-0",
    "namespace": "prod",
    "owner": {
        "kind": "ReplicaSet",
        "name": "web-5d4f8c9b7",
        "workload_kind": "Deployment",
        "workload_name": "web",
        "workload_derived": True,
    },
    "containers": [
        {
            "name": "web",
            "image": "registry.example.com/web:1.4.2",
            "restart_count": 7,
            "limits": {"memory": "512Mi", "cpu": "1"},
            "last_state": {"reason": "OOMKilled", "exit_code": 137},
            "probes": {},
        }
    ],
}


def plan_for(hypothesis_id, signals=(), pod_spec=None, **kwargs):
    analysis = AnalysisResult(
        signals=tuple(signals),
        hypotheses=(hypothesis(hypothesis_id, supporting=signals, **kwargs),),
    )
    return PLANNER.plan(analysis, investigation(pod_spec))


class TestQuantities:
    @pytest.mark.parametrize(
        "value,expected",
        [("512Mi", "1024Mi"), ("1Gi", "2Gi"), ("250m", "500m"), ("2", "4")],
    )
    def test_doubles_preserving_the_unit(self, value, expected):
        assert double_quantity(value) == expected

    def test_returns_none_for_an_unparseable_quantity(self):
        assert double_quantity("lots") is None


class TestOutOfMemory:
    def test_targets_the_owning_deployment_not_the_pod(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )

        assert plan.target.kind == "Deployment"
        assert plan.target.name == "web"
        assert plan.target.namespace == "prod"

    def test_flags_a_derived_workload_name_as_unverified(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )
        assert any("derived" in caveat for caveat in plan.caveats)

    def test_proposes_double_the_current_limit(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )

        assert "1024Mi" in plan.summary
        patch = next(p for p in plan.patches if p.format is PatchFormat.KUBECTL)
        body = json.loads(patch.content)
        container = body["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "web"
        assert container["resources"]["limits"]["memory"] == "1024Mi"

    def test_refuses_to_invent_a_limit_when_none_exists(self):
        spec = {
            **OOM_POD_SPEC,
            "containers": [{**OOM_POD_SPEC["containers"][0], "limits": {}}],
        }
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            spec,
        )

        assert any("no value can be proposed" in caveat for caveat in plan.caveats)
        assert "observed" in plan.summary

    def test_states_risk_blast_radius_and_rollback(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )

        assert plan.risk.level is RiskLevel.MEDIUM
        assert plan.risk.change_kind is ChangeKind.RESOURCE_LIMITS
        assert plan.risk.restart_required is True
        assert "web" in plan.risk.blast_radius
        assert plan.rollback
        assert plan.verification

    def test_emits_kubectl_kustomize_and_helm_artifacts(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )
        formats = {patch.format for patch in plan.patches}
        assert formats == {
            PatchFormat.KUBECTL,
            PatchFormat.KUSTOMIZE,
            PatchFormat.HELM_VALUES,
        }

    def test_generated_yaml_parses(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )
        for patch in plan.patches:
            if patch.format is PatchFormat.KUBECTL:
                json.loads(patch.content)
            else:
                # Kustomize output embeds a second document as a comment block.
                assert list(yaml.safe_load_all(patch.content))


class TestUnmanagedWorkloads:
    """A bare pod has no controller: rollout commands do not apply to it."""

    UNOWNED: ClassVar[dict] = {**OOM_POD_SPEC, "owner": {}}

    def test_never_emits_rollout_commands_for_a_bare_pod(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            self.UNOWNED,
        )

        assert plan.target.kind == "Pod"
        for step in (*plan.remediation, *plan.verification, *plan.rollback):
            assert "rollout" not in (step.command or ""), (
                f"rollout is invalid against a Pod: {step.command}"
            )

    def test_explains_that_a_bare_pod_cannot_be_rolled_back(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            self.UNOWNED,
        )

        assert any(step.manual for step in plan.rollback)
        assert any("no controller owns" in caveat.lower() for caveat in plan.caveats)

    def test_restart_becomes_a_manual_step_for_a_bare_pod(self):
        plan = plan_for(
            "workload.missing_configuration",
            [
                signal(
                    SignalType.CONFIG_KEY_MISSING,
                    kind="ConfigMap",
                    name="web-config",
                    missing_keys=["DB_HOST"],
                )
            ],
            self.UNOWNED,
        )

        restart = [step for step in plan.remediation if "ecreate" in step.description]
        assert restart and restart[0].manual


class TestMissingConfiguration:
    def test_names_the_missing_key(self):
        plan = plan_for(
            "workload.missing_configuration",
            [
                signal(
                    SignalType.CONFIG_KEY_MISSING,
                    kind="ConfigMap",
                    name="web-config",
                    missing_keys=["DB_HOST"],
                )
            ],
            OOM_POD_SPEC,
        )

        assert "DB_HOST" in plan.summary
        assert "web-config" in plan.title
        assert plan.risk.level is RiskLevel.LOW

    def test_generates_a_configmap_manifest_with_placeholder_values(self):
        plan = plan_for(
            "workload.missing_configuration",
            [
                signal(
                    SignalType.CONFIG_KEY_MISSING,
                    kind="ConfigMap",
                    name="web-config",
                    missing_keys=["DB_HOST"],
                )
            ],
            OOM_POD_SPEC,
        )

        patch = next(p for p in plan.patches if p.format is PatchFormat.YAML_MANIFEST)
        body = yaml.safe_load(patch.content)
        assert body["kind"] == "ConfigMap"
        assert body["metadata"]["name"] == "web-config"
        assert body["data"] == {"DB_HOST": "<value>"}

    def test_never_generates_secret_values(self):
        plan = plan_for(
            "workload.missing_configuration",
            [
                signal(
                    SignalType.CONFIG_REFERENCE_MISSING,
                    kind="Secret",
                    name="db-creds",
                )
            ],
            OOM_POD_SPEC,
        )

        assert plan.patches == ()
        assert any("never reads or writes secret" in caveat for caveat in plan.caveats)


class TestHighRiskOperations:
    def test_node_drain_is_critical_and_lists_disruption_budget_checks(self):
        node = ResourceRef(kind="Node", name="node-1")
        analysis = AnalysisResult(hypotheses=(hypothesis("node.unhealthy", target=node),))
        plan = PLANNER.plan(analysis, investigation())

        assert plan.risk.level is RiskLevel.CRITICAL
        assert plan.risk.change_kind is ChangeKind.INFRASTRUCTURE
        assert any("poddisruptionbudget" in (step.command or "") for step in plan.preconditions)
        assert any(step.manual for step in plan.preconditions)

    def test_default_storage_class_is_high_risk_because_it_is_cluster_wide(self):
        plan = plan_for(
            "storage.no_default_storage_class",
            [
                signal(
                    SignalType.STORAGE_NO_DEFAULT_CLASS,
                    target=ResourceRef(kind="Cluster", name="test"),
                    classes=["slow"],
                )
            ],
        )

        assert plan.risk.level is RiskLevel.HIGH
        assert "cluster" in plan.risk.blast_radius.lower()

    def test_network_policy_change_states_its_placeholders(self):
        policy = ResourceRef(kind="NetworkPolicy", name="deny-all", namespace="prod")
        analysis = AnalysisResult(
            hypotheses=(hypothesis("network.ingress_denied_by_policy", target=policy),)
        )
        plan = PLANNER.plan(analysis, investigation())

        assert plan.risk.level is RiskLevel.HIGH
        assert any("placeholder" in caveat for caveat in plan.caveats)


class TestPermissions:
    def test_plans_declare_the_rbac_they_need(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )

        assert plan.required_permissions
        permission = plan.required_permissions[0]
        assert "patch" in permission.verbs
        assert permission.check_command.startswith("kubectl auth can-i")
        assert "-n prod" in permission.check_command


class TestFallback:
    def test_unknown_hypothesis_yields_a_diagnostic_plan_not_a_guess(self):
        plan = plan_for("workload.something_unmodelled")

        assert plan.id == "diagnostic-only"
        assert plan.risk.change_kind is ChangeKind.NONE
        assert plan.requires_approval is False
        assert plan.patches == ()
        # It proposes investigation, never a change.
        assert all(step.manual or step.command for step in plan.remediation)

    def test_diagnostic_plan_uses_the_hypothesis_missing_evidence(self):
        plan = plan_for("workload.something_unmodelled")
        assert any(
            "Something that would confirm it" in step.description for step in plan.remediation
        )

    def test_no_hypothesis_yields_no_plan(self):
        assert PLANNER.plan(AnalysisResult(), investigation()) is None

    def test_a_failing_rule_degrades_to_the_diagnostic_plan(self):
        class Exploding:
            hypothesis_id = "workload.out_of_memory"

            def build(self, context):
                raise RuntimeError("rule bug")

        planner = RemediationPlanner([Exploding()])
        analysis = AnalysisResult(hypotheses=(hypothesis("workload.out_of_memory"),))
        plan = planner.plan(analysis, investigation())

        assert plan is not None
        assert plan.id == "diagnostic-only"


class TestProvenance:
    def test_plans_cite_the_signals_and_evidence_behind_them(self):
        oom = signal(SignalType.CONTAINER_OOM_EXIT, container="web")
        plan = plan_for("workload.out_of_memory", [oom], OOM_POD_SPEC)

        assert oom.id in plan.signal_ids
        assert f"k8s.pod.spec:{POD.key}" in plan.evidence_ids


class TestLegacyProjection:
    def test_risk_projects_to_the_shape_the_console_reads(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )
        legacy = plan.risk.to_legacy()

        assert legacy["level"] == "Medium"
        assert legacy["impact"][0] == "Restart Required"
        assert "Rollback Available" in legacy["impact"]

    def test_plan_projects_to_the_shape_the_console_reads(self):
        plan = plan_for(
            "workload.out_of_memory",
            [signal(SignalType.CONTAINER_OOM_EXIT, container="web")],
            OOM_POD_SPEC,
        )
        legacy = plan.to_legacy_plan()

        assert set(legacy) == {
            "requires_approval",
            "dry_run_first",
            "pre_checks",
            "review_commands",
            "rollback_commands",
        }
        assert legacy["rollback_commands"]
