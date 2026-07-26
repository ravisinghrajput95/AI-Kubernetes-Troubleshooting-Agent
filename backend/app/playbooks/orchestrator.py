"""Iterative, hypothesis-driven investigation.

Baseline collection answers "what is broken". The analysis that follows names
hypotheses and the evidence each one still needs. Playbooks then collect exactly
that evidence, and the analysis is recomputed against it.

This is the hypothesis → evidence → validate → confidence loop, implemented on
top of the existing collector infrastructure rather than beside it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.ai.evidence_redactor import EvidenceRedactor
from app.analysis.engine import AnalysisEngine
from app.analysis.models import AnalysisResult
from app.collectors.base import CollectionContext, Collector
from app.collectors.registry import CollectorRegistry
from app.collectors.scheduler import CollectionScheduler
from app.evidence.store import EvidenceStore
from app.playbooks.base import DEFAULT_MAX_TARGETS, PlaybookContext
from app.playbooks.registry import PlaybookRegistry

InvestigationViewBuilder = Callable[[EvidenceStore], dict[str, Any]]

DEFAULT_MAX_ROUNDS = 1


@dataclass
class PlaybookRound:
    """What one deep-investigation round did, for the audit trail."""

    round: int
    playbooks: list[str] = field(default_factory=list)
    collectors: list[str] = field(default_factory=list)
    evidence_added: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "playbooks": self.playbooks,
            "collectors": self.collectors,
            "evidence_added": self.evidence_added,
        }


@dataclass
class OrchestrationResult:
    store: EvidenceStore
    analysis: AnalysisResult
    rounds: list[PlaybookRound] = field(default_factory=list)

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.rounds]


class InvestigationOrchestrator:
    def __init__(
        self,
        playbooks: PlaybookRegistry,
        engine: AnalysisEngine | None = None,
        redactor: EvidenceRedactor | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_targets: int = DEFAULT_MAX_TARGETS,
    ) -> None:
        self.playbooks = playbooks
        self.engine = engine or AnalysisEngine()
        self.redactor = redactor or EvidenceRedactor()
        self.max_rounds = max_rounds
        self.max_targets = max_targets

    async def run(
        self,
        context: CollectionContext,
        baseline: Sequence[Collector],
        build_view: InvestigationViewBuilder,
    ) -> OrchestrationResult:
        await self._collect(context, baseline)
        analysis = self.engine.analyze(build_view(context.store))

        rounds: list[PlaybookRound] = []
        executed: set[str] = {collector.id for collector in baseline}

        for round_number in range(1, self.max_rounds + 1):
            if context.remaining <= 0:
                logger.warning("Collection budget exhausted; skipping playbook rounds")
                break

            planned, selected = self._plan(context, analysis, executed)
            if not planned:
                break

            logger.info(
                "Playbook round {round}: {playbooks} -> {count} targeted collector(s)",
                round=round_number,
                playbooks=", ".join(selected),
                count=len(planned),
            )
            context.report(
                f"Running deep investigation ({', '.join(selected)})",
                round=round_number,
                playbooks=selected,
            )

            before = len(context.store)
            await self._collect(context, planned)
            executed |= {collector.id for collector in planned}

            rounds.append(
                PlaybookRound(
                    round=round_number,
                    playbooks=selected,
                    collectors=[collector.id for collector in planned],
                    evidence_added=len(context.store) - before,
                )
            )

            analysis = self.engine.analyze(build_view(context.store))

        return OrchestrationResult(store=context.store, analysis=analysis, rounds=rounds)

    def _plan(
        self,
        context: CollectionContext,
        analysis: AnalysisResult,
        executed: set[str],
    ) -> tuple[list[Collector], list[str]]:
        playbook_context = PlaybookContext(
            scope=context.scope,
            analysis=analysis,
            store=context.store,
            max_targets=self.max_targets,
        )

        planned: dict[str, Collector] = {}
        selected: list[str] = []

        for playbook in self.playbooks.select(playbook_context):
            try:
                collectors = playbook.plan(playbook_context)
            except Exception as exc:
                logger.opt(exception=exc).error(
                    "Playbook {id} failed while planning", id=playbook.id
                )
                continue

            # Skip anything already collected so repeated rounds converge.
            fresh = [item for item in collectors if item.id not in executed]
            if not fresh:
                continue

            selected.append(playbook.id)
            for collector in fresh:
                planned.setdefault(collector.id, collector)

        return list(planned.values()), selected

    async def _collect(
        self,
        context: CollectionContext,
        collectors: Sequence[Collector],
    ) -> None:
        if not collectors:
            return

        # A fresh registry per round; evidence already in the store satisfies
        # dependencies, so baseline collectors need not be re-registered.
        scheduler = CollectionScheduler(CollectorRegistry(collectors), self.redactor)
        await scheduler.run(context)
