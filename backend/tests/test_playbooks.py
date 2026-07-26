"""Playbook selection, planning, and the deep-investigation loop."""

import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.models import AnalysisResult, Severity, Signal, SignalType
from app.collectors.base import (
    BaseCollector,
    CollectionBudget,
    CollectionContext,
    InvestigationScope,
)
from app.evidence.models import Evidence, EvidenceStatus, ResourceRef
from app.evidence.store import EvidenceStore
from app.playbooks.base import BasePlaybook, PlaybookContext
from app.playbooks.kubernetes import (
    DEFAULT_PLAYBOOKS,
    CrashLoopPlaybook,
    NetworkPlaybook,
    PendingPlaybook,
    StoragePlaybook,
)
from app.playbooks.orchestrator import InvestigationOrchestrator
from app.playbooks.registry import PlaybookRegistry
from app.providers.local_kubectl import LocalKubectlProvider


def pod_signal(signal_type, name="web-0", namespace="prod", severity=Severity.CRITICAL):
    return Signal.create(
        signal_type,
        severity,
        f"{name} is unhealthy",
        ResourceRef(kind="Pod", name=name, namespace=namespace),
        ("k8s.pods:cluster/_cluster/test",),
    )


def playbook_context(*signals, max_targets=5):
    return PlaybookContext(
        scope=InvestigationScope(context="test"),
        analysis=AnalysisResult(signals=tuple(signals)),
        store=EvidenceStore(),
        max_targets=max_targets,
    )


class TestSelection:
    def test_crashloop_playbook_selected_for_crash_loop_signal(self):
        registry = PlaybookRegistry(DEFAULT_PLAYBOOKS)
        selected = registry.select(playbook_context(pod_signal(SignalType.POD_CRASH_LOOP)))
        assert [item.id for item in selected] == ["crashloop"]

    def test_unrelated_signal_selects_nothing(self):
        registry = PlaybookRegistry(DEFAULT_PLAYBOOKS)
        assert registry.select(playbook_context(pod_signal(SignalType.WORKLOAD_DEGRADED))) == []

    def test_several_playbooks_can_apply_at_once(self):
        registry = PlaybookRegistry(DEFAULT_PLAYBOOKS)
        selected = registry.select(
            playbook_context(
                pod_signal(SignalType.POD_CRASH_LOOP),
                pod_signal(SignalType.POD_PENDING, name="db-0"),
                pod_signal(SignalType.STORAGE_PVC_UNBOUND, name="db-data"),
            )
        )
        assert {item.id for item in selected} == {"crashloop", "pending", "storage"}

    def test_a_playbook_raising_during_selection_is_skipped(self):
        class Broken(BasePlaybook):
            id = "broken"
            triggers = frozenset({SignalType.POD_CRASH_LOOP})

            def applicable(self, context):
                raise RuntimeError("selection bug")

            def plan(self, context):
                return []

        registry = PlaybookRegistry([Broken(), CrashLoopPlaybook()])
        selected = registry.select(playbook_context(pod_signal(SignalType.POD_CRASH_LOOP)))
        assert [item.id for item in selected] == ["crashloop"]

    def test_duplicate_playbook_ids_are_rejected(self):
        with pytest.raises(ValueError, match="Duplicate"):
            PlaybookRegistry([CrashLoopPlaybook(), CrashLoopPlaybook()])


class TestPlanning:
    def test_crashloop_plans_the_evidence_its_hypothesis_lacks(self):
        collectors = CrashLoopPlaybook().plan(
            playbook_context(pod_signal(SignalType.POD_CRASH_LOOP))
        )
        prefixes = {collector.id.split(":")[0] for collector in collectors}
        assert prefixes == {
            "k8s.pod.spec",
            "k8s.pod.logs.previous",
            "k8s.resource.events",
            "k8s.pod.config_refs",
            # Optional backends are always planned; an absent one records why.
            "prometheus.pod.metrics",
            "loki.pod.logs",
        }

    def test_targets_are_capped(self):
        signals = [
            pod_signal(SignalType.POD_CRASH_LOOP, name=f"web-{index}") for index in range(20)
        ]
        collectors = CrashLoopPlaybook().plan(playbook_context(*signals, max_targets=3))
        targets = {collector.id.split(":", 1)[1] for collector in collectors}
        assert len(targets) == 3

    def test_targets_prefer_the_most_severe_signals(self):
        context = playbook_context(
            pod_signal(SignalType.POD_ERROR, name="low", severity=Severity.HIGH),
            pod_signal(SignalType.POD_CRASH_LOOP, name="high", severity=Severity.CRITICAL),
            max_targets=1,
        )
        collectors = CrashLoopPlaybook().plan(context)
        assert all("high" in collector.id for collector in collectors)

    def test_pending_adds_namespace_scoped_quota_checks(self):
        collectors = PendingPlaybook().plan(
            playbook_context(pod_signal(SignalType.POD_PENDING, name="db-0"))
        )
        prefixes = {collector.id.split(":")[0] for collector in collectors}
        assert "k8s.quotas" in prefixes
        assert "k8s.limitranges" in prefixes

    def test_network_only_checks_dns_when_dns_is_implicated(self):
        without_dns = NetworkPlaybook().plan(
            playbook_context(pod_signal(SignalType.NETWORK_NO_ENDPOINTS, name="api"))
        )
        assert not any("dns" in collector.id for collector in without_dns)

        with_dns = NetworkPlaybook().plan(
            playbook_context(pod_signal(SignalType.NETWORK_DNS_MISSING, name="kube-dns"))
        )
        assert any("dns" in collector.id for collector in with_dns)

    def test_storage_always_checks_cluster_scoped_resources(self):
        collectors = StoragePlaybook().plan(
            playbook_context(pod_signal(SignalType.STORAGE_PVC_UNBOUND, name="db-data"))
        )
        prefixes = {collector.id.split(":")[0] for collector in collectors}
        assert {"k8s.storageclasses", "k8s.volumeattachments"} <= prefixes


