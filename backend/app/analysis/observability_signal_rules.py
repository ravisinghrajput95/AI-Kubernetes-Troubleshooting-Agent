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
MEMORY_AT_LIMIT_PERCENT = 98.0
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
        peak = payload.get("memory_peak_percent")
        current = payload.get("memory_utilisation_percent")
        signals = []

        if peak is not None and peak >= MEMORY_AT_LIMIT_PERCENT:
            signals.append(
                Signal.create(
                    SignalType.METRICS_MEMORY_PEAKED_AT_LIMIT,
                    Severity.CRITICAL,
                    f"Memory usage reached {peak}% of the container limit, so the "
                    f"limit — not a leak elsewhere — is what terminated it.",
                    target,
                    evidence,
                    {"peak_percent": peak, "current_percent": current},
                )
            )
        elif current is not None and current >= MEMORY_NEAR_LIMIT_PERCENT:
            signals.append(
                Signal.create(
                    SignalType.METRICS_MEMORY_NEAR_LIMIT,
                    Severity.HIGH,
                    f"Memory usage is at {current}% of the container limit.",
                    target,
                    evidence,
                    {"current_percent": current, "peak_percent": peak},
                )
            )

        return signals

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
