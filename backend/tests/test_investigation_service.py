"""End-to-end checks of the investigation pipeline against a fake cluster.

The fake executor subclasses the real one, so these tests also assert that
every built-in collector issues commands permitted by the read-only policy.
"""

import json

from app.kubernetes.kubectl_executor import KubectlExecutor, KubectlResult
from app.providers.local_kubectl import LocalKubectlProvider
from app.services.investigation_runner import collection_failure
from app.services.investigation_service import InvestigationService

LEAKED_PASSWORD = "hunter2"
LEAKED_TOKEN = "abc123xyz"

PODS = {
    "items": [
        {
            "metadata": {"name": "web-0", "namespace": "prod"},
            "spec": {
                "nodeName": "node-1",
                "containers": [
                    {
                        "name": "web",
                        "image": "registry.example.com/web:1.4.2",
                        "resources": {"limits": {"cpu": "1"}, "requests": {"cpu": "500m"}},
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
            },
        }
    ]
}

NODES = {
    "items": [
        {
            "metadata": {"name": "node-1"},
            "spec": {},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "capacity": {"cpu": "4"},
                "allocatable": {"cpu": "3800m"},
            },
        }
    ]
}

# A healthy cluster DNS service, so the fixture does not emit a spurious
# cluster-wide DNS finding that would mask the pod failure under test.
SERVICES = {
    "items": [
        {
            "metadata": {"name": "coredns", "namespace": "kube-system"},
            "spec": {"type": "ClusterIP", "selector": {"k8s-app": "kube-dns"}},
        }
    ]
}

ENDPOINTS = {
    "items": [
        {
            "metadata": {"name": "coredns", "namespace": "kube-system"},
            "subsets": [{"addresses": [{"ip": "10.0.0.10"}, {"ip": "10.0.0.11"}]}],
        }
    ]
}

POD_LOG_OUTPUT = (
    f"2026-07-26T10:00:00Z ERROR auth failed password={LEAKED_PASSWORD}\n"
    f"2026-07-26T10:00:01Z ERROR retry with token={LEAKED_TOKEN}\n"
)

PREVIOUS_LOG_OUTPUT = (
    "2026-07-26T09:59:58Z INFO starting web\n"
    "2026-07-26T09:59:59Z FATAL config key DB_HOST is not set\n"
)

# Full detail for the targeted `get pod <name> -o json` a playbook issues.
# Deliberately encodes a confirmable OOM kill, an aggressive liveness probe,
# and a ConfigMap reference whose key does not exist.
POD_DETAIL = {
    "metadata": {
        "name": "web-0",
        "namespace": "prod",
        "labels": {"app": "web"},
        # Pods are normally owned by a ReplicaSet, which is owned by a Deployment.
        # Remediation must target the Deployment, not the pod.
        "ownerReferences": [{"kind": "ReplicaSet", "name": "web-5d4f8c9b7", "controller": True}],
    },
    "spec": {
        "nodeName": "node-1",
        "serviceAccountName": "default",
        "volumes": [{"name": "cfg", "configMap": {"name": "web-config"}}],
        "containers": [
            {
                "name": "web",
                "image": "registry.example.com/web:1.4.2",
                "resources": {"limits": {"cpu": "1"}, "requests": {"cpu": "500m"}},
                "livenessProbe": {
                    "httpGet": {"path": "/healthz", "port": 8080},
                    "initialDelaySeconds": 2,
                    "periodSeconds": 5,
                },
                "env": [
                    {
                        "name": "DB_HOST",
                        "valueFrom": {"configMapKeyRef": {"name": "web-config", "key": "DB_HOST"}},
                    }
                ],
            }
        ],
    },
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {
                "name": "web",
                "ready": False,
                "restartCount": 7,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {
                    "terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "finishedAt": "2026-07-26T09:59:59Z",
                    }
                },
            }
        ],
    },
}

# The referenced ConfigMap exists but lacks the key the container reads.
CONFIGMAP = {
    "metadata": {"name": "web-config", "namespace": "prod"},
    "data": {"LOG_LEVEL": "info"},
}

POD_SCOPED_EVENTS = {
    "items": [
        {
            "reason": "BackOff",
            "type": "Warning",
            "message": "Back-off restarting failed container web",
            "count": 12,
            "lastTimestamp": "2026-07-26T10:00:00Z",
        }
    ]
}


