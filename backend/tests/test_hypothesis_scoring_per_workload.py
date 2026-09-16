"""A hypothesis names one resource, so it is scored on that resource's evidence.

Captured from a kind cluster minutes after Docker Desktop restarted it, through
its kubeconfig and then through an enrolled agent. The kubeconfig run reported
**"Application fails on startup and restarts repeatedly"** about the in-cluster
agent pod — Ready for nine minutes, one exit code to its name — at **92%**,
because a rule pooled every pod its triggers fired on: checkout's FATAL log
line, checkout's unavailable replicas and metrics-server's failing probe all
counted as support for it. The tie rationale quoted the pool, "rests on more
signals (21 against 9)". The runner-up claimed metrics-server's Service had
"no healthy backend" on the strength of a pod that was running and Ready,
supported by crash loops in every namespace.

The agent run, seconds later, named a different root cause: metrics-server's
restart aged out of `STABLE_AFTER` between the two reads, and that one pod's
churn moved a pooled confidence enough to reorder the leaders. Scored per
workload, churn on an unrelated pod cannot move the one named, and both
providers agree.
"""

import json
from pathlib import Path

import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.hypothesis_rules import DEFAULT_HYPOTHESIS_RULES, SignalPatternRule
from app.analysis.models import ResourceRef, Severity, Signal, SignalType

FIXTURE = (
    Path(__file__).parent / "fixtures" / "real_investigations_after_restart_two_providers.json"
)


@pytest.fixture(scope="module")
def captured() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def kubeconfig(captured):
    return AnalysisEngine().analyze(captured["kubeconfig"])


@pytest.fixture(scope="module")
def agent(captured):
    return AnalysisEngine().analyze(captured["agent"])


def by_id(result, hypothesis_id):
    return next(item for item in result.hypotheses if item.id == hypothesis_id)


class TestTheNamedWorkloadCarriesItsOwnEvidence:
    def test_startup_failure_names_the_pod_that_is_failing_on_startup(self, kubeconfig):
        # checkout crash-loops with `FATAL: config key DB_HOST is not set`; the
        # agent pod restarted once, during the node restart, and is Ready.
        hypothesis = by_id(kubeconfig, "workload.application_startup_failure")
        assert hypothesis.target.namespace == "payments"
        assert hypothesis.target.name.startswith("checkout-")

    def test_its_support_is_about_that_workload_alone(self, kubeconfig):
        hypothesis = by_id(kubeconfig, "workload.application_startup_failure")
        foreign = [
            signal_id
            for signal_id in hypothesis.supporting_signal_ids
            if "/payments/checkout" not in signal_id
        ]
        assert not foreign, foreign

    def test_confidence_is_the_named_workloads_not_the_pools(self, kubeconfig):
        # base 55 + three supporting types (exit code, unavailable replicas, a
        # log error). The pool added metrics-server's probe failure and hit
        # the 92 ceiling.
        assert by_id(kubeconfig, "workload.application_startup_failure").confidence == 85

    def test_no_namespaced_hypothesis_is_supported_from_another_namespace(self, kubeconfig, agent):
        for result in (kubeconfig, agent):
            signals = {signal.id: signal for signal in result.signals}
            for hypothesis in result.hypotheses:
                if not hypothesis.target.namespace:
                    continue
                elsewhere = [
                    signal_id
                    for signal_id in hypothesis.supporting_signal_ids
                    if signals[signal_id].target.namespace
                    not in (None, hypothesis.target.namespace)
                ]
                assert not elsewhere, (hypothesis.id, elsewhere)


class TestARunningPodIsNotAFailingBackend:
    def test_a_service_whose_pod_is_ready_has_a_healthy_backend(self, kubeconfig):
        # metrics-server's pod is listed for its restart history
        # (`reported_now: false`): running, Ready, and too recently restarted
        # to rule a crash loop out. That is not "all pods it selects are
        # failing".
        assert not [
            signal
            for signal in kubeconfig.signals
            if signal.type == SignalType.SERVICE_BACKENDS_FAILING
            and signal.target.name == "metrics-server"
        ]

    def test_the_fixture_still_lists_that_pod(self, captured):
        # Vacuity: without the restart-history entry there is nothing for the
        # graph rule to misread.
        entries = captured["kubeconfig"]["pods"]["problematic_pods"]
        assert any(
            entry["name"].startswith("metrics-server-") and entry.get("reported_now") is False
            for entry in entries
        )


