"""Diagnosis pipeline: grounded model output is used, ungrounded output is not."""

import json

import pytest

from app.ai.root_cause_analyzer import RootCauseAnalyzer

INVESTIGATION = {
    "context": "test-cluster",
    "health": {"status": "issues_found", "message": "Found 2 signals"},
    "severity": {"severity": "Critical"},
    "evidence_coverage": {"completeness": 100, "degraded": []},
    "evidence": [
        {"id": "k8s.pods:cluster/_cluster/test", "kind": "k8s.pods"},
        {"id": "k8s.pods.logs:cluster/_cluster/test", "kind": "k8s.pods.logs"},
    ],
    "pods": {
        "problematic_pods": [{"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}]
    },
    "logs": {
        "logs": [
            {
                "name": "web-0",
                "namespace": "prod",
                "relevant_lines": ["error: missing environment variable DB_HOST"],
            }
        ]
    },
    "metrics": {"available": True},
    "deployments": {"unhealthy_deployments": []},
    "network": {"findings": []},
    "nodes": {"findings": []},
    "storage": {"findings": []},
    "events": {"findings": []},
}

HEALTHY = {
    "context": "test-cluster",
    "health": {"status": "healthy"},
    "severity": {"severity": "Healthy"},
    "evidence_coverage": {"completeness": 100, "degraded": []},
    "evidence": [],
    "pods": {"problematic_pods": []},
    "logs": {"logs": []},
    "metrics": {"available": True},
}

REQUIRED_KEYS = {
    "root_cause",
    "explanation",
    "fix",
    "kubectl_commands",
    "prevention",
    "evidence_gaps",
    "next_steps",
    "confidence",
    "confidence_reasoning",
    "remediation_risk",
    "remediation_plan",
    "ai_generated",
}


class StubLLM:
    def __init__(self, payload=None, error=""):
        self.payload = payload
        self.error = error
        self.messages = None

    def complete(self, messages):
        self.messages = messages
        if self.payload is None:
            return {"success": False, "error": self.error, "content": ""}
        return {"success": True, "error": "", "content": json.dumps(self.payload)}


@pytest.fixture
def analyzer():
    return RootCauseAnalyzer()


def crash_loop_signal_id():
    return "pod.crash_loop:pod/prod/web-0"


def test_backward_compatible_keys_are_present_on_both_paths(analyzer):
    analyzer.llm_client = StubLLM(error="no key")
    deterministic = analyzer.analyze(INVESTIGATION)

    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": "workload.application_startup_failure",
            "root_cause": "Missing DB_HOST environment variable",
            "explanation": "The container exits during startup.",
            "cited_signals": [crash_loop_signal_id()],
            "fix": "Add DB_HOST to the deployment.",
            "kubectl_commands": ["kubectl get deployment web -n prod"],
            "prevention": "Validate env vars in CI.",
            "confidence": 80,
            "confidence_reasoning": ["Logs name the missing variable"],
            "evidence_gaps": [],
            "next_steps": ["Patch the deployment"],
        }
    )
    grounded = analyzer.analyze(INVESTIGATION)

    assert set(deterministic) >= REQUIRED_KEYS
    assert set(grounded) >= REQUIRED_KEYS
    assert deterministic["ai_generated"] is False
    assert grounded["ai_generated"] is True


def test_grounded_model_output_is_used(analyzer):
    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": "workload.application_startup_failure",
            "root_cause": "Missing DB_HOST environment variable",
            "explanation": "The container exits during startup.",
            "cited_signals": [crash_loop_signal_id()],
            "fix": "Add DB_HOST to the deployment.",
            "kubectl_commands": ["kubectl get deployment web -n prod"],
            "confidence": 80,
        }
    )
    diagnosis = analyzer.analyze(INVESTIGATION)

    assert diagnosis["root_cause"] == "Missing DB_HOST environment variable"
    assert diagnosis["selected_hypothesis"] == "workload.application_startup_failure"
    assert diagnosis["cited_signals"] == [crash_loop_signal_id()]
    assert diagnosis["cited_evidence"] == ["k8s.pods:cluster/_cluster/test"]
    assert diagnosis["grounding"]["valid"] is True


def test_hallucinated_diagnosis_is_discarded(analyzer):
    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": "workload.application_startup_failure",
            "root_cause": "Node node-9 ran out of disk space",
            "explanation": "Invented from nothing.",
            "cited_signals": ["node.disk_full:node/_cluster/node-9"],
            "confidence": 99,
        }
    )
    diagnosis = analyzer.analyze(INVESTIGATION)

    assert diagnosis["ai_generated"] is False
    assert "node-9" not in diagnosis["root_cause"]
    assert diagnosis["confidence"] < 99


