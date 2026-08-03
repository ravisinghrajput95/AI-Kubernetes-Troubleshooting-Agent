"""Reasoning quality: what the diagnosis says beyond naming a root cause.

Tier 3 of `docs/QA_AUDIT_2026-08-03.md`. These are not detection tests — the
platform already found these faults. They are about the difference between a
correct answer and a usable one: whether an ordering explains itself, whether
nine concurrent faults read as nine or as one, whether "no endpoints" says
*which* of two unrelated problems it is, and whether a log line that names the
root cause is quoted or merely counted.
"""

from app.analysis.engine import AnalysisEngine
from app.analysis.hypothesis_rules import UNCORROBORATED_PENALTY, SignalPatternRule
from app.analysis.incidents import group_incidents, selection_rationale
from app.analysis.models import ResourceRef, Severity, Signal, SignalType

ENGINE = AnalysisEngine()

EVIDENCE_KINDS = (
    "k8s.pods",
    "k8s.pods.logs",
    "k8s.events",
    "k8s.deployments",
    "k8s.network",
    "k8s.nodes",
    "k8s.storage",
    "k8s.workloads",
)


def investigation(**sections):
    base = {
        "context": "test-cluster",
        "health": {"status": "issues_found"},
        "evidence": [{"id": f"{k}:cluster/_cluster/t", "kind": k} for k in EVIDENCE_KINDS],
    }
    base.update(sections)
    return base


def hypothesis(hid, *, severity, confidence, supporting=(), title=None, pod=None, service=None):
    """A ranked hypothesis, built directly so ordering can be controlled."""
    rule = SignalPatternRule(
        id=hid,
        title=title or hid,
        category="test",
        rationale="",
        triggers=frozenset({SignalType.POD_CRASH_LOOP}),
    )
    target = (
        ResourceRef(kind="Service", name=service, namespace="prod")
        if service
        else ResourceRef(kind="Pod", name=pod or hid, namespace="prod")
    )
    signal = Signal.create(SignalType.POD_CRASH_LOOP, severity, "", target, ("k8s.pods:t",))
    built = rule.evaluate([signal])
    return type(built)(
        **{
            **{f: getattr(built, f) for f in built.__dataclass_fields__},
            "confidence": confidence,
            "supporting_signal_ids": tuple(supporting) or built.supporting_signal_ids,
        }
    )


