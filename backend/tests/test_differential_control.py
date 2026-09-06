"""The churn-vs-divergence discriminator, tested where it can actually run.

`tests/differential.py` decides whether the agent and the kubeconfig disagree
or whether the cluster moved underneath them. It is used only by
`tests/test_agent_transport.py`, which needs `K8S_AGENT_CLUSTER_INTEGRATION=1`
and skips everywhere else — so a defect in the discriminator would be invisible
in every run of the default suite and in most runs of CI. That is the standing
`scripts/mutation_check.py` was written to fix.

Two directions matter equally and both are asserted here. Excluding too little
puts the CI flake back. Excluding too much is worse and quieter: a comparison
that discards every difference passes forever while proving nothing, which is
the same shape as an over-strict grounding check routing everything to the
fallback.
"""

from typing import ClassVar

import pytest

from tests.differential import Comparison, compare, leaves, unstable


class TestFlattening:
    def test_a_nested_payload_becomes_addressable_leaves(self):
        assert leaves({"healthy": False, "counts": {"pods": 3}}) == {
            "healthy": False,
            "counts.pods": 3,
        }

    def test_list_items_are_addressed_by_identity_not_position(self):
        """A pod appearing must not shift the path of every pod after it.

        Positional addressing is what made the original comparison report a
        whole list as different when one item was inserted at the front.
        """
        before = leaves({"pods": [{"namespace": "a", "name": "web", "phase": "Running"}]})
        after = leaves(
            {
                "pods": [
                    {"namespace": "a", "name": "api", "phase": "Pending"},
                    {"namespace": "a", "name": "web", "phase": "Running"},
                ]
            }
        )
        assert "pods[namespace=a/name=web].phase" in before
        assert (
            before["pods[namespace=a/name=web].phase"] == after["pods[namespace=a/name=web].phase"]
        )

    def test_items_sharing_an_identity_do_not_collide(self):
        """Two findings about one Service are two values, not one overwriting the other."""
        flat = leaves(
            {
                "findings": [
                    {"namespace": "a", "service": "s", "issue": "no endpoints"},
                    {"namespace": "a", "service": "s", "issue": "no selector"},
                ]
            }
        )
        issues = sorted(value for key, value in flat.items() if key.endswith(".issue"))
        assert issues == ["no endpoints", "no selector"]

    def test_an_empty_container_is_a_leaf(self):
        """ "No findings" and "one finding" must differ at the same path.

        If an empty list contributed no leaf, a section that lost its findings
        entirely would be absent from both sides of the comparison and agree.
        """
        assert leaves({"findings": []}) == {"findings": []}
        assert compare(
            {"findings": []}, {"findings": [{"name": "x"}]}, {"findings": []}
        ).divergences

    def test_a_list_of_scalars_falls_back_to_position(self):
        assert leaves({"names": ["a", "b"]}) == {"names[0]": "a", "names[1]": "b"}


