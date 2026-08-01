"""The inspectors as pure analysis, which is what M5 made them.

Before M5 an inspector held a `KubectlExecutor`, built argv, ran it and analysed
the result, so testing the analysis meant faking a subprocess. It is now a
function from provider results to findings, and can be tested by handing it the
JSON a cluster would have returned.

Two things these cover that nothing else does:

- **`requests()` asks for the right scope.** A namespaced investigation that
  silently read the whole cluster, or a cluster-wide one that read only
  `default`, would still produce plausible findings.
- **A conclusion that depends on scope rather than on data.** The cluster-DNS
  check is the only one, and it was unreachable for as long as it existed.
"""

from typing import Any

import pytest

from app.collectors.base import InvestigationScope
from app.kubernetes.deployment_inspector import DeploymentInspector
from app.kubernetes.events_analyzer import EventsAnalyzer
from app.kubernetes.network_inspector import NetworkInspector
from app.kubernetes.node_inspector import NodeInspector
from app.kubernetes.pod_inspector import PodInspector
from app.kubernetes.storage_inspector import StorageInspector
from app.kubernetes.workload_inspector import WorkloadInspector
from app.providers.base import ProviderResult, ReadVerb

ALL_INSPECTORS = [
    PodInspector,
    EventsAnalyzer,
    DeploymentInspector,
    NetworkInspector,
    NodeInspector,
    StorageInspector,
    WorkloadInspector,
]


def ok(payload: dict[str, Any]) -> ProviderResult:
    return ProviderResult(success=True, data=payload, equivalent_command="kubectl get x -o json")


def listing(*items: dict[str, Any]) -> ProviderResult:
    return ok({"items": list(items)})


def denied() -> ProviderResult:
    return ProviderResult(
        success=False,
        error="Error from server (Forbidden): pods is forbidden",
        equivalent_command="kubectl get pods -A -o json",
    )


def cluster_wide() -> InvestigationScope:
    return InvestigationScope(context="test")


def namespaced(namespace: str = "prod") -> InvestigationScope:
    return InvestigationScope(context="test", namespace=namespace)


def service(name: str, namespace: str, **spec: Any) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"type": "ClusterIP", "selector": {"app": name}, **spec},
    }


class TestScopeReachesTheRequests:
    """What an inspector asks for has to follow the investigation's scope."""

    @pytest.mark.parametrize("inspector", ALL_INSPECTORS)
    def test_a_namespaced_scope_narrows_every_namespaced_read(self, inspector):
        requests = inspector().requests(namespaced("prod"))

        for request in requests:
            if request.resource in {"nodes", "pv"}:
                continue  # cluster-scoped: a namespace would mean nothing
            assert request.namespace == "prod", f"{request.resource} ignored the namespace"
            assert not request.all_namespaces

    @pytest.mark.parametrize("inspector", ALL_INSPECTORS)
    def test_a_cluster_scope_reads_every_namespace(self, inspector):
        requests = inspector().requests(cluster_wide())

        for request in requests:
            if request.resource in {"nodes", "pv"}:
                continue
            assert request.all_namespaces, f"{request.resource} would read one namespace only"
            assert request.namespace is None

    @pytest.mark.parametrize("inspector", ALL_INSPECTORS)
    def test_no_inspector_asks_for_anything_but_a_read(self, inspector):
        """The closed verb set, restated where the requests are built."""
        for request in inspector().requests(cluster_wide()):
            assert request.verb in {ReadVerb.GET, ReadVerb.LOGS, ReadVerb.TOP, ReadVerb.DESCRIBE}

    def test_a_pod_scoped_investigation_reads_one_pod(self):
        scope = InvestigationScope(
            context="test", namespace="prod", resource_kind="pod", resource_name="web-0"
        )
        request = PodInspector().requests(scope)[0]

        assert request.resource == "pod"
        assert request.name == "web-0"
        assert request.namespace == "prod"
        assert not request.all_namespaces

    def test_workloads_ask_for_all_four_kinds_in_order(self):
        requests = WorkloadInspector().requests(cluster_wide())
        assert [request.resource for request in requests] == [
            "statefulsets",
            "daemonsets",
            "jobs",
            "cronjobs",
        ]


class TestFailedReadsStayFailures:
    """A read that did not happen must not become a healthy finding."""

    @pytest.mark.parametrize("inspector", ALL_INSPECTORS)
    def test_a_denied_primary_read_reports_an_error(self, inspector):
        instance = inspector()
        results = [denied() for _ in instance.requests(cluster_wide())]

        payload = instance.analyse(results, cluster_wide())

        assert payload["healthy"] is False
        assert "Forbidden" in payload["error"]

    def test_workloads_degrade_partially_when_only_some_reads_fail(self):
        results = [listing(), denied(), listing(), listing()]

        payload = WorkloadInspector().analyse(results, cluster_wide())

        # Three of four succeeded: findings, not a collection failure.
        assert "error" not in payload
        assert any(finding.get("resource") == "daemonsets" for finding in payload["findings"])

    def test_workloads_report_a_failure_when_every_read_fails(self):
        """The regression that made a wholly failed investigation look partial."""
        payload = WorkloadInspector().analyse([denied()] * 4, cluster_wide())

        assert payload["healthy"] is False
        assert "Forbidden" in payload["error"]
        assert payload["findings"] == []

    def test_storage_survives_a_failed_volume_read(self):
        """Claims are still analysable without volumes, so PV failure degrades."""
        claim = {
            "metadata": {"name": "data", "namespace": "prod"},
            "spec": {"storageClassName": "fast"},
            "status": {"phase": "Bound"},
        }
        payload = StorageInspector().analyse([listing(claim), denied()], cluster_wide())

        assert "error" not in payload
        assert payload["total_claims"] == 1


