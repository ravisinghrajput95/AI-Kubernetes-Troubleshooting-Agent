"""A pod waiting on configuration, through the whole pipeline.

`CreateContainerConfigError` is the canonical missing-ConfigMap fault, and on
a live cluster its investigation said:

    ConfigMap <name-from-the-pod-spec> ... no collected evidence names which one
    No controller owns pod/notifier-6fbf6f6dc-v89dg

while the pod's own events named `notifier-config-does-not-exist` and its
ReplicaSet was one read away. No playbook triggered on the fault, so the
reference resolver never ran for it — it ran only under CrashLoop, and every
pipeline fixture here was a crash-looping pod, where it always did. Each half
was tested and correct: the resolver on crash loops, the rule on a supplied
signal. What joined them was never exercised.

The other two tests are the same seam failing the other way: a read that
*failed* was recorded as a ConfigMap or Secret that does not exist.
"""

import json

import pytest

from app.ai.providers import Completion
from app.ai.root_cause_analyzer import RootCauseAnalyzer
from app.analysis.engine import AnalysisEngine
from app.kubernetes.kubectl_executor import KubectlResult
from app.providers.base import ProviderUnsupported, ReadVerb, ResourceRequest
from app.providers.remote_agent import spec_for
from tests.test_investigation_service import NODES, FakeKubectl, build_service

POD = "notifier-6fbf6f6dc-v89dg"

PODS = {
    "items": [
        {
            "metadata": {"name": POD, "namespace": "payments"},
            "spec": {
                "nodeName": "node-1",
                "containers": [{"name": "notifier", "image": "busybox:1.36"}],
            },
            "status": {
                "phase": "Pending",
                "containerStatuses": [
                    {
                        "name": "notifier",
                        "ready": False,
                        "restartCount": 0,
                        "state": {"waiting": {"reason": "CreateContainerConfigError"}},
                    }
                ],
            },
        }
    ]
}


def pod_detail(reference: dict) -> dict:
    return {
        "metadata": {
            "name": POD,
            "namespace": "payments",
            "labels": {"app": "notifier", "pod-template-hash": "6fbf6f6dc"},
            "ownerReferences": [
                {"kind": "ReplicaSet", "name": "notifier-6fbf6f6dc", "controller": True}
            ],
        },
        "spec": {
            "nodeName": "node-1",
            "containers": [
                {
                    "name": "notifier",
                    "image": "busybox:1.36",
                    "resources": {"limits": {"memory": "64Mi"}},
                    "env": [{"name": "SETTING", "valueFrom": reference}],
                }
            ],
        },
        "status": PODS["items"][0]["status"],
    }


class ConfigErrorCluster(FakeKubectl):
    """A pod that cannot start because of a reference, and how that reads."""

    def __init__(self, reference: dict, reference_read: KubectlResult | None = None):
        super().__init__()
        self.reference = reference
        self.reference_read = reference_read

    def run(self, args, parse_json=False):
        result = super().run(args, parse_json)
        resource = args[1] if len(args) > 1 else ""
        named = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
        command = result.command

        def answer(payload):
            return KubectlResult(command, True, json.dumps(payload), "", 0, data=payload)

        if resource in {"configmap", "secret"} and named:
            if self.reference_read is not None:
                return KubectlResult(
                    command,
                    self.reference_read.success,
                    self.reference_read.stdout,
                    self.reference_read.stderr,
                    self.reference_read.return_code,
                )
            return KubectlResult(
                command,
                False,
                "",
                f'Error from server (NotFound): {resource}s "{named}" not found',
                1,
            )
        if resource == "pod" and named:
            return answer(pod_detail(self.reference))
        if resource in {"pods", "pod"}:
            return answer(PODS)
        if resource == "nodes":
            return answer(NODES)
        return result


class NoModel:
    def complete(self, messages):
        return Completion(success=False, error="no key")


async def diagnose(cluster: ConfigErrorCluster):
    investigation = await build_service(cluster).run()
    analyzer = RootCauseAnalyzer()
    analyzer.llm_client = NoModel()
    return investigation, analyzer.analyze(investigation)


CONFIGMAP_REF = {"configMapKeyRef": {"name": "notifier-config", "key": "SETTING"}}