class FakeKubectl(KubectlExecutor):
    """Read-only fake that answers the built-in collectors' commands."""

    def __init__(self, failing_resources: set[str] | None = None) -> None:
        super().__init__(context="test-cluster")
        self.failing_resources = failing_resources or set()

    def run(self, args: list[str], parse_json: bool = False) -> KubectlResult:
        from app.kubernetes.command_policy import assert_read_only

        assert_read_only(args)
        command = ["kubectl", "--context", "test-cluster", *args]
        with self._audit_lock:
            self.executed_commands.append(" ".join(command))

        verb = args[0]
        resource = args[1] if len(args) > 1 else ""
        # A targeted read names the object: `get pod web-0 ...`.
        named = args[2] if len(args) > 2 and not args[2].startswith("-") else ""

        if resource in self.failing_resources:
            return KubectlResult(command, False, "", "Error from server (Forbidden)", 1)

        if verb == "logs":
            if "--previous" in args:
                return KubectlResult(command, True, PREVIOUS_LOG_OUTPUT, "", 0)
            return KubectlResult(command, True, POD_LOG_OUTPUT, "", 0)

        if verb == "top":
            output = (
                "node-1   100m   5%   1024Mi   40%\n"
                if resource == "nodes"
                else "prod   web-0   100m   256Mi\n"
            )
            return KubectlResult(command, True, output, "", 0)

        if verb == "describe" and resource == "secret":
            return KubectlResult(command, False, "", f'secrets "{named}" not found', 1)

        payload: dict = {"items": []}
        if resource == "pod" and named:
            payload = POD_DETAIL
        elif resource in {"pods", "pod"}:
            payload = PODS
        elif resource == "configmap" and named:
            payload = CONFIGMAP
        elif resource == "events" and any(arg.startswith("--field-selector") for arg in args):
            payload = POD_SCOPED_EVENTS
        elif resource == "nodes":
            payload = NODES
        elif resource == "services":
            payload = SERVICES
        elif resource == "endpoints":
            payload = ENDPOINTS
        elif resource == "serviceaccount":
            payload = {"metadata": {"name": "default"}, "imagePullSecrets": []}

        return KubectlResult(command, True, json.dumps(payload), "", 0, data=payload)


def build_service(kubectl: FakeKubectl) -> InvestigationService:
    """Inject the fake cluster at the provider seam.

    The engine reaches a cluster only through a provider, so tests substitute
    one rather than reaching past it.
    """
    service = InvestigationService(context="test-cluster")
    service.provider = LocalKubectlProvider(executor=kubectl)
    return service


async def test_investigation_returns_the_expected_payload_shape():
    investigation = await build_service(FakeKubectl()).run()

    expected_keys = {
        "context",
        "scope",
        "health",
        "overview",
        "severity",
        "metrics",
        "security",
        "topology",
        "timeline",
        "executed_commands",
        "pods",
        "logs",
        "events",
        "deployments",
        "network",
        "nodes",
        "storage",
        "workloads",
    }
    assert expected_keys <= set(investigation)


async def test_evidence_index_and_coverage_are_attached():
    investigation = await build_service(FakeKubectl()).run()

    kinds = {entry["kind"] for entry in investigation["evidence"]}
    assert {"k8s.pods", "k8s.pods.logs", "k8s.events", "k8s.nodes"} <= kinds

    coverage = investigation["evidence_coverage"]
    # Prometheus and Loki are not configured in tests, so their evidence is
    # not-applicable and must not count against completeness.
    assert coverage["completeness"] == 100
    assert coverage["not_applicable"] > 0
    assert coverage["applicable"] < coverage["total"]

    for entry in investigation["evidence"]:
        assert entry["id"]
        assert entry["collector_id"]


async def test_problematic_pods_drive_dependent_log_collection():
    investigation = await build_service(FakeKubectl()).run()

    assert investigation["pods"]["problematic_pods"][0]["name"] == "web-0"
    assert investigation["logs"]["checked_pods"] == 1


async def test_secrets_never_reach_the_persisted_investigation():
    investigation = await build_service(FakeKubectl()).run()
    serialized = json.dumps(investigation)

    assert LEAKED_PASSWORD not in serialized
    assert LEAKED_TOKEN not in serialized
    assert "[REDACTED]" in serialized


async def test_metrics_are_derived_from_collected_evidence():
    investigation = await build_service(FakeKubectl()).run()

    assert investigation["metrics"]["available"] is True
    assert investigation["metrics"]["cpu_usage"] == "5%"
    assert investigation["metrics"]["memory_usage"] == "40%"
    assert investigation["overview"]["nodes"] == "1/1 Healthy"


async def test_one_failing_inspector_does_not_abort_the_investigation():
    investigation = await build_service(FakeKubectl(failing_resources={"pvc"})).run()

    assert investigation["health"]["status"] == "error"
    assert "permissions" in investigation["health"]["message"]
    # Unrelated evidence was still collected.
    assert investigation["pods"]["problematic_pods"][0]["name"] == "web-0"
    assert investigation["metrics"]["available"] is True


async def test_timeline_reports_each_collection_stage():
    investigation = await build_service(FakeKubectl()).run()

    messages = [entry["message"] for entry in investigation["timeline"]]
    assert messages[0] == "Investigation Started"
    assert messages[-1] == "Evidence Collection Complete"
    assert "Retrieved Pods" in messages
    assert "Read Pod Logs" in messages
    assert messages.index("Retrieved Pods") < messages.index("Read Pod Logs")

    # The deep investigation is visible, and follows baseline collection.
    assert "Deep Investigation Started" in messages
    assert "Inspected Pod Specifications" in messages
    assert messages.index("Read Pod Logs") < messages.index("Deep Investigation Started")


