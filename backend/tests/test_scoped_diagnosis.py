"""An investigation asked about one resource answers about that resource.

On the QA cluster, the console's form with Resource kind "Deployment" and name
"checkout" produced:

    Pod references configuration that does not exist (pod/payments/notifier-…)

with checkout itself crash-looping on the same page. A resource scope narrows
the pod and deployment reads only, so every other pod's signals were still in
play and the most severe one won. The form promised a subject; the diagnosis
did not honour it — F31's shape at the scope seam.
"""

import json

from app.ai.providers import Completion
from app.ai.root_cause_analyzer import RootCauseAnalyzer
from app.kubernetes.kubectl_executor import KubectlResult
from app.providers.local_kubectl import LocalKubectlProvider
from app.services.investigation_service import InvestigationService
from tests.test_configuration_investigation import CONFIGMAP_REF, PODS, ConfigErrorCluster

CHECKOUT = {
    "metadata": {"name": "checkout-5b5fd56dbf-4cnmv", "namespace": "payments"},
    "spec": {"nodeName": "node-1", "containers": [{"name": "checkout", "image": "busybox:1.36"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {
                "name": "checkout",
                "ready": False,
                "restartCount": 9,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            }
        ],
    },
}


class Namespace(ConfigErrorCluster):
    """The notifier pod from the config-error fixture, plus checkout."""

    def __init__(self, with_checkout: bool = True):
        super().__init__(CONFIGMAP_REF)
        self.with_checkout = with_checkout

    def run(self, args, parse_json=False):
        result = super().run(args, parse_json)
        resource = args[1] if len(args) > 1 else ""
        named = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
        if resource == "pod" and named == CHECKOUT["metadata"]["name"]:
            detail = {
                **CHECKOUT,
                "metadata": {
                    **CHECKOUT["metadata"],
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-5b5fd56dbf"}],
                },
            }
            return KubectlResult(result.command, True, json.dumps(detail), "", 0, data=detail)
        if resource in {"pods", "pod"} and not named:
            items = [*PODS["items"], *([CHECKOUT] if self.with_checkout else [])]
            payload = {"items": items}
            return KubectlResult(result.command, True, json.dumps(payload), "", 0, data=payload)
        return result


class NoModel:
    def complete(self, messages):
        return Completion(success=False, error="no key")


async def diagnose_scoped(cluster: Namespace, kind="deployment", name="checkout"):
    service = InvestigationService(
        context="test-cluster", namespace="payments", resource_kind=kind, resource_name=name
    )
    service.provider = LocalKubectlProvider(context="test-cluster", executor=cluster)
    investigation = await service.run()
    analyzer = RootCauseAnalyzer()
    analyzer.llm_client = NoModel()
    return investigation, analyzer.analyze(investigation)


async def test_the_unscoped_run_blames_the_other_pod():
    # Vacuity: without the scope the leader really is about notifier, so the
    # scoped assertions below are about ranking, not about a fixture in which
    # checkout would have won anyway.
    service = InvestigationService(context="test-cluster", namespace="payments")
    service.provider = LocalKubectlProvider(context="test-cluster", executor=Namespace())
    investigation = await service.run()
    analyzer = RootCauseAnalyzer()
    analyzer.llm_client = NoModel()
    diagnosis = analyzer.analyze(investigation)
    assert "notifier" in diagnosis["root_cause"]


async def test_a_deployment_scope_selects_a_cause_about_that_deployment():
    _, diagnosis = await diagnose_scoped(Namespace())

    assert "checkout" in diagnosis["root_cause"], diagnosis["root_cause"]
    assert "notifier" not in diagnosis["root_cause"]
    top = diagnosis["hypotheses"][0]
    assert top["target"]["name"].startswith("checkout-")
    # The other workload's fault is still reported, as a candidate.
    assert any("notifier" in h["target"]["name"] for h in diagnosis["hypotheses"][1:])


async def test_a_scope_nothing_matched_says_so_instead_of_blaming_a_neighbour():
    _, diagnosis = await diagnose_scoped(Namespace(with_checkout=False))

    assert diagnosis["root_cause"].startswith(
        "No finding in the collected evidence is about deployment/checkout."
    ), diagnosis["root_cause"]


async def test_the_scoped_selection_explains_itself_truthfully():
    _, diagnosis = await diagnose_scoped(Namespace())
    selected = next(
        h for h in diagnosis["hypotheses"] if h["id"] == diagnosis["selected_hypothesis"]
    )
    others = [h for h in diagnosis["hypotheses"] if h["confidence"] > selected["confidence"]]
    # Vacuity: a more confident cause about another workload must exist, or
    # there is no ordering to explain.
    assert others, [(h["id"], h["confidence"]) for h in diagnosis["hypotheses"]]
    assert "deployment/checkout" in diagnosis["selection_rationale"], diagnosis[
        "selection_rationale"
    ]


async def test_evidence_the_round_collected_is_not_reported_missing():
    """Lessons Learned listed "Container exit code and termination reason" as
    evidence that would have shortened the investigation — on a page showing
    the exit code, read from the pod spec that round collected."""
    investigation, diagnosis = await diagnose_scoped(Namespace())
    selected = diagnosis["selected_hypothesis"]
    target = next(h for h in diagnosis["hypotheses"] if h["id"] == selected)["target"]
    held = {
        entry["id"] for entry in investigation["evidence"] if entry["status"] in {"ok", "empty"}
    }
    # Vacuity: the pod spec for the selected target really was collected.
    assert f"k8s.pod.spec:pod/{target['namespace']}/{target['name']}" in held

    gaps = diagnosis["evidence_gaps"]
    assert "Container exit code and termination reason" not in gaps, gaps
    steps = [step["description"] for step in diagnosis["remediation"]["remediation"]]
    assert not any("exit code" in step for step in steps), steps


def test_evidence_not_collected_for_the_target_is_still_reported():
    # The control: another pod's spec answers nothing about this one.
    from app.analysis.evidence_gaps import outstanding
    from app.analysis.models import Hypothesis, Severity
    from app.evidence.models import ResourceRef

    hypothesis = Hypothesis(
        id="workload.application_startup_failure",
        title="t",
        category="workload",
        severity=Severity.CRITICAL,
        confidence=80,
        rationale="",
        target=ResourceRef(kind="Pod", name="checkout-0", namespace="payments"),
        supporting_signal_ids=("s",),
        missing_evidence=("Container exit code and termination reason", "Which revision"),
    )
    elsewhere = {"evidence": [{"id": "k8s.pod.spec:pod/payments/other-0", "status": "ok"}]}
    assert outstanding(hypothesis, elsewhere) == hypothesis.missing_evidence


def test_every_mapped_item_is_one_a_rule_declares():
    # A key that matches no rule's wording maps nothing, and the item it was
    # meant for goes on being reported missing after it was collected.
    from app.analysis.evidence_gaps import ANSWERED_BY
    from app.analysis.hypothesis_rules import DEFAULT_HYPOTHESIS_RULES

    declared = {item for rule in DEFAULT_HYPOTHESIS_RULES for item in rule.missing_evidence}
    assert set(ANSWERED_BY) <= declared, set(ANSWERED_BY) - declared
