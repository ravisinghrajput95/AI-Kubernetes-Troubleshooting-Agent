"""A pod that restarted is not a pod the kubelet says is crash-looping.

Minutes after the kind node came back, the agent pod, metrics-server and the
local-path provisioner were reported "in CrashLoopBackOff" while `kubectl get
pods` printed all three Running and Ready — their containers had exited once
while their dependencies were still coming up. The whole-cluster root cause
became "Application fails on startup and restarts repeatedly" about the
platform's own agent pod.

The finding stands: a container that exited twice in the last few minutes is
worth looking at, and `_stable` deliberately keeps it for ten minutes. What
cannot stand is saying it in the present tense about a container that is up.
"""

from datetime import UTC, datetime, timedelta

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from app.kubernetes.pod_inspector import PodInspector
from app.providers.base import ProviderResult

NOW = datetime(2026, 9, 16, 5, 34, tzinfo=UTC)


def pod(started_ago: timedelta, ready: bool, waiting: str | None = None):
    state = (
        {"waiting": {"reason": waiting}}
        if waiting
        else {"running": {"startedAt": (NOW - started_ago).isoformat()}}
    )
    return {
        "metadata": {
            "name": "k8s-ops-agent-cff5c446f-bfvf9",
            "namespace": "k8s-ops-agent",
            "creationTimestamp": NOW.isoformat(),
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [
                {
                    "name": "agent",
                    "ready": ready,
                    "restartCount": 3,
                    "state": state,
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
        },
    }


def analysed(item):
    pods = PodInspector().analyse([ProviderResult(success=True, data={"items": [item]})], None)
    return pods, AnalysisEngine().analyze({"pods": pods})


def test_a_pod_running_again_is_not_said_to_be_in_crashloopbackoff():
    pods, result = analysed(pod(timedelta(minutes=3), ready=True))

    entry = pods["problematic_pods"][0]
    assert entry["status"] == "CrashLoopBackOff"  # still worth investigating
    assert entry["reported_now"] is False
    (signal,) = result.by_type(SignalType.POD_CRASH_LOOP)
    assert "is in CrashLoopBackOff" not in signal.summary
    assert "restarted recently" in signal.summary
    assert "too recently to rule out CrashLoopBackOff" in signal.summary


def test_a_pod_the_kubelet_is_backing_off_is_still_said_plainly():
    pods, result = analysed(pod(timedelta(minutes=3), ready=False, waiting="CrashLoopBackOff"))

    assert pods["problematic_pods"][0]["reported_now"] is True
    (signal,) = result.by_type(SignalType.POD_CRASH_LOOP)
    assert "is in CrashLoopBackOff." in signal.summary


def test_a_pod_up_past_the_backoff_window_is_not_reported_at_all():
    pods, _ = analysed(pod(timedelta(minutes=30), ready=True))

    assert pods["problematic_pods"] == []
