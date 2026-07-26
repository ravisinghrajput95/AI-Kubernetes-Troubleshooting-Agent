import asyncio

from app.collectors.base import (
    BaseCollector,
    CollectionBudget,
    CollectionContext,
    InvestigationScope,
)
from app.collectors.registry import CollectorRegistry
from app.collectors.scheduler import CollectionScheduler
from app.evidence.models import Evidence, EvidenceStatus


class RecordingCollector(BaseCollector):
    """Collector whose behaviour is supplied per test."""

    def __init__(self, collector_id, kind, behaviour=None, requires=frozenset()):
        self.id = collector_id
        self.provides = frozenset({kind})
        self.requires = frozenset(requires)
        self.optional_requires = frozenset()
        self.kind = kind
        self._behaviour = behaviour
        self.ran = False

    async def collect(self, context):
        self.ran = True
        if self._behaviour is not None:
            return await self._behaviour(context, self)
        return [
            Evidence.create(
                kind=self.kind,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                data={"ok": True},
                collector_id=self.id,
            )
        ]


def make_context(budget=None):
    return CollectionContext(
        scope=InvestigationScope(context="test-cluster"),
        kubectl=None,
        budget=budget or CollectionBudget(),
    )


async def run(collectors, context=None):
    context = context or make_context()
    scheduler = CollectionScheduler(CollectorRegistry(collectors))
    return await scheduler.run(context)


async def test_healthy_collection_stores_usable_evidence():
    store = await run([RecordingCollector("a", "k.a")])
    assert store.has("k.a")
    assert store.data("k.a") == {"ok": True}


async def test_raising_collector_does_not_abort_the_investigation():
    async def boom(context, collector):
        raise RuntimeError("kubectl exploded")

    healthy = RecordingCollector("healthy", "k.ok")
    store = await run([RecordingCollector("bad", "k.bad", behaviour=boom), healthy])

    assert healthy.ran is True
    assert store.has("k.ok") is True

    failed = store.first("k.bad")
    assert failed.status is EvidenceStatus.FAILED
    assert "kubectl exploded" in failed.detail


async def test_hanging_collector_is_bounded_by_its_timeout():
    async def hang(context, collector):
        await asyncio.sleep(10)
        return []

    context = make_context(CollectionBudget(per_collector_timeout=0.05))
    store = await run([RecordingCollector("slow", "k.slow", behaviour=hang)], context)

    assert store.first("k.slow").status is EvidenceStatus.TIMEOUT


async def test_dependent_collector_is_skipped_with_a_stated_reason():
    async def boom(context, collector):
        raise RuntimeError("no cluster")

    dependent = RecordingCollector("logs", "k.logs", requires={"k.pods"})
    store = await run([RecordingCollector("pods", "k.pods", behaviour=boom), dependent])

    assert dependent.ran is False
    skipped = store.first("k.logs")
    assert skipped.status is EvidenceStatus.NOT_APPLICABLE
    assert "k.pods" in skipped.detail


async def test_every_declared_kind_is_present_even_when_degraded():
    async def boom(context, collector):
        raise RuntimeError("failure")

    store = await run(
        [
            RecordingCollector("a", "k.a"),
            RecordingCollector("b", "k.b", behaviour=boom),
            RecordingCollector("c", "k.c", requires={"k.b"}),
        ]
    )
    assert {"k.a", "k.b", "k.c"} <= set(store.statuses())


async def test_coverage_reports_completeness():
    async def boom(context, collector):
        raise RuntimeError("failure")

    store = await run(
        [
            RecordingCollector("a", "k.a"),
            RecordingCollector("b", "k.b", behaviour=boom),
        ]
    )
    coverage = store.coverage()
    assert coverage["total"] == 2
    assert coverage["usable"] == 1
    assert coverage["completeness"] == 50
    assert coverage["degraded"][0]["kind"] == "k.b"


async def test_independent_collectors_run_concurrently():
    started = []

    async def slow(context, collector):
        started.append(collector.id)
        await asyncio.sleep(0.05)
        return [
            Evidence.create(
                kind=collector.kind,
                status=EvidenceStatus.OK,
                target=context.scope.cluster_ref,
                collector_id=collector.id,
            )
        ]

    collectors = [RecordingCollector(f"c{i}", f"k.{i}", behaviour=slow) for i in range(4)]
    loop = asyncio.get_running_loop()
    began = loop.time()
    await run(collectors)
    elapsed = loop.time() - began

    assert len(started) == 4
    # Sequential execution would take ~0.2s; concurrent stays near one interval.
    assert elapsed < 0.15
