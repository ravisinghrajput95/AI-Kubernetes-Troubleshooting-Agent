"""A log line is an error pattern only if it names a failure.

Found by reading a console sweep against a live cluster: the checkout pod
logged `starting checkout service` then `FATAL: config key DB_HOST is not set`.
No keyword matched either line — "fatal" was not a keyword — so the collector
fell back to the log's tail under the key that means "failure lines", and the
fleet page read `logs report: "starting checkout service" (+1 more)` as a HIGH
error pattern, on all three routes to that cluster.

Driven from the provider's text through the collector into the analysis
engine, because the defect was the collector and the rule disagreeing about
what the key meant.
"""

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from app.kubernetes.logs_collector import LogsCollector
from app.providers.base import ProviderResult

POD = {"name": "checkout-5b5fd56dbf-4cnmv", "namespace": "payments", "status": "CrashLoopBackOff"}


def analysed(text: str):
    logs = LogsCollector().analyse([POD], [ProviderResult(success=True, text=text)])
    return logs, AnalysisEngine().analyze({"logs": logs})


def test_the_line_that_names_the_failure_is_quoted():
    logs, result = analysed("starting checkout service\nFATAL: config key DB_HOST is not set\n")

    (signal,) = result.by_type(SignalType.LOGS_ERROR_PATTERN)
    assert '"FATAL: config key DB_HOST is not set"' in signal.summary
    assert "starting checkout service" not in signal.summary
    assert logs["logs"][0]["last_lines"][-1] == "FATAL: config key DB_HOST is not set"


def test_ordinary_output_is_not_an_error_pattern():
    logs, result = analysed("starting checkout service\nlistening on :8080\n")

    assert not result.by_type(SignalType.LOGS_ERROR_PATTERN)
    # The tail is still there for a reader, just not as a finding.
    assert logs["logs"][0]["last_lines"] == ["starting checkout service", "listening on :8080"]


def test_an_oom_marker_is_seen_without_another_keyword_beside_it():
    _, result = analysed("worker 3 ready\nruntime: out of memory\n")

    assert result.by_type(SignalType.LOGS_OOM_PATTERN)