class TestChurnIsNotDivergence:
    """The defect that failed CI on da5de44, and its opposite."""

    SUBJECT: ClassVar[dict] = {
        "deployments": [{"namespace": "k", "name": "app", "unavailable_replicas": 0}]
    }
    MOVED: ClassVar[dict] = {
        "deployments": [{"namespace": "k", "name": "app", "unavailable_replicas": 1}]
    }

    def test_a_value_the_cluster_moved_is_not_a_divergence(self):
        # The control read through the *same* provider disagrees with the
        # subject, so the field was moving and cannot prove anything.
        result = compare(self.SUBJECT, self.MOVED, control=self.MOVED)
        assert not result.divergences
        assert result.churned == ["deployments[namespace=k/name=app].unavailable_replicas"]

    def test_a_value_stable_on_one_provider_and_different_on_the_other_is_a_divergence(self):
        # Same two payloads. What changes the verdict is the control: the
        # subject read the same value twice, so the difference is the provider.
        result = compare(self.SUBJECT, self.MOVED, control=self.SUBJECT)
        assert result.divergences and "unavailable_replicas" in result.divergences[0]

    def test_churn_in_one_field_does_not_excuse_a_divergence_in_another(self):
        """The exclusion is per value, not per section.

        A single moving field must not blanket a whole list, or a real
        divergence beside it goes unreported — which is how excluding too much
        would silently disarm the suite.
        """
        subject = {
            "pods": [
                {"namespace": "a", "name": "churning", "phase": "Pending"},
                {"namespace": "a", "name": "steady", "phase": "Running"},
            ]
        }
        other = {
            "pods": [
                {"namespace": "a", "name": "churning", "phase": "Running"},
                {"namespace": "a", "name": "steady", "phase": "Failed"},
            ]
        }
        control = {
            "pods": [
                {"namespace": "a", "name": "churning", "phase": "Running"},
                {"namespace": "a", "name": "steady", "phase": "Running"},
            ]
        }
        result = compare(subject, other, control)
        assert result.churned == ["pods[namespace=a/name=churning].phase"]
        assert len(result.divergences) == 1
        assert "steady" in result.divergences[0]

    def test_an_object_only_one_provider_saw_is_a_divergence(self):
        """A pod missing from one path is the failure this suite exists for."""
        subject = {"pods": [{"namespace": "a", "name": "web"}]}
        result = compare(subject, {"pods": []}, control=subject)
        assert result.divergences

    def test_an_object_that_appeared_mid_comparison_is_churn(self):
        subject = {"pods": [{"namespace": "a", "name": "web"}]}
        grown = {"pods": [{"namespace": "a", "name": "web"}, {"namespace": "a", "name": "new"}]}
        assert not compare(subject, grown, control=grown).divergences

    def test_unstable_reports_presence_as_movement(self):
        assert unstable({"a": 1}, {}) == {"a"}


class TestItRefusesRatherThanProvingNothing:
    """The guard, and its own control.

    An exclusion mechanism with no floor passes hardest exactly when the
    cluster is least readable. Every check here is paired with a case the
    others would accept, for the same reason `tests/test_soak_guard.py` drives
    each of its three checks with a run the other two let through.
    """

    def test_a_comparison_where_everything_moved_is_refused(self):
        subject = {"a": 1, "b": 1}
        moved = {"a": 2, "b": 2}
        result = compare(subject, moved, control=moved)
        assert not result.divergences, "precondition: all of it reads as churn"
        assert result.refusal() is not None

    def test_a_comparison_where_most_of_it_moved_is_refused(self):
        subject = {"a": 1, "b": 1, "c": 1, "d": 1}
        other = {"a": 1, "b": 2, "c": 2, "d": 2}
        control = {"a": 1, "b": 2, "c": 2, "d": 2}
        result = compare(subject, other, control)
        assert len(result.stable) == 1 and len(result.churned) == 3
        assert result.refusal() is not None

    def test_a_mostly_stable_comparison_is_not_refused(self):
        """An over-strict guard means the suite never establishes anything.

        This is the case that must keep passing: one moving value on an
        otherwise quiet cluster is the normal state of the CI kind cluster, and
        refusing there would replace the flake with a different flake.
        """
        subject = {"a": 1, "b": 1, "c": 1, "d": 1}
        other = {"a": 1, "b": 1, "c": 1, "d": 2}
        control = {"a": 1, "b": 1, "c": 1, "d": 2}
        result = compare(subject, other, control)
        assert result.refusal() is None
        assert not result.divergences

    def test_silence_is_not_a_refusal(self):
        """Two providers that both found no problematic pods agree.

        A projection that is legitimately empty has nothing to churn, and
        refusing it would fail the suite on a healthy cluster.
        """
        assert compare({}, {}, {}).refusal() is None

    def test_a_refusal_names_what_it_refused(self):
        refusal = Comparison(churned=["a", "b"], stable=[]).refusal()
        assert refusal and "2" in refusal

    @pytest.mark.parametrize("noisy", [True, False])
    def test_divergences_are_reported_whether_or_not_the_run_is_refused(self, noisy):
        """A refusal must not swallow a defect it did happen to find."""
        subject = {"real": 1, "moving": 1}
        other = {"real": 2, "moving": 2 if noisy else 1}
        control = {"real": 1, "moving": 2 if noisy else 1}
        result = compare(subject, other, control)
        assert any("real" in line for line in result.divergences)
