"""The model must not be able to assert things that were never observed."""

from app.analysis.grounding import GroundingValidator
from app.analysis.models import AnalysisResult, Hypothesis, Severity, Signal
from app.evidence.models import ResourceRef

VALIDATOR = GroundingValidator()

POD = ResourceRef(kind="Pod", name="web-0", namespace="prod")
SIGNAL = Signal.create(
    "pod.crash_loop",
    Severity.CRITICAL,
    "Pod prod/web-0 is in CrashLoopBackOff.",
    POD,
    ("k8s.pods:cluster/_cluster/test",),
)
HYPOTHESIS = Hypothesis(
    id="workload.application_startup_failure",
    title="Application fails on startup",
    category="workload",
    severity=Severity.CRITICAL,
    confidence=60,
    rationale="...",
    target=POD,
    supporting_signal_ids=(SIGNAL.id,),
)
ANALYSIS = AnalysisResult(signals=(SIGNAL,), hypotheses=(HYPOTHESIS,))
EMPTY = AnalysisResult()


def test_grounded_response_is_accepted():
    result = VALIDATOR.validate(
        {
            "root_cause": "Application fails on startup",
            "selected_hypothesis": HYPOTHESIS.id,
            "cited_signals": [SIGNAL.id],
        },
        ANALYSIS,
    )

    assert result.valid is True
    assert result.cited_signals == (SIGNAL.id,)
    assert result.rejected_citations == ()


def test_fabricated_hypothesis_id_is_rejected():
    result = VALIDATOR.validate(
        {
            "root_cause": "Something else entirely",
            "selected_hypothesis": "workload.aliens",
            "cited_signals": [SIGNAL.id],
        },
        ANALYSIS,
    )

    assert result.valid is False
    assert "unknown hypothesis" in result.reason


def test_fabricated_signal_citations_are_stripped():
    result = VALIDATOR.validate(
        {
            "root_cause": "Application fails on startup",
            "selected_hypothesis": HYPOTHESIS.id,
            "cited_signals": [SIGNAL.id, "pod.oom_killed:pod/prod/ghost"],
        },
        ANALYSIS,
    )

    assert result.valid is True
    assert result.cited_signals == (SIGNAL.id,)
    assert result.rejected_citations == ("pod.oom_killed:pod/prod/ghost",)


def test_response_citing_only_invented_signals_is_rejected():
    result = VALIDATOR.validate(
        {
            "root_cause": "Node ran out of disk",
            "selected_hypothesis": None,
            "cited_signals": ["node.disk_full:node/_cluster/node-9"],
        },
        ANALYSIS,
    )

    assert result.valid is False
    assert "cited no valid signal" in result.reason


def test_response_with_no_citations_is_rejected_when_signals_exist():
    result = VALIDATOR.validate(
        {"root_cause": "Probably networking", "cited_signals": []},
        ANALYSIS,
    )
    assert result.valid is False


def test_citations_are_not_required_when_there_are_no_signals():
    result = VALIDATOR.validate(
        {"root_cause": "Cluster appears healthy", "cited_signals": []},
        EMPTY,
    )
    assert result.valid is True


def test_empty_root_cause_is_rejected():
    result = VALIDATOR.validate({"root_cause": "   ", "cited_signals": [SIGNAL.id]}, ANALYSIS)
    assert result.valid is False
    assert "no root cause" in result.reason


def test_null_hypothesis_selection_is_permitted():
    result = VALIDATOR.validate(
        {
            "root_cause": "Signals conflict; no single cause fits",
            "selected_hypothesis": "null",
            "cited_signals": [SIGNAL.id],
        },
        ANALYSIS,
    )

    assert result.valid is True
    assert result.selected_hypothesis is None


def test_string_citation_is_tolerated():
    result = VALIDATOR.validate(
        {"root_cause": "Crash loop", "cited_signals": SIGNAL.id},
        ANALYSIS,
    )
    assert result.valid is True
    assert result.cited_signals == (SIGNAL.id,)
