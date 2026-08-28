import asyncio
import time
from dataclasses import replace

from loguru import logger

from app.ai.evidence_redactor import EvidenceRedactor
from app.collectors.base import CollectionContext, Collector
from app.collectors.registry import CollectorRegistry
from app.evidence.models import Evidence, EvidenceSource, EvidenceStatus
from app.evidence.store import EvidenceStore
from app.observability import metrics
from app.providers.cache import FreshnessWindow, freshness_window


class CollectionScheduler:
    """Runs collectors in dependency order with per-collector fault isolation.

    Three guarantees hold regardless of what any individual collector does:

    1. A collector that raises, hangs, or overruns the budget degrades only its
       own evidence; the investigation continues with the rest.
    2. Every declared evidence kind ends up in the store, even if only as a
       non-usable record explaining why it is missing.
    3. Nothing enters the store un-redacted, so persisted reports and API
       responses cannot leak secrets that were present in raw cluster output.
    """

    def __init__(
        self,
        registry: CollectorRegistry,
        redactor: EvidenceRedactor | None = None,
    ) -> None:
        self.registry = registry
        self.redactor = redactor or EvidenceRedactor()

    async def run(self, context: CollectionContext) -> EvidenceStore:
        started = time.perf_counter()
        # Evidence from earlier rounds already satisfies dependencies.
        waves = self.registry.resolve(available=frozenset(context.store.statuses()))
        semaphore = asyncio.Semaphore(context.budget.max_concurrency)

        for index, wave in enumerate(waves, start=1):
            runnable = [collector for collector in wave if self._should_run(collector, context)]
            skipped = [collector for collector in wave if collector not in runnable]

            for collector in skipped:
                context.store.extend(self._skipped_evidence(collector, context))

            if not runnable:
                continue

            logger.info(
                "Collection wave {index}/{total}: {collectors}",
                index=index,
                total=len(waves),
                collectors=", ".join(collector.id for collector in runnable),
            )

            context.report(
                f"Collecting evidence (wave {index} of {len(waves)})",
                wave=index,
                total_waves=len(waves),
                collectors=[collector.id for collector in runnable],
            )

            results = await asyncio.gather(
                *(self._run_collector(collector, context, semaphore) for collector in runnable)
            )
            for evidence_items in results:
                context.store.extend(evidence_items)

        coverage = context.store.coverage()
        # By status, never by kind or target: status is a closed enum, while
        # kinds grow with every collector and targets are cluster objects.
        metrics.collection_finished(
            time.perf_counter() - started,
            {str(status): count for status, count in (coverage.get("by_status") or {}).items()},
        )
        logger.info(
            "Collection complete: {count} evidence records, {completeness}% usable",
            count=len(context.store),
            completeness=coverage["completeness"],
        )
        return context.store

    def _should_run(self, collector: Collector, context: CollectionContext) -> bool:
        if not collector.applicable(context):
            return False
        return all(context.store.has(kind) for kind in collector.requires)

    def _missing_requirements(self, collector: Collector, context: CollectionContext) -> list[str]:
        return sorted(kind for kind in collector.requires if not context.store.has(kind))

    def _skipped_evidence(
        self,
        collector: Collector,
        context: CollectionContext,
    ) -> list[Evidence]:
        missing = self._missing_requirements(collector, context)
        if missing:
            detail = f"Skipped because required evidence was unavailable: {', '.join(missing)}."
        else:
            detail = "Skipped because it does not apply to this investigation scope."

        return [
            Evidence.create(
                kind=kind,
                status=EvidenceStatus.NOT_APPLICABLE,
                target=context.scope.cluster_ref,
                source=EvidenceSource.KUBECTL,
                detail=detail,
                collector_id=collector.id,
            )
            for kind in sorted(collector.provides)
        ]

    async def _run_collector(
        self,
        collector: Collector,
        context: CollectionContext,
        semaphore: asyncio.Semaphore,
    ) -> list[Evidence]:
        timeout = context.timeout_for_next()
        if timeout <= 0:
            return self._degraded_evidence(
                collector,
                context,
                EvidenceStatus.TIMEOUT,
                "Investigation collection budget was exhausted before this collector ran.",
            )

        started = time.monotonic()
        async with semaphore:
            # Everything this collector reads is observed inside the window, so
            # evidence built from a reused read can be stamped with the age of
            # the read rather than the age of the investigation. Opened here
            # rather than around the whole wave because the window has to be
            # per collector: one collector working from a cached list must not
            # backdate another that read the cluster live.
            with freshness_window() as window:
                try:
                    collected = await asyncio.wait_for(collector.collect(context), timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "Collector {id} exceeded its {timeout:.0f}s budget",
                        id=collector.id,
                        timeout=timeout,
                    )
                    return self._degraded_evidence(
                        collector,
                        context,
                        EvidenceStatus.TIMEOUT,
                        f"Collector exceeded its {timeout:.0f}s time budget.",
                    )
                except Exception as exc:
                    logger.opt(exception=exc).error("Collector {id} failed", id=collector.id)
                    return self._degraded_evidence(
                        collector,
                        context,
                        EvidenceStatus.FAILED,
                        f"Collector raised {type(exc).__name__}: {exc}",
                    )

        duration_ms = int((time.monotonic() - started) * 1000)
        context.report(
            getattr(collector, "label", "") or f"Collected {collector.id}",
            collector=collector.id,
            duration_ms=duration_ms,
        )
        return [self._sanitize(item, duration_ms, window) for item in collected]

    def _degraded_evidence(
        self,
        collector: Collector,
        context: CollectionContext,
        status: EvidenceStatus,
        detail: str,
    ) -> list[Evidence]:
        return [
            Evidence.create(
                kind=kind,
                status=status,
                target=context.scope.cluster_ref,
                source=EvidenceSource.KUBECTL,
                detail=detail,
                collector_id=collector.id,
            )
            for kind in sorted(collector.provides)
        ]

    def _sanitize(
        self,
        evidence: Evidence,
        duration_ms: int,
        window: FreshnessWindow | None = None,
    ) -> Evidence:
        """Redact at the collection boundary, and date the record honestly.

        Redacting here rather than at the prompt boundary means every consumer
        — reports on disk, the HTTP API, and the LLM — sees the same scrubbed
        payload, and no consumer can be added later that bypasses it.

        The timestamp is the same argument applied to time. `Evidence` defaults
        `collected_at` to construction time, which is the truth only while
        every read is live. Once a read can be served from
        `app/providers/cache.py`, a record built from it must carry the age of
        the *read* — every conclusion cites an evidence id, so a record dated
        now for a fact observed forty seconds ago is a false citation rather
        than an untidy one. Backdating only, never forward: the window holds
        the oldest read the collector saw, so a collector that mixed cached and
        live reads understates its freshness, which is the safe direction.
        """
        collected_at = evidence.collected_at
        if window is not None and window.oldest is not None and window.oldest < collected_at:
            collected_at = window.oldest

        if evidence.redacted or evidence.data is None:
            return replace(
                evidence,
                duration_ms=evidence.duration_ms or duration_ms,
                collected_at=collected_at,
            )

        return replace(
            evidence,
            data=self.redactor.redact(evidence.data),
            redacted=True,
            duration_ms=evidence.duration_ms or duration_ms,
            collected_at=collected_at,
        )