class TestTheOrderingExplainsItself:
    """Ranking is severity first, confidence second, so the selected
    explanation can carry *lower* confidence than one listed below it — the
    audit saw a CRITICAL at 90% chosen over a HIGH at 92%. Correct, and
    invisible: nothing told the reader severity was the tiebreak."""

    def test_it_explains_a_lower_confidence_winner(self):
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, title="Service is down"),
            hypothesis("b", severity=Severity.HIGH, confidence=92, title="App fails to start"),
        )

        text = selection_rationale(ranked)

        assert "Service is down" in text
        assert "App fails to start" in text
        assert "90" in text and "92" in text
        assert "critical" in text and "high" in text

    def test_it_says_nothing_when_the_winner_is_also_the_most_confident(self):
        """The common case. A sentence explaining an ordering that needs no
        explanation is noise in a report that omits empty sections by rule."""
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=92),
            hypothesis("b", severity=Severity.HIGH, confidence=70),
        )

        assert selection_rationale(ranked) == ""

    def test_it_says_nothing_when_confidence_ties(self):
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=80),
            hypothesis("b", severity=Severity.HIGH, confidence=80),
        )

        assert selection_rationale(ranked) == ""

    def test_no_hypotheses_is_not_an_error(self):
        assert selection_rationale(()) == ""

    def test_both_new_fields_reach_the_diagnosis(self):
        """A rationale nothing renders explains nothing, and an incident list
        the API does not return groups nothing.

        Asserted on the *diagnosis* rather than on the helpers, because
        `_normalize` spreads the fallback: a field added to one path and not
        the other would leave the model path silently missing it, which is the
        failure the "any new field must come from `_fallback()` first" rule
        exists to prevent.
        """
        from app.ai.root_cause_analyzer import RootCauseAnalyzer

        diagnosis = RootCauseAnalyzer().analyze(
            investigation(
                severity={"severity": "Critical"},
                evidence_coverage={"completeness": 90, "degraded": []},
                logs={"logs": []},
                metrics={"available": True},
                pods={
                    "problematic_pods": [
                        {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                    ]
                },
            )
        )

        assert "selection_rationale" in diagnosis
        assert isinstance(diagnosis["incidents"], list)
        assert diagnosis["incidents"], "a diagnosed cluster must have at least one incident"
        assert diagnosis["incidents"][0]["primary_hypothesis"] == diagnosis["selected_hypothesis"]


class TestConcurrentFaultsReadAsSeparateProblems:
    """Nine broken workloads produced one root cause and a flat list of ten
    hypotheses. Nothing said which entries were the same problem seen twice and
    which were unrelated outages happening at once."""

    def test_two_explanations_of_one_workload_are_one_incident(self):
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, pod="web-abc12-9v2cd"),
            hypothesis("b", severity=Severity.HIGH, confidence=80, pod="web-abc12-9v2cd"),
        )

        incidents = group_incidents(ranked)

        assert len(incidents) == 1
        assert len(incidents[0].hypotheses) == 2

    def test_different_workloads_are_different_incidents(self):
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, pod="ledger-abc12-9v2cd"),
            hypothesis("b", severity=Severity.HIGH, confidence=80, pod="archiver-def34-2ktq5"),
        )

        assert len(group_incidents(ranked)) == 2

    def test_replicas_of_one_deployment_are_one_incident(self):
        """Three pods of a Deployment failing the same way is one problem, not
        three. The replicaset hash and pod suffix are stripped."""
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, pod="web-77bd6cbdf4-2c5ln"),
            hypothesis("b", severity=Severity.HIGH, confidence=80, pod="web-77bd6cbdf4-2ktq5"),
        )

        incidents = group_incidents(ranked)

        assert len(incidents) == 1
        assert incidents[0].id == "workload/prod/web"

    def test_a_shared_signal_does_not_merge_unrelated_workloads(self):
        """The regression that made the first implementation useless.

        A hypothesis cites *every* signal of its supporting types across the
        whole scope — on the audit cluster one cited 35, spanning every broken
        workload. Grouping on shared citations therefore merged all fourteen
        faults into a single incident containing every hypothesis, which
        distinguished nothing. Hypotheses are scope-wide, not per-resource.
        """
        shared = ("pod.pending:pod/prod/unrelated-0", "event.warning:pod/prod/other-0")
        ranked = (
            hypothesis(
                "a",
                severity=Severity.CRITICAL,
                confidence=90,
                pod="ledger-a1b2c-9v2cd",
                supporting=shared,
            ),
            hypothesis(
                "b",
                severity=Severity.HIGH,
                confidence=80,
                pod="archiver-d3e4f-2ktq5",
                supporting=shared,
            ),
        )

        assert len(group_incidents(ranked)) == 2

    def test_non_pod_targets_are_not_folded(self):
        """Stripping segments from a Service or claim name would merge
        genuinely separate resources sharing a prefix."""
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, service="checkout-svc"),
            hypothesis("b", severity=Severity.HIGH, confidence=80, service="checkout-admin"),
        )

        assert len(group_incidents(ranked)) == 2

    def test_the_first_incident_leads_with_the_selected_hypothesis(self):
        """Incidents must not re-rank. A different order here would contradict
        `selected_hypothesis` for the top incident."""
        ranked = (
            hypothesis("a", severity=Severity.CRITICAL, confidence=90, pod="one-a1b2c-9v2cd"),
            hypothesis("b", severity=Severity.HIGH, confidence=92, pod="two-d3e4f-2ktq5"),
        )

        incidents = group_incidents(ranked)

        assert incidents[0].primary.id == "a"

    def test_no_hypotheses_produces_no_incidents(self):
        assert group_incidents(()) == ()


class TestThinlySupportedHypothesesAreLessConfident:
    """Confidence should express how well evidence converges. Without a penalty
    a rule with a high base score reads the same whether ten signals agree or
    one does."""

    def rule(self, base=80):
        return SignalPatternRule(
            id="test.thin",
            title="t",
            category="c",
            rationale="",
            triggers=frozenset({SignalType.POD_CRASH_LOOP}),
            supporting=frozenset({SignalType.EVENT_BACKOFF}),
            base_confidence=base,
        )

    def signal(self, signal_type, name="web-0"):
        return Signal.create(
            signal_type,
            Severity.CRITICAL,
            "",
            ResourceRef(kind="Pod", name=name, namespace="prod"),
            ("k8s.pods:t",),
        )

    def test_a_single_uncorroborated_signal_is_penalised(self):
        built = self.rule().evaluate([self.signal(SignalType.POD_CRASH_LOOP)])

        assert built.confidence == 80 - UNCORROBORATED_PENALTY

    def test_corroboration_removes_the_penalty(self):
        built = self.rule().evaluate(
            [self.signal(SignalType.POD_CRASH_LOOP), self.signal(SignalType.EVENT_BACKOFF)]
        )

        assert built.confidence > 80

    def test_two_triggering_signals_are_their_own_corroboration(self):
        """Two pods showing the same fault is convergence, even with nothing
        in the `supporting` set."""
        built = self.rule().evaluate(
            [
                self.signal(SignalType.POD_CRASH_LOOP, "web-0"),
                self.signal(SignalType.POD_CRASH_LOOP, "web-1"),
            ]
        )

        assert built.confidence == 80

    def test_severity_is_untouched(self):
        """A penalty on breadth, not on seriousness: one CRITICAL signal is
        still critical and still ranks first."""
        built = self.rule().evaluate([self.signal(SignalType.POD_CRASH_LOOP)])

        assert built.severity is Severity.CRITICAL


