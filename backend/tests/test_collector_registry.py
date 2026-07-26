import pytest

from app.collectors.base import BaseCollector
from app.collectors.registry import CollectorGraphError, CollectorRegistry


class StubCollector(BaseCollector):
    def __init__(self, collector_id, provides, requires=frozenset(), optional=frozenset()):
        self.id = collector_id
        self.provides = frozenset(provides)
        self.requires = frozenset(requires)
        self.optional_requires = frozenset(optional)

    async def collect(self, context):
        return []


def wave_ids(waves):
    return [sorted(collector.id for collector in wave) for wave in waves]


def test_independent_collectors_share_one_wave():
    registry = CollectorRegistry([StubCollector("a", {"k.a"}), StubCollector("b", {"k.b"})])
    assert wave_ids(registry.resolve()) == [["a", "b"]]


def test_dependencies_are_ordered_into_later_waves():
    registry = CollectorRegistry(
        [
            StubCollector("logs", {"k.logs"}, requires={"k.pods"}),
            StubCollector("pods", {"k.pods"}),
            StubCollector("summary", {"k.sum"}, requires={"k.logs"}),
        ]
    )
    assert wave_ids(registry.resolve()) == [["pods"], ["logs"], ["summary"]]


def test_missing_hard_requirement_is_a_wiring_error():
    registry = CollectorRegistry([StubCollector("logs", {"k.logs"}, requires={"k.pods"})])
    with pytest.raises(CollectorGraphError, match=r"k\.pods"):
        registry.resolve()


def test_missing_optional_requirement_is_tolerated():
    registry = CollectorRegistry([StubCollector("rca", {"k.rca"}, optional={"prometheus.cpu"})])
    assert wave_ids(registry.resolve()) == [["rca"]]


def test_optional_requirement_orders_after_its_provider():
    registry = CollectorRegistry(
        [
            StubCollector("rca", {"k.rca"}, optional={"prom.cpu"}),
            StubCollector("prom", {"prom.cpu"}),
        ]
    )
    assert wave_ids(registry.resolve()) == [["prom"], ["rca"]]


def test_cycles_are_rejected():
    registry = CollectorRegistry(
        [
            StubCollector("a", {"k.a"}, requires={"k.b"}),
            StubCollector("b", {"k.b"}, requires={"k.a"}),
        ]
    )
    with pytest.raises(CollectorGraphError, match="Cyclic"):
        registry.resolve()


def test_duplicate_ids_are_rejected():
    registry = CollectorRegistry([StubCollector("a", {"k.a"})])
    with pytest.raises(CollectorGraphError, match="Duplicate"):
        registry.register(StubCollector("a", {"k.other"}))


def test_collector_providing_nothing_is_rejected():
    with pytest.raises(CollectorGraphError, match="provides no evidence"):
        CollectorRegistry([StubCollector("a", set())])