class TestAMissingConfigMapIsNamed:
    @pytest.fixture
    async def result(self):
        return await diagnose(ConfigErrorCluster(CONFIGMAP_REF))

    async def test_a_playbook_reads_the_pod_that_cannot_start(self, result):
        investigation, _ = result
        collectors = [c for r in investigation["playbook_rounds"] for c in r["collectors"]]
        assert f"k8s.pod.config_refs:pod/payments/{POD}" in collectors

    async def test_the_plan_names_the_object_and_its_real_owner(self, result):
        _, diagnosis = result
        plan = diagnosis["remediation"]

        assert diagnosis["selected_hypothesis"] == "workload.missing_configuration"
        assert plan["target"] == {
            "kind": "ConfigMap",
            "name": "notifier-config",
            "namespace": "payments",
            "uid": None,
        }
        rendered = json.dumps(plan)
        assert "<name-from-the-pod-spec>" not in rendered
        assert "no controller owns" not in rendered.lower()
        assert "not managed by a controller" not in rendered.lower()
        assert any(
            "deployment notifier" in (step.get("command") or "") for step in plan["rollback"]
        ), plan["rollback"]


class TestAReadThatFailedIsNotAnAbsence:
    async def test_a_refused_configmap_read_does_not_become_a_missing_configmap(self):
        refused = KubectlResult(
            [],
            False,
            "",
            'Error from server (Forbidden): configmaps "notifier-config" is forbidden: '
            'User "alice" cannot get resource "configmaps"',
            1,
        )
        investigation, diagnosis = await diagnose(ConfigErrorCluster(CONFIGMAP_REF, refused))

        # Vacuity: the resolver did run and did record the failed read.
        references = [
            ref
            for entry in investigation["deep_evidence"]["k8s.pod.config_refs"]
            for ref in entry["data"]["references"]
        ]
        assert references and references[0]["exists"] is None

        types = {signal.type for signal in AnalysisEngine().analyze(investigation).signals}
        assert "config.reference_missing" not in types
        assert "does not exist" not in json.dumps(diagnosis["remediation"])

    async def test_the_agents_refusal_to_describe_a_secret_does_not_become_a_missing_secret(self):
        # The exact error the agent path produces: `describe` is kubectl's
        # renderer and has no evidence kind, so this read always fails there.
        with pytest.raises(ProviderUnsupported) as refusal:
            spec_for(ResourceRequest(verb=ReadVerb.DESCRIBE, resource="secret", name="token"))
        agent_answer = KubectlResult([], False, "", str(refusal.value), 1)

        investigation, _ = await diagnose(
            ConfigErrorCluster({"secretKeyRef": {"name": "token", "key": "t"}}, agent_answer)
        )
        references = [
            ref
            for entry in investigation["deep_evidence"]["k8s.pod.config_refs"]
            for ref in entry["data"]["references"]
        ]
        assert references and references[0]["kind"] == "Secret"
        assert references[0]["exists"] is None

        signals = AnalysisEngine().analyze(investigation).signals
        assert not [s for s in signals if s.type == "config.reference_missing"], [
            s.summary for s in signals
        ]


class TestTheReportSaysWhatTheDeepRoundChanged:
    """ "The initial pass alone would not have reached this conclusion" was
    printed under every investigation that ran a playbook, including the ones
    whose baseline had already selected the same cause."""

    @staticmethod
    def lessons(investigation, diagnosis) -> str:
        from app.reports.composer import IncidentReportComposer

        report = IncidentReportComposer().compose(
            diagnosis, investigation, "INC-1", "2026-09-14T00:00:00Z", "payments", "success"
        )
        section = next(s for s in report.sections if s.title == "Lessons Learned")
        return "\n".join(section.as_lines())

    async def test_a_conclusion_the_baseline_had_already_reached_says_so(self):
        investigation, diagnosis = await diagnose(ConfigErrorCluster(CONFIGMAP_REF))
        text = self.lessons(investigation, diagnosis)
        assert "Deep investigation" in text
        assert "would not have reached" not in text
        assert "had already reached this conclusion" in text

    async def test_a_conclusion_the_deep_round_produced_still_says_so(self):
        # The control: the sentence the defect printed everywhere is true here,
        # where the baseline ranked a different cause first.
        investigation, diagnosis = await diagnose(FakeKubectl())
        before = investigation["playbook_rounds"][0]["hypotheses_before"]
        assert before and before[0] != diagnosis["selected_hypothesis"]
        assert "would not have reached" in self.lessons(investigation, diagnosis) or (
            "ranked it first" in self.lessons(investigation, diagnosis)
        )