class TestNoEndpointsSaysWhichProblemItIs:
    """'No ready endpoints' is true of two faults with nothing in common:
    a selector that matches nothing, or a selector that matches pods which are
    all unhealthy. One is fixed by editing labels, the other by repairing the
    workload."""

    def service(self, selector, pod_labels, namespace="prod"):
        return investigation(
            network={"findings": [], "selectors": {f"{namespace}/api": selector}},
            pods={
                "problematic_pods": [],
                "pod_inventory": [{"name": "web-0", "namespace": namespace, "labels": pod_labels}],
            },
        )

    def test_a_selector_matching_nothing_is_named(self):
        result = ENGINE.analyze(self.service({"app": "api-v2"}, {"app": "api"}))

        signal = result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)[0]
        assert "matches no pod" in signal.summary
        assert "app=api-v2" in signal.summary

    def test_a_matching_selector_is_silent(self):
        result = ENGINE.analyze(self.service({"app": "api"}, {"app": "api"}))

        assert not result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)

    def test_extra_pod_labels_still_match(self):
        """Kubernetes selector semantics: every selector key must match, and
        the pod may carry any number of other labels."""
        result = ENGINE.analyze(
            self.service({"app": "api"}, {"app": "api", "version": "3", "tier": "web"})
        )

        assert not result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)

    def test_a_partial_selector_match_does_not_count(self):
        """Every key must match. One of two is not a match."""
        result = ENGINE.analyze(
            self.service({"app": "api", "tier": "web"}, {"app": "api", "tier": "batch"})
        )

        assert result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)

    def test_it_stays_silent_when_no_pods_were_collected(self):
        """The guard that matters. A scope that read no pods would otherwise
        report every service in the cluster as broken — absence of evidence
        presented as evidence of absence."""
        result = ENGINE.analyze(
            investigation(
                network={"findings": [], "selectors": {"prod/api": {"app": "api"}}},
                pods={"problematic_pods": [], "pod_inventory": []},
            )
        )

        assert not result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)

    def test_a_service_with_no_selector_is_not_a_mismatch(self):
        """Selector-less services are routed by hand-managed endpoints and are
        a different finding (`network.no_selector`) entirely."""
        result = ENGINE.analyze(self.service({}, {"app": "api"}))

        assert not result.by_type(SignalType.NETWORK_SELECTOR_MATCHES_NOTHING)


class TestTheLogLineIsQuoted:
    """`sample_lines` always carried the text, but only the tally reached the
    summary — which is what the report renders and what a reader sees first. On
    the audit cluster the application printed its own root cause and the
    diagnosis said 'logs contain 4 failure line(s)'."""

    def logs(self, *lines):
        return investigation(
            logs={"logs": [{"name": "web-0", "namespace": "prod", "relevant_lines": list(lines)}]}
        )

    def test_the_first_line_appears_verbatim(self):
        result = ENGINE.analyze(self.logs("FATAL: config key DB_HOST is not set"))

        summary = result.by_type(SignalType.LOGS_ERROR_PATTERN)[0].summary
        assert "FATAL: config key DB_HOST is not set" in summary

    def test_the_remaining_count_is_kept(self):
        result = ENGINE.analyze(self.logs("first failure", "second", "third"))

        assert "(+2 more)" in result.by_type(SignalType.LOGS_ERROR_PATTERN)[0].summary

    def test_a_single_line_says_nothing_about_others(self):
        result = ENGINE.analyze(self.logs("only failure"))

        assert "more)" not in result.by_type(SignalType.LOGS_ERROR_PATTERN)[0].summary

    def test_a_long_line_is_truncated(self):
        """A stack trace must not push the summary out of the report's line
        budget — the PDF is hand-wrapped."""
        result = ENGINE.analyze(self.logs("x" * 900))

        assert len(result.by_type(SignalType.LOGS_ERROR_PATTERN)[0].summary) < 260
