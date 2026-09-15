"""Guidance must describe the diagnosis, not whatever the payload listed first.

`FixRecommendationEngine` matched the first substring in `str(investigation)`
and aimed every command and follow-up at the first unhealthy deployment. On
the QA cluster's `payments` namespace — nine concurrent faults — a diagnosis of
a pod missing its ConfigMap was shipped with image-tag prevention advice and
`kubectl edit deployment checkout`, and MCP returned that command to an agent.

Every earlier fixture had one fault, where "the first workload" and "the
diagnosed workload" are the same object, so none could tell the heuristic from
the rule. These use the captured multi-fault pod list and put an unrelated
deployment first, which is the shape that ships.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.ai.fix_recommendation_engine import PREVENTION_BY_CATEGORY, FixRecommendationEngine
from app.ai.providers import Completion
from app.ai.root_cause_analyzer import RootCauseAnalyzer
from app.analysis.hypothesis_rules import DEFAULT_HYPOTHESIS_RULES
from app.collectors.base import InvestigationScope
from app.kubernetes.pod_inspector import PodInspector
from app.mcp.tools import _diagnosis_view
from app.providers.base import ProviderResult
from app.reports.composer import IncidentReportComposer

FIXTURE = Path(__file__).parent / "fixtures" / "real_pods_kind_qa.json"


class NoModel:
    def complete(self, messages):
        return Completion(success=False, error="no key")


class Model:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def complete(self, messages):
        return Completion(success=True, content=json.dumps(self.payload))


@pytest.fixture(scope="module")
def investigation() -> dict[str, Any]:
    pods = json.loads(FIXTURE.read_text())
    analysed = PodInspector().analyse(
        [ProviderResult(success=True, data=pods, equivalent_command="kubectl get pods -o json")],
        InvestigationScope(context="kind-qa"),
    )
    return {
        "context": "kind-qa",
        "health": {"status": "issues_found"},
        "severity": {"severity": "Critical"},
        "evidence_coverage": {"completeness": 100, "degraded": []},
        "evidence": [
            {"id": "k8s.pods:cluster/_cluster/kind-qa", "kind": "k8s.pods"},
            {"id": "k8s.deployments:cluster/_cluster/kind-qa", "kind": "k8s.deployments"},
        ],
        "pods": analysed,
        # First in the list, and not what the fixture's leading hypothesis is
        # about. This is the object the heuristic used to target.
        "deployments": {
            "unhealthy_deployments": [
                {
                    "name": "checkout",
                    "namespace": "payments",
                    "desired_replicas": 2,
                    "available_replicas": 0,
                },
            ]
        },
    }


def workload_names(investigation: dict[str, Any]) -> set[str]:
    """Every workload a guess could have landed on, as the name a command uses."""
    names = {
        deployment["name"] for deployment in investigation["deployments"]["unhealthy_deployments"]
    }
    for pod in investigation["pods"]["problematic_pods"]:
        names.add(pod["name"])
        # A pod `checkout-77bd6cbdf4-2c5ln` is addressed as deployment `checkout`.
        names.add(re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$", "", pod["name"]))
    return names


def named_in(text: str, names: set[str]) -> set[str]:
    return {name for name in names if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)}


def about(diagnosis: dict[str, Any]) -> set[str]:
    """The names this diagnosis is entitled to mention."""
    hypothesis = next(
        h for h in diagnosis["hypotheses"] if h["id"] == diagnosis["selected_hypothesis"]
    )
    allowed = {hypothesis["target"]["name"], diagnosis["remediation"]["target"]["name"]}
    return allowed | {re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$", "", name) for name in allowed}


def guidance(diagnosis: dict[str, Any]) -> str:
    return "\n".join(
        [*diagnosis["kubectl_commands"], *diagnosis["next_steps"], diagnosis["prevention"]]
    )


class TestTheDeterministicPath:
    @pytest.fixture(scope="class")
    def diagnosis(self, investigation):
        analyzer = RootCauseAnalyzer()
        analyzer.llm_client = NoModel()
        return analyzer.analyze(investigation)

    def test_the_fixture_has_the_shape_that_hid_the_defect(self, investigation, diagnosis):
        # Vacuity: if the leader were about `checkout`, the heuristic and the
        # rule would agree and nothing below could fail.
        # Which of the fixture's tied faults leads is not the point, and moved
        # when support was scoped to the workloads a hypothesis is about.
        assert diagnosis["ai_generated"] is False
        assert "checkout" not in about(diagnosis)
        assert len(investigation["pods"]["problematic_pods"]) > 3

    def test_no_command_or_step_names_a_workload_the_diagnosis_is_not_about(
        self, investigation, diagnosis
    ):
        strays = named_in(guidance(diagnosis), workload_names(investigation)) - about(diagnosis)
        assert not strays, f"guidance names {strays}: {guidance(diagnosis)}"

    def test_the_commands_are_the_plans(self, diagnosis):
        planned = [
            step["command"]
            for key in ("preconditions", "remediation", "verification")
            for step in diagnosis["remediation"][key]
            if step.get("command")
        ]
        assert [command.split("   #")[0] for command in diagnosis["kubectl_commands"]] == planned

    def test_prevention_is_for_the_category_diagnosed(self, diagnosis):
        selected = next(
            h for h in diagnosis["hypotheses"] if h["id"] == diagnosis["selected_hypothesis"]
        )
        assert diagnosis["prevention"] == PREVENTION_BY_CATEGORY[selected["category"]]

    def test_the_report_and_mcp_carry_the_same_guidance(self, investigation, diagnosis):
        # The two consumers that surfaced the defect: the Preventive Actions
        # section of every report, and the commands MCP hands an agent.
        report = IncidentReportComposer().compose(
            diagnosis, investigation, "INC-1", "2026-09-14T00:00:00Z", "payments", "success"
        )
        preventive = next(s for s in report.sections if s.title == "Preventive Actions")
        text = "\n".join(preventive.as_lines())
        assert not named_in(text, workload_names(investigation)) - about(diagnosis)

        view = _diagnosis_view(
            "id", "succeeded", "", {"investigation": investigation, "diagnosis": diagnosis}
        )
        assert not named_in("\n".join(view["commands"]), workload_names(investigation)) - about(
            diagnosis
        )


class TestTheGroundedPath:
    def test_guidance_follows_the_hypothesis_the_model_selected(self, investigation):
        # A defensible disagreement with the deterministic ranking: the model
        # picks the image fault. The root cause is then about `ledger`, and the
        # plan, commands and prevention must be too — they were built for the
        # deterministic leader regardless.
        analyzer = RootCauseAnalyzer()
        analyzer.llm_client = NoModel()
        baseline = analyzer.analyze(investigation)
        leader = next(
            h for h in baseline["hypotheses"] if h["id"] == baseline["selected_hypothesis"]
        )
        # The model picks a fault that is *not* the deterministic leader, or
        # guidance built for the leader would pass. Which one leads the tie
        # moved once already; choosing here keeps the test from going vacuous.
        choice = (
            "image.pull_failure"
            if leader["id"] != "image.pull_failure"
            else ("workload.missing_configuration")
        )
        chosen = next(h for h in baseline["hypotheses"] if h["id"] == choice)
        assert chosen["category"] != leader["category"]
        cited = next(s for s in chosen["supporting_signals"] if s.startswith("pod."))
        pod = chosen["target"]["name"]

        analyzer.llm_client = Model(
            {
                "selected_hypothesis": choice,
                "root_cause": f"Pod payments/{pod} is failing: {chosen['title']}.",
                "explanation": chosen["rationale"],
                "cited_signals": [cited],
                "fix": "Correct it.",
                "confidence": 80,
            }
        )
        diagnosis = analyzer.analyze(investigation)

        assert diagnosis["ai_generated"] is True, diagnosis["grounding"]
        assert diagnosis["selected_hypothesis"] == choice
        assert diagnosis["remediation"]["hypothesis_id"] == choice
        assert diagnosis["prevention"] == PREVENTION_BY_CATEGORY[chosen["category"]]
        assert diagnosis["prevention"] != PREVENTION_BY_CATEGORY[leader["category"]]
        strays = named_in(guidance(diagnosis), workload_names(investigation)) - about(diagnosis)
        assert not strays, f"guidance names {strays}: {guidance(diagnosis)}"


def test_every_category_a_rule_declares_has_prevention():
    categories = {rule.category for rule in DEFAULT_HYPOTHESIS_RULES}
    assert categories <= set(PREVENTION_BY_CATEGORY), categories - set(PREVENTION_BY_CATEGORY)


def test_with_nothing_diagnosed_nothing_is_targeted(investigation):
    recommendation = FixRecommendationEngine().recommend(investigation)
    assert not named_in(
        "\n".join([*recommendation["kubectl_commands"], *recommendation["next_steps"]]),
        workload_names(investigation),
    )
