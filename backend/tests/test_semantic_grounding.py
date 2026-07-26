"""Semantic consistency of model output.

F9 from the 2026-07-26 review. Citation integrity guarantees every id resolves;
it does not guarantee the prose agrees with what those ids say. Verified gap: a
response citing a genuine CrashLoopBackOff signal while concluding "Resolved -
no action needed" was accepted.

The false-positive tests matter as much as the rejection tests. An over-strict
check does not fail loudly — it silently routes every investigation to the
deterministic fallback, turning the model off without telling anyone.
"""

import pytest

from app.analysis.grounding import GroundingValidator
from app.analysis.models import AnalysisResult, Hypothesis, Severity, Signal
from app.evidence.models import ResourceRef

VALIDATOR = GroundingValidator()

POD = ResourceRef(kind="Pod", name="web-0", namespace="prod")
NODE = ResourceRef(kind="Node", name="node-1")


def signal(signal_type, severity, summary, target=POD):
    return Signal.create(
        signal_type, severity, summary, target, ("k8s.pods:cluster/_cluster/test",)
    )


CRASH = signal("pod.crash_loop", Severity.CRITICAL, "Pod prod/web-0 is in CrashLoopBackOff.")
PROBE = signal("event.probe_failure", Severity.MEDIUM, "Readiness probe failed on prod/web-0.")
NODE_PRESSURE = signal("node.pressure", Severity.HIGH, "Node node-1 reports MemoryPressure.", NODE)


def hypothesis(hypothesis_id, supporting):
    return Hypothesis(
        id=hypothesis_id,
        title="Application fails on startup",
        category="workload",
        severity=Severity.CRITICAL,
        confidence=70,
        rationale="The container exits repeatedly.",
        target=POD,
        supporting_signal_ids=tuple(s.id for s in supporting),
    )


STARTUP = hypothesis("workload.application_startup_failure", [CRASH, PROBE])
NODE_HYP = hypothesis("node.unhealthy", [NODE_PRESSURE])

ANALYSIS = AnalysisResult(signals=(CRASH, PROBE, NODE_PRESSURE), hypotheses=(STARTUP, NODE_HYP))
HEALTHY = AnalysisResult()


def validate(**payload):
    return VALIDATOR.validate(payload, payload.pop("_analysis", ANALYSIS))


class TestContradiction:
    """The verified gap: correct citations, wrong conclusion."""

    def test_the_original_finding_is_rejected(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Resolved - no action needed",
                "explanation": "All clear.",
                "cited_signals": [CRASH.id],
                "selected_hypothesis": STARTUP.id,
            },
            ANALYSIS,
        )

        assert result.valid is False
        assert "severe signal" in result.reason
        assert "CrashLoopBackOff" in result.reason

    @pytest.mark.parametrize(
        "claim",
        [
            "The cluster appears healthy.",
            "No issues were found.",
            "Everything is operating normally.",
            "This has already resolved itself.",
            "No further action is required.",
            "The workload is working as expected.",
        ],
    )
    def test_reassurance_over_severe_evidence_is_rejected(self, claim):
        result = VALIDATOR.validate({"root_cause": claim, "cited_signals": [CRASH.id]}, ANALYSIS)
        assert result.valid is False

    def test_the_contradiction_may_appear_in_the_explanation(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Container restarts observed.",
                "explanation": "On inspection the cluster appears healthy.",
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )
        assert result.valid is False

    def test_the_same_wording_is_fine_on_a_healthy_cluster(self):
        """No severe signals means the reassurance is the correct conclusion."""
        result = VALIDATOR.validate(
            {"root_cause": "No issues found; the cluster appears healthy.", "cited_signals": []},
            HEALTHY,
        )
        assert result.valid is True

    def test_only_severe_signals_trigger_the_check(self):
        minor = AnalysisResult(signals=(PROBE,))
        result = VALIDATOR.validate(
            {"root_cause": "No action needed for this.", "cited_signals": [PROBE.id]},
            minor,
        )
        assert result.valid is True