def test_invented_hypothesis_is_discarded(analyzer):
    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": "cosmic.ray_bitflip",
            "root_cause": "A cosmic ray flipped a bit",
            "cited_signals": [crash_loop_signal_id()],
            "confidence": 95,
        }
    )
    assert analyzer.analyze(INVESTIGATION)["ai_generated"] is False


def test_malformed_model_json_falls_back(analyzer):
    class BadJSON(StubLLM):
        def complete(self, messages):
            return {"success": True, "error": "", "content": "not json at all"}

    analyzer.llm_client = BadJSON()
    assert analyzer.analyze(INVESTIGATION)["ai_generated"] is False


def test_deterministic_path_selects_the_ranked_hypothesis(analyzer):
    analyzer.llm_client = StubLLM(error="OPENAI_API_KEY is not configured")
    diagnosis = analyzer.analyze(INVESTIGATION)

    assert diagnosis["selected_hypothesis"] == "workload.application_startup_failure"
    assert crash_loop_signal_id() in diagnosis["cited_signals"]
    assert diagnosis["signals"]
    assert diagnosis["hypotheses"]


def test_confidence_combines_three_components_on_the_ai_path(analyzer):
    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": "workload.application_startup_failure",
            "root_cause": "Missing DB_HOST",
            "cited_signals": [crash_loop_signal_id()],
            "confidence": 80,
        }
    )
    diagnosis = analyzer.analyze(INVESTIGATION)

    names = [item["component"] for item in diagnosis["confidence_breakdown"]]
    assert names == ["Evidence Strength", "AI Reasoning", "Evidence Completeness"]
    assert sum(item["weight"] for item in diagnosis["confidence_breakdown"]) == 100
    assert diagnosis["confidence"] == sum(
        item["contribution"] for item in diagnosis["confidence_breakdown"]
    )


def test_incomplete_evidence_lowers_confidence(analyzer):
    analyzer.llm_client = StubLLM(error="no key")
    complete = analyzer.analyze(INVESTIGATION)["confidence"]

    degraded = {
        **INVESTIGATION,
        "evidence_coverage": {
            "completeness": 40,
            "degraded": [{"kind": "k8s.nodes", "status": "forbidden", "detail": "RBAC denied"}],
        },
    }
    assert analyzer.analyze(degraded)["confidence"] < complete


def test_evidence_gaps_include_what_would_confirm_the_hypothesis(analyzer):
    analyzer.llm_client = StubLLM(error="no key")
    gaps = analyzer.analyze(INVESTIGATION)["evidence_gaps"]

    assert any("previous" in gap.lower() for gap in gaps)
    assert any("probe" in gap.lower() for gap in gaps)


def test_degraded_evidence_is_reported_as_a_gap(analyzer):
    analyzer.llm_client = StubLLM(error="no key")
    degraded = {
        **INVESTIGATION,
        "evidence_coverage": {
            "completeness": 60,
            "degraded": [{"kind": "k8s.nodes", "status": "forbidden", "detail": "RBAC denied"}],
        },
    }
    assert "RBAC denied" in analyzer.analyze(degraded)["evidence_gaps"]


def test_healthy_cluster_needs_no_citations(analyzer):
    analyzer.llm_client = StubLLM(
        {
            "selected_hypothesis": None,
            "root_cause": "No critical issues detected.",
            "cited_signals": [],
            "confidence": 70,
        }
    )
    diagnosis = analyzer.analyze(HEALTHY)

    assert diagnosis["ai_generated"] is True
    assert diagnosis["signals"] == []


def test_prompt_contains_signals_and_never_raw_evidence(analyzer):
    stub = StubLLM(error="no key")
    analyzer.llm_client = stub
    analyzer.analyze(INVESTIGATION)

    user_prompt = stub.messages[1]["content"]
    assert crash_loop_signal_id() in user_prompt
    assert "workload.application_startup_failure" in user_prompt
    # The raw executed-command and inventory dump must not be resent wholesale.
    assert "executed_commands" not in user_prompt
    assert "pod_inventory" not in user_prompt


def test_system_prompt_states_the_citation_contract(analyzer):
    stub = StubLLM(error="no key")
    analyzer.llm_client = stub
    analyzer.analyze(INVESTIGATION)

    system_prompt = stub.messages[0]["content"]
    assert "cited_signals" in system_prompt
    assert "Fabricated ids are rejected" in system_prompt