async def test_crashloop_playbook_runs_and_is_recorded():
    investigation = await build_service(FakeKubectl()).run()

    rounds = investigation["playbook_rounds"]
    assert rounds, "a crash-looping pod should trigger a deep investigation"
    assert "crashloop" in rounds[0]["playbooks"]
    assert rounds[0]["evidence_added"] > 0

    collectors = rounds[0]["collectors"]
    assert any(item.startswith("k8s.pod.spec:") for item in collectors)
    assert any(item.startswith("k8s.pod.logs.previous:") for item in collectors)
    assert any(item.startswith("k8s.pod.config_refs:") for item in collectors)


async def test_deep_evidence_is_attached_and_addressable():
    investigation = await build_service(FakeKubectl()).run()

    deep = investigation["deep_evidence"]
    assert "k8s.pod.spec" in deep
    assert deep["k8s.pod.spec"][0]["id"] == "k8s.pod.spec:pod/prod/web-0"
    assert deep["k8s.pod.spec"][0]["data"]["containers"][0]["restart_count"] == 7

    # Baseline evidence stays in its named section rather than being duplicated.
    assert "k8s.pods" not in deep


async def test_targeted_evidence_produces_signals_baseline_could_not():
    from app.analysis.engine import AnalysisEngine

    investigation = await build_service(FakeKubectl()).run()
    analysis = AnalysisEngine().analyze(investigation)
    types = {signal.type for signal in analysis.signals}

    # Exit code 137 confirms the OOM that the pod status alone only hinted at.
    assert "container.oom_exit_code" in types
    # The referenced ConfigMap exists but lacks the key the container reads.
    assert "config.key_missing" in types

    hypotheses = {item.id for item in analysis.hypotheses}
    assert "workload.out_of_memory" in hypotheses
    assert "workload.missing_configuration" in hypotheses


async def test_previous_container_logs_are_collected():
    investigation = await build_service(FakeKubectl()).run()

    previous = investigation["deep_evidence"]["k8s.pod.logs.previous"][0]["data"]
    assert any("DB_HOST is not set" in line for line in previous["lines"])


async def test_secret_values_are_never_requested():
    kubectl = FakeKubectl()
    await build_service(kubectl).run()

    for command in kubectl.executed_commands:
        if "secret" in command:
            assert command.startswith("kubectl --context test-cluster describe secret"), (
                f"secrets must be read with describe, never get: {command}"
            )


async def test_audit_trail_records_every_command():
    kubectl = FakeKubectl()
    investigation = await build_service(kubectl).run()

    assert investigation["executed_commands"]
    assert any("get pods" in command for command in investigation["executed_commands"])


class UnreachableCluster(FakeKubectl):
    """Every read fails, as when the kubeconfig names a context that is gone.

    Delegates to the real fake first so the read-only policy check and the
    audit trail still run, then replaces the outcome.
    """

    def run(self, args: list[str], parse_json: bool = False) -> KubectlResult:
        result = super().run(args, parse_json)
        return KubectlResult(result.command, False, "", "Unable to connect to the server", 1)


class TestTotallyUnreachableCluster:
    """A cluster that answered nothing must not look like a successful run.

    `collection_failure()` draws the line between partial degradation and total
    failure by asking whether *any* usable evidence exists. That verdict is only
    as good as the collectors' honesty: one collector reporting `ok` when it
    read nothing is enough to make a wholly failed investigation report itself
    as succeeded. `WorkloadInspector` did exactly that — it recorded its read
    failures as findings and returned no top-level error.
    """

    async def test_no_collector_claims_usable_evidence(self):
        investigation = await build_service(UnreachableCluster()).run()

        usable = [item for item in investigation["evidence"] if item["status"] in {"ok", "empty"}]
        assert usable == [], f"collectors claimed evidence they could not have: {usable}"

    async def test_the_run_is_reported_as_a_total_failure(self):
        investigation = await build_service(UnreachableCluster()).run()

        assert investigation["evidence_coverage"]["usable"] == 0
        assert investigation["health"]["status"] == "error"
        assert collection_failure(investigation), (
            "an investigation that collected nothing must not be reported as success"
        )

    async def test_read_failures_are_not_reported_as_workload_findings(self):
        """Four unreadable resources are one failure, not four problems found."""
        investigation = await build_service(UnreachableCluster()).run()

        assert investigation.get("workloads", {}).get("findings", []) == []

    async def test_a_reachable_cluster_is_still_a_success(self):
        """The guard against making the check so strict that nothing passes."""
        investigation = await build_service(FakeKubectl()).run()

        assert investigation["evidence_coverage"]["usable"] > 0
        assert collection_failure(investigation) is None
