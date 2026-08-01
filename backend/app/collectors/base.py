import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from app.evidence.models import Evidence, ResourceRef
from app.evidence.store import EvidenceStore
from app.providers.base import ClusterProvider, ProviderResult, ResourceRequest


@dataclass(frozen=True, slots=True)
class InvestigationScope:
    """What the investigation is allowed to look at."""

    context: str | None = None
    namespace: str | None = None
    resource_kind: str | None = None
    resource_name: str | None = None

    @property
    def cluster_ref(self) -> ResourceRef:
        return ResourceRef.cluster(self.context)

    def targets(self, kind: str) -> bool:
        """True when the scope is narrowed to a specific resource of `kind`."""
        return self.resource_kind == kind and bool(self.resource_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace or "all",
            "resource_kind": self.resource_kind or "cluster",
            "resource_name": self.resource_name or "",
        }


@dataclass(frozen=True, slots=True)
class CollectionBudget:
    """Bounds on a single investigation's evidence gathering.

    Deep investigations fan out to many cluster reads; without an explicit
    budget a large cluster can stall an investigation indefinitely.
    """

    max_concurrency: int = 8
    per_collector_timeout: float = 60.0
    total_deadline: float = 240.0


class ProgressReporter(Protocol):
    """Sink for progress notifications emitted during collection."""

    def report(self, message: str, **data: Any) -> None: ...


class NullProgressReporter:
    """Default reporter: collection is silent unless someone is listening."""

    def report(self, message: str, **data: Any) -> None:
        return None


@dataclass
class CollectionContext:
    """Everything a collector needs, and nothing more."""

    scope: InvestigationScope
    provider: ClusterProvider
    store: EvidenceStore = field(default_factory=EvidenceStore)
    budget: CollectionBudget = field(default_factory=CollectionBudget)
    reporter: ProgressReporter = field(default_factory=NullProgressReporter)
    started_at: float = field(default_factory=time.monotonic)

    async def fetch(self, request: ResourceRequest) -> ProviderResult:
        """Fetch evidence declaratively. The interface collectors should use."""
        return await self.provider.fetch(request)

    def report(self, message: str, **data: Any) -> None:
        self.reporter.report(message, **data)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget.total_deadline - self.elapsed)

    def timeout_for_next(self) -> float:
        return min(self.budget.per_collector_timeout, self.remaining)


@runtime_checkable
class Collector(Protocol):
    """Contract for every evidence producer.

    `provides` and `requires` form the dependency graph the scheduler resolves,
    so ordering is declared rather than hardcoded in a caller.
    """

    id: str
    provides: frozenset[str]
    requires: frozenset[str]
    optional_requires: frozenset[str]

    def applicable(self, context: CollectionContext) -> bool: ...

    async def collect(self, context: CollectionContext) -> Sequence[Evidence]: ...


class BaseCollector(ABC):
    """Convenience base supplying the common defaults of the protocol."""

    id: ClassVar[str] = ""
    # Human-readable progress label; falls back to the id when unset.
    label: ClassVar[str] = ""
    provides: ClassVar[frozenset[str]] = frozenset()
    requires: ClassVar[frozenset[str]] = frozenset()
    optional_requires: ClassVar[frozenset[str]] = frozenset()

    def applicable(self, context: CollectionContext) -> bool:
        return True

    @abstractmethod
    async def collect(self, context: CollectionContext) -> Sequence[Evidence]: ...
