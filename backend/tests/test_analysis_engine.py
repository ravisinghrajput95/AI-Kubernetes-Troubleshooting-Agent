from app.analysis.engine import AnalysisEngine
from app.analysis.models import Severity, SignalType

ENGINE = AnalysisEngine()


def investigation(**sections):
    base = {
        "context": "test-cluster",
        "health": {"status": "issues_found"},
        "evidence": [
            {"id": f"{kind}:cluster/_cluster/test", "kind": kind}
            for kind in (
                "k8s.pods",
                "k8s.pods.logs",
                "k8s.events",
                "k8s.deployments",
                "k8s.network",
                "k8s.nodes",
                "k8s.storage",
                "k8s.workloads",
            )
        ],
    }
    base.update(sections)
    return base


def test_pod_status_becomes_a_signal_with_provenance():
    result = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            }
        )
    )

    signal = result.by_type(SignalType.POD_CRASH_LOOP)[0]
    assert signal.id == "pod.crash_loop:pod/prod/web-0"
    assert signal.severity is Severity.CRITICAL
    assert signal.evidence_ids == ("k8s.pods:cluster/_cluster/test",)


def test_signals_are_deduplicated_by_deterministic_id():
    result = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"},
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"},
                ]
            }
        )
    )
    assert len(result.by_type(SignalType.POD_CRASH_LOOP)) == 1


def test_oom_is_detected_from_log_content():
    result = ENGINE.analyze(
        investigation(
            logs={
                "logs": [
                    {
                        "name": "api-1",
                        "namespace": "prod",
                        "relevant_lines": ["fatal: container was OOMKilled after 3s"],
                    }
                ]
            }
        )
    )

    assert result.by_type(SignalType.LOGS_OOM_PATTERN)
    assert result.top_hypothesis.id == "workload.out_of_memory"


def test_crashloop_with_logs_produces_startup_failure_hypothesis():
    result = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            },
            logs={
                "logs": [
                    {
                        "name": "web-0",
                        "namespace": "prod",
                        "relevant_lines": ["error: missing environment variable DB_HOST"],
                    }
                ]
            },
        )
    )

    hypothesis = result.hypothesis("workload.application_startup_failure")
    assert hypothesis is not None
    assert "logs.error_pattern:pod/prod/web-0" in hypothesis.supporting_signal_ids
    assert hypothesis.missing_evidence


def test_refuting_signal_lowers_hypothesis_confidence():
    crash_only = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            }
        )
    ).hypothesis("workload.application_startup_failure")

    with_refutation = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"},
                    {"name": "web-1", "namespace": "prod", "status": "ImagePullBackOff"},
                ]
            }
        )
    ).hypothesis("workload.application_startup_failure")

    assert with_refutation.confidence < crash_only.confidence
    assert with_refutation.refuting_signal_ids


def test_pending_pod_with_unbound_pvc_ranks_scheduling_hypothesis():
    result = ENGINE.analyze(
        investigation(
            pods={"problematic_pods": [{"name": "db-0", "namespace": "prod", "status": "Pending"}]},
            events={
                "findings": [
                    {
                        "namespace": "prod",
                        "reason": "FailedScheduling",
                        "object": "Pod/db-0",
                        "message": "0/3 nodes are available",
                    }
                ]
            },
            storage={
                "findings": [
                    {
                        "namespace": "prod",
                        "name": "db-data",
                        "phase": "Pending",
                        "issue": "PersistentVolumeClaim is not bound",
                    }
                ]
            },
        )
    )

    scheduling = result.hypothesis("scheduling.unschedulable")
    assert scheduling is not None
    assert "storage.pvc_unbound:persistentvolumeclaim/prod/db-data" in (
        scheduling.supporting_signal_ids
    )


def test_service_without_endpoints_is_critical():
    result = ENGINE.analyze(
        investigation(
            network={
                "findings": [
                    {
                        "namespace": "prod",
                        "service": "api",
                        "issue": "Service has no ready endpoints; selector may not match any pods",
                    }
                ]
            }
        )
    )

    signal = result.by_type(SignalType.NETWORK_NO_ENDPOINTS)[0]
    assert signal.severity is Severity.CRITICAL
    assert result.hypothesis("network.service_without_endpoints") is not None


def test_node_conditions_map_to_distinct_signal_types():
    result = ENGINE.analyze(
        investigation(
            nodes={
                "findings": [
                    {"node": "node-1", "type": "Ready", "status": "False", "reason": "KubeletDown"},
                    {"node": "node-2", "type": "MemoryPressure", "status": "True", "reason": "Low"},
                ]
            }
        )
    )

    assert result.by_type(SignalType.NODE_NOT_READY)
    assert result.by_type(SignalType.NODE_PRESSURE)
    assert result.hypothesis("node.unhealthy") is not None


def test_healthy_investigation_produces_nothing_to_explain():
    result = ENGINE.analyze(investigation(health={"status": "healthy"}))
    assert result.signals == ()
    assert result.hypotheses == ()
    assert result.top_hypothesis is None


def test_a_failing_rule_does_not_stop_other_rules():
    class ExplodingRule:
        id = "boom"

        def extract(self, data):
            raise RuntimeError("rule bug")

    engine = AnalysisEngine(signal_rules=[ExplodingRule(), *AnalysisEngine().signal_rules])
    result = engine.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            }
        )
    )
    assert result.by_type(SignalType.POD_CRASH_LOOP)


def test_missing_evidence_index_still_yields_honest_provenance():
    result = AnalysisEngine().analyze(
        {
            "health": {"status": "issues_found"},
            "pods": {
                "problematic_pods": [{"name": "web-0", "namespace": "prod", "status": "OOMKilled"}]
            },
        }
    )
    signal = result.by_type(SignalType.POD_OOM_KILLED)[0]
    assert signal.evidence_ids == ("investigation.pods",)


def test_hypotheses_are_ranked_by_severity_then_confidence():
    result = ENGINE.analyze(
        investigation(
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "OOMKilled"},
                ]
            },
            events={
                "findings": [
                    {
                        "namespace": "prod",
                        "reason": "Unhealthy",
                        "object": "Pod/web-0",
                        "message": "probe failed",
                    }
                ]
            },
        )
    )

    ids = [item.id for item in result.hypotheses]
    assert ids[0] == "workload.out_of_memory"
    assert "probe.failing" in ids