class TestBothProvidersReachTheSameRanking:
    def test_the_leading_causes_agree(self, kubeconfig, agent):
        def leaders(result):
            return [(item.id, item.target.key, item.confidence) for item in result.hypotheses[:5]]

        assert leaders(kubeconfig) == leaders(agent)

    def test_the_captures_do_differ(self, captured):
        # Vacuity: the two reads must actually disagree somewhere, or agreement
        # proves nothing about churn.
        def restarting(payload):
            return {
                entry["name"]
                for entry in payload["pods"]["problematic_pods"]
                if entry.get("reported_now") is False
            }

        assert restarting(captured["kubeconfig"]) != restarting(captured["agent"])


def crash(name: str, namespace: str = "prod") -> Signal:
    return Signal.create(
        SignalType.POD_CRASH_LOOP,
        Severity.CRITICAL,
        "",
        ResourceRef(kind="Pod", name=name, namespace=namespace),
        ("k8s.pods:t",),
    )


def rule() -> SignalPatternRule:
    return SignalPatternRule(
        id="test.pods",
        title="t",
        category="c",
        rationale="r",
        triggers=frozenset({SignalType.POD_CRASH_LOOP}),
        base_confidence=80,
    )


class TestReplicasCorroborateUnrelatedWorkloadsDoNot:
    def test_two_replicas_of_one_deployment_corroborate_each_other(self):
        built = rule().evaluate([crash("api-7d9f8b6c4-abcde"), crash("api-7d9f8b6c4-fghij")])
        assert built.confidence == 80
        assert len(built.supporting_signal_ids) == 2

    def test_two_unrelated_workloads_are_two_single_observations(self):
        built = rule().evaluate([crash("api-7d9f8b6c4-abcde"), crash("worker-5c6d7e8f9-klmno")])
        assert built.confidence < 80
        assert len(built.supporting_signal_ids) == 1


class TestAServiceRestsOnWhatItSelects:
    """Same namespace, and still not a backend.

    The captured cluster's metrics-server case also crossed namespaces, which
    the namespace rule alone catches — so it could not tell whether support was
    scoped to the pods the Service selects. This can.
    """

    def evaluate(self, *extra: Signal):
        backends = Signal.create(
            SignalType.SERVICE_BACKENDS_FAILING,
            Severity.CRITICAL,
            "",
            ResourceRef(kind="Service", name="checkout-svc", namespace="payments"),
            ("k8s.network:t",),
            attributes={"pods": ["checkout-5b5fd56dbf-4cnmv"]},
        )
        rule = next(
            item for item in DEFAULT_HYPOTHESIS_RULES if item.id == "network.backends_all_failing"
        )
        return rule.evaluate([backends, *extra])

    def test_a_pod_it_selects_supports_it(self):
        built = self.evaluate(crash("checkout-5b5fd56dbf-4cnmv", "payments"))
        assert (
            "pod.crash_loop:pod/payments/checkout-5b5fd56dbf-4cnmv" in built.supporting_signal_ids
        )

    def test_a_pod_it_does_not_select_does_not(self):
        built = self.evaluate(crash("secret-reader-7b79d59f9d-klmfz", "payments"))
        assert built.supporting_signal_ids == (
            "graph.service_backends_failing:service/payments/checkout-svc",
        )

    def test_another_services_missing_endpoints_does_not(self):
        other = Signal.create(
            SignalType.NETWORK_NO_ENDPOINTS,
            Severity.CRITICAL,
            "",
            ResourceRef(kind="Service", name="gateway-svc", namespace="payments"),
            ("k8s.network:t",),
        )
        assert len(self.evaluate(other).supporting_signal_ids) == 1
