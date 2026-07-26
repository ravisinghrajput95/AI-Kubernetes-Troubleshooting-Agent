from collections.abc import Iterable

from app.collectors.base import Collector


class CollectorGraphError(RuntimeError):
    """Raised for an unsatisfiable or cyclic collector graph.

    This is a wiring error surfaced at resolve time rather than a runtime
    condition, so it fails loudly during startup instead of silently dropping
    evidence during an incident.
    """


class CollectorRegistry:
    """Holds collectors and resolves them into ordered execution waves."""

    def __init__(self, collectors: Iterable[Collector] | None = None) -> None:
        self._collectors: dict[str, Collector] = {}
        for collector in collectors or ():
            self.register(collector)

    def register(self, collector: Collector) -> None:
        if not collector.id:
            raise CollectorGraphError(f"{type(collector).__name__} has no id")
        if collector.id in self._collectors:
            raise CollectorGraphError(f"Duplicate collector id: {collector.id}")
        if not collector.provides:
            raise CollectorGraphError(f"Collector {collector.id} provides no evidence kinds")

        self._collectors[collector.id] = collector

    def __len__(self) -> int:
        return len(self._collectors)

    @property
    def collectors(self) -> list[Collector]:
        return list(self._collectors.values())

    def _providers(self) -> dict[str, set[str]]:
        providers: dict[str, set[str]] = {}
        for collector in self._collectors.values():
            for kind in collector.provides:
                providers.setdefault(kind, set()).add(collector.id)
        return providers

    def resolve(self, available: frozenset[str] = frozenset()) -> list[list[Collector]]:
        """Topologically order collectors into waves that can run concurrently.

        Hard `requires` must have a registered provider; a missing one is a
        wiring error. `optional_requires` only influences ordering when a
        provider happens to be registered, which is what lets optional backends
        such as Prometheus be absent without breaking the graph.

        `available` lists evidence kinds already collected in an earlier round.
        A requirement they satisfy needs no provider in this registry, which is
        what lets a playbook round depend on baseline evidence without
        re-registering the collectors that produced it.
        """
        providers = self._providers()
        pending = dict(self._collectors)
        dependencies: dict[str, set[str]] = {}

        for collector in pending.values():
            required_ids: set[str] = set()
            for kind in collector.requires:
                if kind in available:
                    continue
                if kind not in providers:
                    raise CollectorGraphError(
                        f"Collector {collector.id} requires '{kind}' which no collector provides"
                    )
                required_ids |= providers[kind]
            for kind in collector.optional_requires:
                required_ids |= providers.get(kind, set())

            dependencies[collector.id] = required_ids - {collector.id}

        waves: list[list[Collector]] = []
        satisfied: set[str] = set()

        while pending:
            wave_ids = [
                collector_id for collector_id in pending if dependencies[collector_id] <= satisfied
            ]
            if not wave_ids:
                raise CollectorGraphError(
                    f"Cyclic collector dependencies among: {', '.join(sorted(pending))}"
                )

            waves.append([pending.pop(collector_id) for collector_id in wave_ids])
            satisfied |= set(wave_ids)

        return waves
