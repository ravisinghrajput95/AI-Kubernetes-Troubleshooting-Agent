"""Signals that only exist once a playbook has collected targeted evidence."""

from app.analysis.engine import AnalysisEngine
from app.analysis.models import Severity, SignalType

ENGINE = AnalysisEngine()

POD_TARGET = {"kind": "Pod", "name": "web-0", "namespace": "prod"}


def deep(kind, data, target=None, evidence_id=None):
    return {
        kind: [
            {
                "id": evidence_id or f"{kind}:pod/prod/web-0",
                "target": target or POD_TARGET,
                "status": "ok",
                "data": data,
            }
        ]
    }


def investigation(deep_evidence, **sections):
    base = {
        "context": "test-cluster",
        "health": {"status": "issues_found"},
        "evidence": [],
        "deep_evidence": deep_evidence,
    }
    base.update(sections)
    return base


def test_exit_code_137_confirms_an_oom_kill():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "containers": [
                        {
                            "name": "web",
                            "restart_count": 7,
                            "limits": {"cpu": "1"},
                            "last_state": {"reason": "OOMKilled", "exit_code": 137},
                        }
                    ]
                },
            )
        )
    )

    signal = result.by_type(SignalType.CONTAINER_OOM_EXIT)[0]
    assert signal.severity is Severity.CRITICAL
    assert "137" in signal.summary
    assert signal.evidence_ids == ("k8s.pod.spec:pod/prod/web-0",)


def test_oom_exit_code_promotes_the_out_of_memory_hypothesis():
    baseline = ENGINE.analyze(
        {
            "health": {"status": "issues_found"},
            "pods": {
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            },
        }
    )
    assert baseline.top_hypothesis.id == "workload.application_startup_failure"

    deepened = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "containers": [
                        {
                            "name": "web",
                            "restart_count": 7,
                            "limits": {},
                            "last_state": {"reason": "OOMKilled", "exit_code": 137},
                        }
                    ]
                },
            ),
            pods={
                "problematic_pods": [
                    {"name": "web-0", "namespace": "prod", "status": "CrashLoopBackOff"}
                ]
            },
        )
    )

    assert deepened.top_hypothesis.id == "workload.out_of_memory"
    # The startup-failure hypothesis is now actively refuted.
    startup = deepened.hypothesis("workload.application_startup_failure")
    assert startup.refuting_signal_ids


def test_nonzero_exit_code_is_reported_without_claiming_oom():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "containers": [
                        {"name": "web", "last_state": {"exit_code": 1, "reason": "Error"}}
                    ]
                },
            )
        )
    )

    assert result.by_type(SignalType.CONTAINER_NONZERO_EXIT)
    assert not result.by_type(SignalType.CONTAINER_OOM_EXIT)


def test_clean_exit_codes_produce_no_signal():
    for exit_code in (0, 143):
        result = ENGINE.analyze(
            investigation(
                deep(
                    "k8s.pod.spec",
                    {"containers": [{"name": "web", "last_state": {"exit_code": exit_code}}]},
                )
            )
        )
        assert not result.by_type(SignalType.CONTAINER_NONZERO_EXIT)


def test_missing_config_key_is_critical_and_creates_a_hypothesis():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.config_refs",
                {
                    "references": [
                        {
                            "kind": "ConfigMap",
                            "name": "web-config",
                            "exists": True,
                            "available_keys": ["LOG_LEVEL"],
                            "required_keys": ["DB_HOST"],
                            "missing_keys": ["DB_HOST"],
                        }
                    ]
                },
            )
        )
    )

    signal = result.by_type(SignalType.CONFIG_KEY_MISSING)[0]
    assert "DB_HOST" in signal.summary
    assert result.top_hypothesis.id == "workload.missing_configuration"


def test_absent_configmap_is_reported_separately_from_a_missing_key():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.config_refs",
                {"references": [{"kind": "Secret", "name": "db-creds", "exists": False}]},
            )
        )
    )

    assert result.by_type(SignalType.CONFIG_REFERENCE_MISSING)
    assert not result.by_type(SignalType.CONFIG_KEY_MISSING)


def test_aggressive_liveness_probe_is_flagged():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "containers": [
                        {
                            "name": "web",
                            "probes": {
                                "liveness": {"initial_delay_seconds": 1, "handler": "httpGet"}
                            },
                        }
                    ]
                },
            )
        )
    )
    assert result.by_type(SignalType.PROBE_AGGRESSIVE)


def test_a_startup_probe_excuses_an_early_liveness_probe():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "containers": [
                        {
                            "name": "web",
                            "probes": {
                                "liveness": {"initial_delay_seconds": 1},
                                "startup": {"failure_threshold": 30},
                            },
                        }
                    ]
                },
            )
        )
    )
    assert not result.by_type(SignalType.PROBE_AGGRESSIVE)


