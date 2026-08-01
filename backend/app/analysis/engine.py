from collections.abc import Sequence

from loguru import logger

from app.analysis.deep_signal_rules import DEEP_SIGNAL_RULES
from app.analysis.graph_signal_rules import GRAPH_SIGNAL_RULES
from app.analysis.hypothesis_rules import DEFAULT_HYPOTHESIS_RULES, HypothesisRule, rank
from app.analysis.models import AnalysisResult, Signal
from app.analysis.observability_signal_rules import OBSERVABILITY_SIGNAL_RULES
from app.analysis.signal_rules import DEFAULT_SIGNAL_RULES, AnalysisInput, SignalRule


class AnalysisEngine:
    """Derives signals and ranked hypotheses from an investigation.

    This layer is entirely deterministic. It runs before any model call, and its
    output is the only thing the reasoning layer is allowed to talk about.
    """

    def __init__(
        self,
        signal_rules: Sequence[SignalRule] | None = None,
        hypothesis_rules: Sequence[HypothesisRule] | None = None,
    ) -> None:
        self.signal_rules = tuple(
            signal_rules
            if signal_rules is not None
            else (
                *DEFAULT_SIGNAL_RULES,
                *DEEP_SIGNAL_RULES,
                *OBSERVABILITY_SIGNAL_RULES,
                *GRAPH_SIGNAL_RULES,
            )
        )
        self.hypothesis_rules = tuple(hypothesis_rules or DEFAULT_HYPOTHESIS_RULES)

    def analyze(self, investigation: dict) -> AnalysisResult:
        data = AnalysisInput(investigation=investigation)
        signals = self._extract_signals(data)
        hypotheses = self._build_hypotheses(signals)

        logger.info(
            "Analysis derived {signals} signal(s) and {hypotheses} hypothesis(es)",
            signals=len(signals),
            hypotheses=len(hypotheses),
        )
        return AnalysisResult(signals=signals, hypotheses=hypotheses)

    def _extract_signals(self, data: AnalysisInput) -> tuple[Signal, ...]:
        """Run every signal rule, isolating failures to the rule that caused them."""
        collected: dict[str, Signal] = {}

        for rule in self.signal_rules:
            try:
                produced = rule.extract(data)
            except Exception as exc:
                logger.opt(exception=exc).error("Signal rule {id} failed", id=rule.id)
                continue

            for signal in produced:
                # Deterministic ids make duplicate observations idempotent.
                collected.setdefault(signal.id, signal)

        return tuple(sorted(collected.values(), key=lambda item: (-item.severity.weight, item.id)))

    def _build_hypotheses(self, signals: Sequence[Signal]):
        hypotheses = []

        for rule in self.hypothesis_rules:
            try:
                hypothesis = rule.evaluate(signals)
            except Exception as exc:
                logger.opt(exception=exc).error("Hypothesis rule {id} failed", id=rule.id)
                continue

            if hypothesis is not None:
                hypotheses.append(hypothesis)

        return rank(hypotheses)
