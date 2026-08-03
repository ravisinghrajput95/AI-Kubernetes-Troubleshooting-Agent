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


# --- Tier-2 detection breadth (docs/QA_AUDIT_2026-08-03.md) -------------------


def event(reason, message="", obj="Pod/web-0"):
    return investigation(
        events={
            "findings": [{"reason": reason, "message": message, "object": obj, "namespace": "prod"}]
        }
    )


class TestVolumeAttachIsNotAnUnboundClaim:
    """A bound volume that will not attach is a different fault from a claim
    that was never provisioned, and the fixes have nothing in common: one waits
    for a detach from another node, the other creates storage. `FailedAttachVolume`
    previously fell through to a generic LOW warning, so the most common form —
    a ReadWriteOnce disk still held by a pod elsewhere — had no finding at all.
    """

    def test_a_failed_attach_is_its_own_signal(self):
        result = ENGINE.analyze(
            event("FailedAttachVolume", "Multi-Attach error for volume pvc-abc123")
        )

        signal = result.by_type(SignalType.STORAGE_ATTACH_FAILURE)[0]
        assert signal.severity is Severity.HIGH

    def test_it_is_not_reported_as_an_unbound_claim(self):
        result = ENGINE.analyze(event("FailedAttachVolume", "Multi-Attach error"))

        assert not result.by_type(SignalType.STORAGE_PVC_UNBOUND)

    def test_it_is_not_swallowed_as_a_generic_warning(self):
        """What it used to be, and the reason it was invisible."""
        result = ENGINE.analyze(event("FailedAttachVolume", "Multi-Attach error"))

        assert not result.by_type(SignalType.EVENT_WARNING)

    def test_it_raises_its_own_hypothesis(self):
        result = ENGINE.analyze(event("FailedAttachVolume", "Multi-Attach error"))

        assert any(h.id == "storage.volume_attach_blocked" for h in result.hypotheses)

    def test_a_mount_failure_stays_a_mount_failure(self):
        """The neighbouring reason must not be absorbed by the new one."""
        result = ENGINE.analyze(event("FailedMount", "MountVolume.SetUp failed"))

        assert result.by_type(SignalType.EVENT_MOUNT_FAILURE)
        assert not result.by_type(SignalType.STORAGE_ATTACH_FAILURE)


class TestTheNewPodStatusesReachHypotheses:
    """A signal that no hypothesis consumes changes no diagnosis."""

    def pods(self, status):
        return investigation(
            pods={"problematic_pods": [{"name": "web-0", "namespace": "prod", "status": status}]}
        )

    def test_a_configuration_error_raises_the_configuration_hypothesis(self):
        """Previously reachable only after a playbook round, so a pod plainly
        reporting `CreateContainerConfigError` produced no configuration
        hypothesis on the first pass."""
        result = ENGINE.analyze(self.pods("CreateContainerConfigError"))

        assert any(h.id == "workload.missing_configuration" for h in result.hypotheses)

    def test_an_evicted_pod_raises_the_node_hypothesis(self):
        """The node condition that caused the eviction may already have
        cleared, leaving the eviction as the only remaining evidence."""
        result = ENGINE.analyze(self.pods("Evicted"))

        assert any(h.id == "node.unhealthy" for h in result.hypotheses)

    def test_a_pod_that_is_never_ready_raises_the_probe_hypothesis(self):
        """Kubernetes expires events after an hour, so a permanently failing
        probe stops producing them and the pod condition is the only lasting
        trace."""
        result = ENGINE.analyze(self.pods("NotReady"))

        assert any(h.id == "probe.failing" for h in result.hypotheses)

    def test_an_out_of_memory_pod_still_raises_its_own_hypothesis(self):
        result = ENGINE.analyze(self.pods("OOMKilled"))

        assert any(h.id == "workload.out_of_memory" for h in result.hypotheses)