def test_scheduling_message_distinguishes_capacity_from_taints():
    capacity = ENGINE.analyze(
        investigation(
            deep(
                "k8s.resource.events",
                {
                    "events": [
                        {"reason": "FailedScheduling", "message": "0/3 nodes: Insufficient cpu"}
                    ]
                },
            )
        )
    )
    assert capacity.by_type(SignalType.SCHEDULING_INSUFFICIENT_RESOURCES)

    taints = ENGINE.analyze(
        investigation(
            deep(
                "k8s.resource.events",
                {"events": [{"reason": "FailedScheduling", "message": "node had taint {a: b}"}]},
            )
        )
    )
    assert taints.by_type(SignalType.SCHEDULING_TAINT_BLOCKED)


def test_image_pull_message_distinguishes_auth_from_missing_image():
    unauthorized = ENGINE.analyze(
        investigation(
            deep(
                "k8s.resource.events",
                {"events": [{"reason": "Failed", "message": "pull access denied: unauthorized"}]},
            )
        )
    )
    assert unauthorized.by_type(SignalType.IMAGE_PULL_UNAUTHORIZED)

    missing = ENGINE.analyze(
        investigation(
            deep(
                "k8s.resource.events",
                {"events": [{"reason": "Failed", "message": "manifest unknown: not found"}]},
            )
        )
    )
    assert missing.by_type(SignalType.IMAGE_NOT_FOUND)


def test_missing_pull_secret_only_reported_when_the_pull_is_failing():
    healthy = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "image_pull_secrets": [],
                    "containers": [{"name": "web", "current_state": {"reason": ""}}],
                },
            )
        )
    )
    assert not healthy.by_type(SignalType.IMAGE_NO_PULL_SECRET)

    failing = ENGINE.analyze(
        investigation(
            deep(
                "k8s.pod.spec",
                {
                    "image_pull_secrets": [],
                    "containers": [
                        {"name": "web", "current_state": {"reason": "ImagePullBackOff"}}
                    ],
                },
            )
        )
    )
    assert failing.by_type(SignalType.IMAGE_NO_PULL_SECRET)


def test_exhausted_quota_is_detected_across_unit_suffixes():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.quotas",
                {
                    "items": [
                        {
                            "name": "team-quota",
                            "hard": {"requests.memory": "10Gi", "pods": "20"},
                            "used": {"requests.memory": "10Gi", "pods": "3"},
                        }
                    ]
                },
                target={"kind": "Namespace", "name": "prod", "namespace": "prod"},
            )
        )
    )

    signal = result.by_type(SignalType.QUOTA_EXCEEDED)[0]
    assert signal.attributes["exhausted"] == ["requests.memory"]
    assert result.hypothesis("scheduling.quota_exhausted") is not None


def test_absent_default_storage_class_is_detected():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.storageclasses",
                {
                    "items": [
                        {"name": "slow", "is_default": False, "volume_binding_mode": "Immediate"}
                    ]
                },
                target={"kind": "Cluster", "name": "test", "namespace": None},
            )
        )
    )

    assert result.by_type(SignalType.STORAGE_NO_DEFAULT_CLASS)
    assert result.hypothesis("storage.no_default_storage_class") is not None


def test_default_storage_class_present_produces_no_signal():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.storageclasses",
                {"items": [{"name": "standard", "is_default": True}]},
                target={"kind": "Cluster", "name": "test", "namespace": None},
            )
        )
    )
    assert not result.by_type(SignalType.STORAGE_NO_DEFAULT_CLASS)


def test_default_deny_network_policy_is_detected():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.networkpolicies",
                {"items": [{"name": "deny-all", "namespace": "prod", "denies_all_ingress": True}]},
                target={"kind": "Namespace", "name": "prod", "namespace": "prod"},
            )
        )
    )

    assert result.by_type(SignalType.NETWORK_POLICY_DENIES_ALL)
    assert result.hypothesis("network.ingress_denied_by_policy") is not None


def test_unready_dns_pods_are_critical():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.dns.workload",
                {"pods": [{"name": "coredns-1", "ready": False}], "ready_count": 0},
                target={"kind": "Cluster", "name": "test", "namespace": None},
            )
        )
    )

    assert result.by_type(SignalType.DNS_WORKLOAD_UNHEALTHY)
    assert result.hypothesis("network.dns_workload_degraded") is not None


def test_healthy_dns_produces_no_signal():
    result = ENGINE.analyze(
        investigation(
            deep(
                "k8s.dns.workload",
                {"pods": [{"name": "coredns-1", "ready": True}], "ready_count": 1},
                target={"kind": "Cluster", "name": "test", "namespace": None},
            )
        )
    )
    assert not result.by_type(SignalType.DNS_WORKLOAD_UNHEALTHY)


