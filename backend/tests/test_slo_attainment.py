"""The SLO evaluator, and through it the objectives docs/SLO.md publishes.

`scripts/slo_attainment.py` reads each objective's PromQL out of the document
rather than keeping a copy, so these tests are also the document's tests: every
objective with a formula must still have one the evaluator can use, and the
soundness objective must stay defined over the model's *answers*. It was
`fallback / all diagnoses`, gated on a label value the platform never emitted,
and read 100% rejections on every deployment without a model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "slo_attainment.py"
_spec = importlib.util.spec_from_file_location("slo_attainment", SCRIPT)
slo = importlib.util.module_from_spec(_spec)
sys.modules["slo_attainment"] = slo
_spec.loader.exec_module(slo)

PUBLISHED = slo.published_expressions(slo.SLO_DOC.read_text())


class FakePrometheus:
    """Answers each query from a table; anything unlisted matched nothing."""

    def __init__(self, answers: dict[str, float]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def scalar(self, query: str) -> float | None:
        self.asked.append(query)
        for fragment, value in self.answers.items():
            if fragment in query:
                return value
        return None


def test_every_objective_with_a_formula_is_published():
    # SLO 3 is served from an ingress by design and has no formula.
    assert set(PUBLISHED) == {1, 2, 4, 5, 6, 7}


def test_soundness_is_over_the_answers_the_model_gave():
    numerator, denominator = slo.ratio_parts(PUBLISHED[5])
    assert "grounding_rejections_total" in numerator
    assert 'k8sagent_llm_calls_total{outcome="succeeded"}' in denominator
    assert "diagnoses_total" not in PUBLISHED[5], (
        "soundness counted fallbacks again — which includes every diagnosis on a "
        "deployment without a model, and every one during a provider outage"
    )


def test_the_window_is_the_run_not_the_published_month():
    assert "[28d]" not in slo.windowed(PUBLISHED[1], 6300)
    assert "[6300s]" in slo.windowed(PUBLISHED[1], 6300)


def test_soundness_is_refused_when_the_model_answered_nothing():
    """The run that found the defect: no model, so no answers — which must be a
    refusal, not a 100% rejection rate."""
    prom = FakePrometheus(
        {
            'outcome="succeeded"': 0.0,
            "grounding_rejections_total": 0.0,
        }
    )
    result = slo.measure_soundness(PUBLISHED[5], prom, 600)
    assert result.verdict == "REFUSED"
    assert result.value is None


def test_soundness_is_measured_when_the_model_answered():
    """The control: with answers, a ratio comes back and is judged."""
    prom = FakePrometheus(
        {
            'increase(k8sagent_llm_calls_total{outcome="succeeded"}': 40.0,
            'rate(k8sagent_llm_calls_total{outcome="succeeded"}': 40.0 / 600,
            "grounding_rejections_total": 1.0 / 600,
        }
    )
    result = slo.measure_soundness(PUBLISHED[5], prom, 600)
    assert result.verdict == "MET"
    assert result.value == pytest.approx(1 / 40)


def test_a_ratio_with_an_empty_denominator_is_refused():
    result = slo.measure_ratio(1, PUBLISHED[1], FakePrometheus({}), 600)
    assert result.verdict == "REFUSED"


def test_latency_is_measured_as_the_quantile_not_the_comparison():
    """`histogram_quantile(...) < 30` returns nothing when false — reading as
    no data exactly when the objective is missed."""
    prom = FakePrometheus({"_count": 100.0, "histogram_quantile": 42.0})
    result = slo.measure_latency(PUBLISHED[2], prom, 600)
    assert result.verdict == "MISSED" and result.value == 42.0
    assert not any(query.rstrip().endswith("30") for query in prom.asked)


def test_submission_availability_counts_what_the_document_says():
    runs = [
        {"status": "succeeded", "finished_at": 10},
        {"status": "failed", "finished_at": 11},  # an investigation outcome, not submission
        {"status": "submit-503", "finished_at": 12},
        {"status": "submit-0", "finished_at": 13},  # a refused connection is unavailable
        {"status": "submit-429", "finished_at": 14},  # the rate limiter working: excluded
        {"status": "submit-409", "finished_at": 15},  # a correct refusal: excluded
        {"status": "submit-503", "finished_at": 99},  # outside the window
    ]
    result = slo.measure_submission(runs, start=0, end=50)
    assert result.sample == "4"
    assert result.value == pytest.approx(0.5)