class TestTheClusterDnsCheckIsReachable:
    """A fix, not a migration — and the reason this file exists.

    The check read the same local the service loop assigned to, so after any
    non-empty service list it held the last service's namespace. Always truthy,
    so `if not namespace` never fired and a cluster with no DNS service
    reported nothing wrong with its DNS. Keyed on the scope instead, it works.
    """

    def test_a_cluster_scan_with_no_dns_service_says_so(self):
        services = listing(service("web", "prod"), service("api", "prod"))
        endpoints = listing(
            {"metadata": {"name": "web", "namespace": "prod"}, "subsets": [{"addresses": [{}]}]},
            {"metadata": {"name": "api", "namespace": "prod"}, "subsets": [{"addresses": [{}]}]},
        )

        payload = NetworkInspector().analyse([services, endpoints], cluster_wide())

        issues = [finding["issue"] for finding in payload["findings"]]
        assert "Cluster DNS service was not found" in issues

    def test_a_cluster_scan_with_coredns_stays_quiet(self):
        services = listing(service("coredns", "kube-system"), service("web", "prod"))
        endpoints = listing(
            {
                "metadata": {"name": "coredns", "namespace": "kube-system"},
                "subsets": [{"addresses": [{}]}],
            },
            {"metadata": {"name": "web", "namespace": "prod"}, "subsets": [{"addresses": [{}]}]},
        )

        payload = NetworkInspector().analyse([services, endpoints], cluster_wide())

        assert payload["findings"] == []

    def test_a_namespaced_scan_never_claims_dns_is_missing(self):
        """It could not have seen kube-system, so it has learned nothing."""
        services = listing(service("web", "prod"))
        endpoints = listing(
            {"metadata": {"name": "web", "namespace": "prod"}, "subsets": [{"addresses": [{}]}]}
        )

        payload = NetworkInspector().analyse([services, endpoints], namespaced("prod"))

        issues = [finding["issue"] for finding in payload["findings"]]
        assert "Cluster DNS service was not found" not in issues


class TestFindingsAreUnchangedByTheMigration:
    """Spot checks on analysis that moved across verbatim."""

    def test_a_service_without_endpoints_is_flagged(self):
        services = listing(service("web", "prod"))
        endpoints = listing()

        payload = NetworkInspector().analyse([services, endpoints], namespaced("prod"))

        assert payload["findings"][0]["issue"].startswith("Service has no ready endpoints")

    def test_an_externalname_service_is_skipped(self):
        services = listing(service("db", "prod", type="ExternalName", selector=None))
        payload = NetworkInspector().analyse([services, listing()], namespaced("prod"))

        assert payload["findings"] == []

    def test_a_crashlooping_pod_is_problematic(self):
        pod = {
            "metadata": {"name": "web-0", "namespace": "prod"},
            "spec": {"containers": [{"name": "app", "image": "web:1"}], "nodeName": "node-1"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
            },
        }
        payload = PodInspector().analyse([listing(pod)], cluster_wide())

        assert payload["problematic_pods"][0]["status"] == "CrashLoopBackOff"
        assert payload["healthy"] is False

    def test_an_unbound_claim_is_flagged(self):
        claim = {
            "metadata": {"name": "data", "namespace": "prod"},
            "spec": {"storageClassName": "fast"},
            "status": {"phase": "Pending"},
        }
        payload = StorageInspector().analyse([listing(claim), listing()], cluster_wide())

        assert payload["findings"][0]["issue"] == "PersistentVolumeClaim is not bound"

    def test_a_not_ready_node_is_flagged(self):
        node = {
            "metadata": {"name": "node-1"},
            "spec": {},
            "status": {
                "conditions": [{"type": "Ready", "status": "False", "reason": "KubeletDown"}]
            },
        }
        payload = NodeInspector().analyse([listing(node)], cluster_wide())

        assert payload["findings"][0]["reason"] == "KubeletDown"

    def test_warning_events_become_findings(self):
        event = {
            "metadata": {"namespace": "prod"},
            "type": "Warning",
            "reason": "FailedScheduling",
            "involvedObject": {"kind": "Pod", "name": "web-0"},
            "message": "insufficient cpu",
        }
        payload = EventsAnalyzer().analyse([listing(event)], cluster_wide())

        assert payload["findings"][0]["object"] == "Pod/web-0"
        assert payload["total_events"] == 1

    def test_an_under_replicated_deployment_is_unhealthy(self):
        deployment = {
            "metadata": {"name": "web", "namespace": "prod"},
            "spec": {"replicas": 3},
            "status": {"availableReplicas": 1, "unavailableReplicas": 2, "conditions": []},
        }
        payload = DeploymentInspector().analyse([listing(deployment)], cluster_wide())

        assert payload["unhealthy_deployments"][0]["unavailable_replicas"] == 2
