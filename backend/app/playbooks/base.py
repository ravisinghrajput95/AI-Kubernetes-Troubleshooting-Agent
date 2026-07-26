"""Playbook contract.

A playbook is a **planner**, not an executor. It inspects the analysis produced
from baseline evidence and emits targeted collectors; the existing scheduler
runs them. That is what lets playbooks inherit fault isolation, redaction,
concurrency and budget enforcement rather than reimplementing any of it.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from app.analysis.models import AnalysisResult
from app.collectors.base import Collector, InvestigationScope
from app.evidence.models import ResourceRef
from app.evidence.store import EvidenceStore

DEFAULT_MAX_TARGETS = 5


@dataclass(frozen=True)
class PlaybookContext:
    """What a playbook sees when deciding what to collect next."""

    scope: InvestigationScope
    analysis: AnalysisResult
    store: EvidenceStore
    max_targets: int = DEFAULT_MAX_TARGETS


class Playbook(Protocol):
    id: str
    title: str
    triggers: frozenset[str]

    def applicable(self, context: PlaybookContext) -> bool: ...

    def plan(self, context: PlaybookContext) -> Sequence[Collector]: ...


class BasePlaybook(ABC):
    """Convenience base: trigger matching and target selection."""

    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    triggers: ClassVar[frozenset[str]] = frozenset()

    def applicable(self, context: PlaybookContext) -> bool:
        return bool(self.matched_signals(context))

    def matched_signals(self, context: PlaybookContext):
        return [signal for signal in context.analysis.signals if signal.type in self.triggers]

    def targets(
        self,
        context: PlaybookContext,
        kinds: frozenset[str] | None = None,
    ) -> list[ResourceRef]:
        """Distinct resources named by the triggering signals, severity first.

        Capped by `max_targets`: a cluster-wide failure can produce hundreds of
        matching signals, and investigating every one would blow the budget
        without adding diagnostic value.
        """
        seen: dict[str, ResourceRef] = {}

        for signal in sorted(
            self.matched_signals(context),
            key=lambda item: item.severity.weight,
            reverse=True,
        ):
            target = signal.target
            if kinds is not None and target.kind not in kinds:
                continue
            seen.setdefault(target.key, target)

        return list(seen.values())[: context.max_targets]

    @abstractmethod
    def plan(self, context: PlaybookContext) -> Sequence[Collector]: ...