class TestCitationRelevance:
    def test_citing_unrelated_signals_is_rejected(self):
        """Real ids that have nothing to do with the chosen hypothesis."""
        result = VALIDATOR.validate(
            {
                "root_cause": "The application fails on startup.",
                "selected_hypothesis": STARTUP.id,
                "cited_signals": [NODE_PRESSURE.id],
            },
            ANALYSIS,
        )

        assert result.valid is False
        assert "support the selected hypothesis" in result.reason

    def test_one_supporting_citation_is_enough(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "The application fails on startup.",
                "selected_hypothesis": STARTUP.id,
                "cited_signals": [CRASH.id, NODE_PRESSURE.id],
            },
            ANALYSIS,
        )
        assert result.valid is True

    def test_no_selection_means_no_relevance_requirement(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Several unrelated problems are present.",
                "selected_hypothesis": None,
                "cited_signals": [NODE_PRESSURE.id],
            },
            ANALYSIS,
        )
        assert result.valid is True


class TestInventedResources:
    def test_a_resource_no_evidence_mentions_is_rejected(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Pod payments/checkout-9 exhausted its disk.",
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )

        assert result.valid is False
        assert "payments/checkout-9" in result.reason

    def test_a_known_resource_is_accepted(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Pod prod/web-0 is restarting.",
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )
        assert result.valid is True

    @pytest.mark.parametrize(
        "prose",
        [
            "Raise the limit from 512Mi/1Gi to give headroom.",
            "The probe at /healthz is failing.",
            "See app/config.yaml for the setting.",
            "Throughput dropped to 20req/s during the incident.",
        ],
    )
    def test_non_resource_slashes_are_not_treated_as_references(self, prose):
        """Quantities, paths and rates must not be mistaken for resources."""
        result = VALIDATOR.validate({"root_cause": prose, "cited_signals": [CRASH.id]}, ANALYSIS)
        assert result.valid is True, f"false positive on: {prose}"

    def test_a_partially_known_reference_is_accepted(self):
        """Leniency is deliberate: a false rejection discards a sound diagnosis."""
        result = VALIDATOR.validate(
            {
                "root_cause": "Another pod in prod/web-1 shows the same pattern.",
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )
        assert result.valid is True

    def test_no_signals_means_no_reference_checking(self):
        result = VALIDATOR.validate(
            {"root_cause": "Nothing observed in payments/checkout-9.", "cited_signals": []},
            HEALTHY,
        )
        assert result.valid is True


class TestTransparency:
    def test_a_valid_result_names_the_checks_that_passed(self):
        result = VALIDATOR.validate(
            {
                "root_cause": "Pod prod/web-0 fails on startup.",
                "selected_hypothesis": STARTUP.id,
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )

        assert result.valid is True
        assert "no_contradiction" in result.checks
        assert "citations_support_selection" in result.checks
        assert "no_invented_resources" in result.checks
        assert result.to_dict()["checks_passed"]

    def test_a_rejection_states_which_check_failed(self):
        result = VALIDATOR.validate(
            {"root_cause": "No issues found.", "cited_signals": [CRASH.id]}, ANALYSIS
        )

        assert result.valid is False
        assert result.reason
        assert result.checks == ()


class TestGenuineDiagnosesStillPass:
    """Guards the fallback rate: over-strict checks turn the model off silently."""

    @pytest.mark.parametrize(
        "root_cause",
        [
            "Pod prod/web-0 is in CrashLoopBackOff after a failed startup.",
            "The container exits with code 137, indicating an OOM kill.",
            "A missing ConfigMap key prevents the container from starting.",
            "Readiness probes fail because the application starts slowly.",
            "Node node-1 is under memory pressure, evicting workloads.",
            "The deployment cannot reach its desired replica count.",
        ],
    )
    def test_realistic_diagnoses_are_accepted(self, root_cause):
        result = VALIDATOR.validate(
            {
                "root_cause": root_cause,
                "explanation": "Derived from the collected evidence.",
                "selected_hypothesis": STARTUP.id,
                "cited_signals": [CRASH.id],
            },
            ANALYSIS,
        )
        assert result.valid is True, f"false rejection: {root_cause} ({result.reason})"
