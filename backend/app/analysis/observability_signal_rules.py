"""Signal rules over metrics and historical logs.

These only fire when the optional backend was actually queried successfully.
An absent Prometheus or Loki produces no signals — and, importantly, no
*negative* signals either: "no metrics" must never read as "metrics look fine".
"""

from collections.abc import Sequence
from typing import Any

from app.analysis.deep_signal_rules import _provenance, _ref
from app.analysis.models import Severity, Signal, SignalType
from app.analysis.signal_rules import AnalysisInput
from app.collectors.observability import LokiKind, PrometheusKind

MEMORY_NEAR_LIMIT_PERCENT = 85.0

MEMORY_AT_LIMIT_PERCENT = 90.0
"""Working set within a tenth of the limit counts as having reached it.

This was 98%, and against a container the kernel was genuinely OOMKilling every
ninety seconds it never once fired. `container_memory_working_set_bytes` is a
*sampled* gauge — 15s here, commonly 30-60s — and a container that allocates
until it dies does so *between* scrapes, so the last observed sample is always
short of the limit. Measured on a 96Mi container killed six times: peak sample
91.6%. A 98% threshold needs the scrape to land in the final fraction of a
second before the kill, which is a coin toss, not a detector.
"""
CPU_THROTTLE_RATIO = 0.25
NODE_COMMITTED_PERCENT = 90.0
HISTORICAL_ERROR_LINES = 20


class PodMetricsRule:
    id = "observability.pod_metrics"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(PrometheusKind.POD_METRICS):
            payload = entry["data"]
            target = _ref(entry)
            evidence = _provenance(entry)

            signals.extend(self._memory(payload, target, evidence))
            signals.extend(self._cpu(payload, target, evidence))
            signals.extend(self._restarts(payload, target, evidence))

        return signals

    def _memory(self, payload: dict[str, Any], target, evidence) -> list[Signal]:
        """Judge on the worst of peak and current, never on current alone.

        These two answer different questions — "did it ever approach the limit"
        and "is it approaching one now" — and the first used to be reachable
        only through a branch the second could skip. A container sampled just
        after an OOM kill reports a near-zero *current* against a peak of 91.6%,
        so the `elif` meant the restart erased the very history
        `max_over_time` was queried to recover. Taking the maximum is what makes
        the finding survive the restart that proves it.
        """
        peak = payload.get("memory_peak_percent")
        current = payload.get("memory_utilisation_percent")

        observed = [value for value in (peak, current) if value is not None]
        if not observed:
            return []
        worst = max(observed)

        # Peak explains a termination that already happened; current describes
        # one still building. Say which, so the wording matches the evidence.
        historical = peak is not None and peak >= worst and (current is None or current < worst)

        if worst >= MEMORY_AT_LIMIT_PERCENT:
            detail = (
                f"Memory usage reached {peak}% of the container limit, so the "
                f"limit — not a leak elsewhere — is what terminated it."
                if historical
                else f"Memory usage is at {current}% of the container limit."
            )
            return [
                Signal.create(
                    SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT,
                    Severity.CRITICAL,
                    detail,
                    target,
                    evidence,
                    {"peak_percent": peak, "current_percent": current},
                )
            ]

        if worst >= MEMORY_NEAR_LIMIT_PERCENT:
            detail = (
                f"Memory usage peaked at {peak}% of the container limit within "
                f"the metrics window, even though it reads {current}% now."
                if historical
                else f"Memory usage is at {current}% of the container limit."
            )
            return [
                Signal.create(
                    SignalType.METRICS_MEMORY_NEAR_LIMIT,
                    Severity.HIGH,
                    detail,
                    target,
                    evidence,
                    {"current_percent": current, "peak_percent": peak},
                )
            ]

        return []

    def _cpu(self, payload: dict[str, Any], target, evidence) -> list[Signal]:
        ratio = payload.get("cpu_throttled_ratio")
        if ratio is None or ratio < CPU_THROTTLE_RATIO:
            return []

        return [
            Signal.create(
                SignalType.METRICS_CPU_THROTTLED,
                Severity.HIGH,
                f"{round(ratio * 100)}% of CPU periods are being throttled, which "
                f"slows request handling and can fail timing-sensitive probes.",
                target,
                evidence,
                {"throttled_ratio": ratio, "cpu_cores": payload.get("cpu_cores")},
            )
        ]

    def _restarts(self, payload: dict[str, Any], target, evidence) -> list[Signal]:
        recent = payload.get("restarts_in_window")
        if recent is None or recent < 2:
            return []

        return [
            Signal.create(
                SignalType.METRICS_RESTART_RATE,
                Severity.HIGH,
                f"The container restarted {int(recent)} time(s) in the metrics "
                f"window, so this is a repeating failure rather than a one-off.",
                target,
                evidence,
                {
                    "restarts_in_window": recent,
                    "restarts_total": payload.get("restarts_total"),
                },
            )
        ]


class NodeMetricsRule:
    id = "observability.node_metrics"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(PrometheusKind.NODE_METRICS):
            payload = entry["data"]
            memory = payload.get("memory_committed_percent")
            cpu = payload.get("cpu_committed_percent")

            over = [
                (name, value)
                for name, value in (("memory", memory), ("CPU", cpu))
                if value is not None and value >= NODE_COMMITTED_PERCENT
            ]
            if not over:
                continue

            described = ", ".join(f"{name} at {value}%" for name, value in over)
            signals.append(
                Signal.create(
                    SignalType.METRICS_NODE_OVERCOMMITTED,
                    Severity.HIGH,
                    f"Node requests are committed beyond headroom ({described}), so "
                    f"the scheduler has nothing left to place against.",
                    _ref(entry),
                    _provenance(entry),
                    {"memory_committed_percent": memory, "cpu_committed_percent": cpu},
                )
            )

        return signals


class HistoricalLogRule:
    id = "observability.historical_logs"

    def extract(self, data: AnalysisInput) -> Sequence[Signal]:
        signals = []

        for entry in data.deep(LokiKind.POD_LOGS):
            payload = entry["data"]
            matched = payload.get("matched_lines", 0)
            if matched < HISTORICAL_ERROR_LINES:
                continue

            sample = [item.get("line", "") for item in payload.get("entries", [])[:5]]
            signals.append(
                Signal.create(
                    SignalType.LOGS_HISTORICAL_ERRORS,
                    Severity.MEDIUM,
                    f"{matched} error-level log lines were retained for this pod, "
                    f"covering container instances that kubectl can no longer serve.",
                    _ref(entry),
                    _provenance(entry),
                    {"matched_lines": matched, "sample_lines": sample},
                )
            )

        return signals


OBSERVABILITY_SIGNAL_RULES = (
    PodMetricsRule(),
    NodeMetricsRule(),
    HistoricalLogRule(),
)
