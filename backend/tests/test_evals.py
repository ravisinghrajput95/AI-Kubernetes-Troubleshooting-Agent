"""The evaluation corpus, enforced in CI.

Signal rules, hypothesis rules, the prompt and the grounding checks can all be
changed without breaking a single unit test while quietly making the platform
worse at its job. This is the regression gate for that.

The corpus itself lives in `evals/cases/` as JSON so a contributor can add a
failure mode without writing Python.
"""

import pytest

from evals.runner import EvalRunner, load_grounding_cases, load_investigation_cases

INVESTIGATIONS = load_investigation_cases()
GROUNDING = load_grounding_cases()
REPORT = EvalRunner().run(INVESTIGATIONS, GROUNDING)


def result_for(case_id: str):
    return next(r for r in (*REPORT.investigations, *REPORT.grounding) if r.case_id == case_id)


class TestCorpusIntegrity:
    def test_the_corpus_is_not_empty(self):
        assert len(INVESTIGATIONS) >= 8
        assert len(GROUNDING) >= 8

    def test_grounding_corpus_covers_both_verdicts(self):
        """A corpus of only-rejections would pass while the model path is dead."""
        accepted = [case for case in GROUNDING if case.expect_valid]
        rejected = [case for case in GROUNDING if not case.expect_valid]

        assert len(accepted) >= 3, "need cases that must be accepted"
        assert len(rejected) >= 3, "need cases that must be rejected"

    def test_every_grounding_case_references_a_real_investigation(self):
        known = {case.id for case in INVESTIGATIONS}
        for case in GROUNDING:
            assert case.investigation_case in known, case.id

    def test_case_ids_are_unique(self):
        ids = [case.id for case in (*INVESTIGATIONS, *GROUNDING)]
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", INVESTIGATIONS, ids=lambda c: c.id)
def test_investigation_reaches_the_expected_conclusion(case):
    result = result_for(case.id)
    assert result.passed, "\n".join([case.description, *result.failures])


@pytest.mark.parametrize("case", GROUNDING, ids=lambda c: c.id)
def test_grounding_reaches_the_expected_verdict(case):
    result = result_for(case.id)
    assert result.passed, "\n".join([case.description, *result.failures])


class TestThresholds:
    def test_reasoning_accuracy_holds(self):
        assert REPORT.hypothesis_accuracy == 1.0, REPORT.summary()

    def test_grounding_accuracy_holds(self):
        assert REPORT.grounding_accuracy == 1.0, REPORT.summary()

    def test_no_sound_diagnosis_is_falsely_rejected(self):
        """The failure mode that does not announce itself.

        An over-strict grounding check routes every investigation to the
        deterministic fallback. Nothing errors; the platform just stops using
        the model. This is the metric that catches it.
        """
        false_rejections = REPORT.false_rejections(GROUNDING)
        assert false_rejections == [], (
            f"grounding rejected sound diagnoses: {false_rejections}. "
            f"This silently disables the model path."
        )