def test_deep_signals_are_ignored_when_no_deep_evidence_exists():
    result = ENGINE.analyze({"health": {"status": "healthy"}})
    assert result.signals == ()


# --- Image-pull events, and what is *not* one ---------------------------------
#
# Every message below is verbatim from a real cluster unless marked otherwise;
# they were captured during the audit in `docs/QA_AUDIT_2026-08-03.md`. The
# defect these pin was found because the platform reported a missing ConfigMap
# as a missing container image — the rule accepted `reason: Failed` (which the
# kubelet emits for config, mount and runtime errors as well as image pulls)
# and then substring-matched "not found" in its message.

CONFIGMAP_MISSING = 'Error: configmap "notifier-config" not found'
OOM_AT_INIT = (
    "Error: failed to create containerd task: failed to create shim task: "
    "OCI runtime create failed: runc create failed: unable to start container "
    "process: container init was OOM-killed (memory limit too low?)"
)
REGISTRY_UNREACHABLE = (
    'Failed to pull image "registry.invalid/does-not-exist:9.9.9": failed to pull '
    'and unpack image "registry.invalid/does-not-exist:9.9.9": failed to resolve '
    'reference "registry.invalid/does-not-exist:9.9.9": failed to do request: Head '
    '"https://registry.invalid/v2/does-not-exist/manifests/9.9.9": dial tcp: lookup '
    "registry.invalid on 192.168.65.254:53: no such host"
)
# Representative containerd phrasing for a tag that genuinely does not exist,
# rather than a registry that could not be reached.
MANIFEST_MISSING = (
    'Failed to pull image "docker.io/library/nginx:9.9.9": rpc error: code = NotFound '
    'desc = failed to pull and unpack image "docker.io/library/nginx:9.9.9": failed to '
    'resolve reference "docker.io/library/nginx:9.9.9": docker.io/library/nginx:9.9.9: '
    "not found"
)


def events(*messages, reason="Failed"):
    return deep(
        "k8s.resource.events",
        {"events": [{"reason": reason, "message": m} for m in messages]},
    )


def test_a_missing_configmap_is_not_an_image_problem():
    """The defect, verbatim. `Failed` + "not found" was enough to emit a
    CRITICAL "image does not exist in the registry" over a message that says
    the opposite, sending an operator to the registry when the fix is to
    create a ConfigMap."""
    result = ENGINE.analyze(investigation(events(CONFIGMAP_MISSING)))

    assert not result.by_type(SignalType.IMAGE_NOT_FOUND)
    assert not result.by_type(SignalType.IMAGE_PULL_UNAUTHORIZED)


def test_a_runtime_failure_is_not_an_image_problem():
    result = ENGINE.analyze(investigation(events(OOM_AT_INIT)))

    assert not result.by_type(SignalType.IMAGE_NOT_FOUND)


def test_a_missing_tag_is_still_reported():
    """The guard that keeps the fix from being a silent over-correction: an
    over-strict gate does not fail loudly, it just stops diagnosing."""
    result = ENGINE.analyze(investigation(events(MANIFEST_MISSING)))

    signal = result.by_type(SignalType.IMAGE_NOT_FOUND)[0]
    assert signal.severity is Severity.CRITICAL


def test_an_unreachable_registry_is_not_reported_as_a_missing_image():
    """Real, and a distinction worth keeping: DNS did not resolve, so nothing
    was learned about whether the image exists. Claiming it does not would be
    the same overreach in a subtler form."""
    result = ENGINE.analyze(investigation(events(REGISTRY_UNREACHABLE)))

    assert not result.by_type(SignalType.IMAGE_NOT_FOUND)


def test_an_unambiguous_image_reason_needs_no_phrasing():
    """`FailedPull` names the image path on its own, so a terse message must
    still classify."""
    result = ENGINE.analyze(investigation(events("manifest unknown", reason="FailedPull")))

    assert result.by_type(SignalType.IMAGE_NOT_FOUND)


def test_an_unauthorized_pull_is_still_reported():
    result = ENGINE.analyze(
        investigation(events("Failed to pull image: pull access denied, 403 unauthorized"))
    )

    assert result.by_type(SignalType.IMAGE_PULL_UNAUTHORIZED)


def test_an_unrelated_403_is_not_an_unauthorized_pull():
    """The gate covers the unauthorized branch too — "403" is at least as easy
    for an unrelated `Failed` event to contain as "not found"."""
    result = ENGINE.analyze(
        investigation(events("Error: failed to mount volume: server returned 403"))
    )

    assert not result.by_type(SignalType.IMAGE_PULL_UNAUTHORIZED)