class RecordingCollector(BaseCollector):
    def __init__(self, collector_id, kind, log):
        self.id = collector_id
        self.provides = frozenset({kind})
        self.requires = frozenset()
        self.optional_requires = frozenset()
        self.kind = kind
        self._log = log

    async def collect(self, context):
        self._log.append(self.id)
        return [
            Evidence.create(
                kind=self.kind,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                data={"ok": True},
                collector_id=self.id,
            )
        ]


class StubPlaybook(BasePlaybook):
    id = "stub"
    triggers = frozenset({SignalType.POD_CRASH_LOOP})

    def __init__(self, collectors):
        self._collectors = collectors

    def applicable(self, context):
        return True

    def plan(self, context):
        return self._collectors


class StubEngine(AnalysisEngine):
    """Returns a fixed analysis so orchestration can be tested in isolation."""

    def __init__(self, result):
        super().__init__()
        self._result = result
        self.calls = 0

    def analyze(self, investigation):
        self.calls += 1
        return self._result


def make_context(budget=None):
    return CollectionContext(
        scope=InvestigationScope(context="test"),
        provider=LocalKubectlProvider(context="test"),
        budget=budget or CollectionBudget(),
    )


class TestOrchestration:
    async def test_baseline_runs_then_the_playbook_round(self):
        log = []
        baseline = RecordingCollector("baseline", "k.base", log)
        deep = RecordingCollector("deep", "k.deep", log)

        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry([StubPlaybook([deep])]),
            engine=StubEngine(AnalysisResult(signals=(pod_signal(SignalType.POD_CRASH_LOOP),))),
        )
        result = await orchestrator.run(make_context(), [baseline], lambda store: {})

        assert log == ["baseline", "deep"]
        assert result.store.has("k.deep")
        assert result.rounds[0].playbooks == ["stub"]
        assert result.rounds[0].evidence_added == 1

    async def test_no_applicable_playbook_means_no_second_round(self):
        log = []
        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry(DEFAULT_PLAYBOOKS),
            engine=StubEngine(AnalysisResult()),
        )
        result = await orchestrator.run(
            make_context(), [RecordingCollector("baseline", "k.base", log)], lambda s: {}
        )

        assert result.rounds == []
        assert log == ["baseline"]

    async def test_collectors_are_not_repeated_across_rounds(self):
        log = []
        deep = RecordingCollector("deep", "k.deep", log)
        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry([StubPlaybook([deep])]),
            engine=StubEngine(AnalysisResult(signals=(pod_signal(SignalType.POD_CRASH_LOOP),))),
            max_rounds=3,
        )
        result = await orchestrator.run(
            make_context(), [RecordingCollector("baseline", "k.base", log)], lambda s: {}
        )

        assert log.count("deep") == 1
        assert len(result.rounds) == 1

    async def test_a_failing_planner_does_not_break_the_investigation(self):
        class ExplodingPlaybook(BasePlaybook):
            id = "boom"
            triggers = frozenset({SignalType.POD_CRASH_LOOP})

            def plan(self, context):
                raise RuntimeError("planning bug")

        log = []
        deep = RecordingCollector("deep", "k.deep", log)
        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry([ExplodingPlaybook(), StubPlaybook([deep])]),
            engine=StubEngine(AnalysisResult(signals=(pod_signal(SignalType.POD_CRASH_LOOP),))),
        )
        result = await orchestrator.run(
            make_context(), [RecordingCollector("baseline", "k.base", log)], lambda s: {}
        )

        assert "deep" in log
        assert result.rounds[0].playbooks == ["stub"]

    async def test_exhausted_budget_skips_deep_collection(self):
        log = []
        deep = RecordingCollector("deep", "k.deep", log)
        context = make_context(CollectionBudget(total_deadline=0.0))

        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry([StubPlaybook([deep])]),
            engine=StubEngine(AnalysisResult(signals=(pod_signal(SignalType.POD_CRASH_LOOP),))),
        )
        result = await orchestrator.run(
            context, [RecordingCollector("baseline", "k.base", log)], lambda s: {}
        )

        assert "deep" not in log
        assert result.rounds == []

    async def test_deep_collectors_may_depend_on_baseline_evidence(self):
        """Round two must not need baseline collectors re-registered."""
        log = []

        class Dependent(RecordingCollector):
            def __init__(self, log):
                super().__init__("dependent", "k.deep", log)
                self.requires = frozenset({"k.base"})

        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry([StubPlaybook([Dependent(log)])]),
            engine=StubEngine(AnalysisResult(signals=(pod_signal(SignalType.POD_CRASH_LOOP),))),
        )
        result = await orchestrator.run(
            make_context(), [RecordingCollector("baseline", "k.base", log)], lambda s: {}
        )

        assert "dependent" in log
        assert result.store.has("k.deep")
