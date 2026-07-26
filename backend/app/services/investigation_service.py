from datetime import datetime
from typing import Any

from loguru import logger

from app.kubernetes.deployment_inspector import DeploymentInspector
from app.kubernetes.events_analyzer import EventsAnalyzer
from app.kubernetes.kubectl_executor import KubectlExecutor
from app.kubernetes.logs_collector import LogsCollector
from app.kubernetes.network_inspector import NetworkInspector
from app.kubernetes.node_inspector import NodeInspector
from app.kubernetes.pod_inspector import PodInspector
from app.kubernetes.storage_inspector import StorageInspector
from app.kubernetes.workload_inspector import WorkloadInspector


class InvestigationService:
    def __init__(
        self,
        context: str | None = None,
        namespace: str | None = None,
        resource_kind: str | None = None,
        resource_name: str | None = None,
    ) -> None:
        self.context = context
        self.namespace = namespace
        self.resource_kind = resource_kind.lower() if resource_kind else None
        self.resource_name = resource_name
        self.kubectl = KubectlExecutor(context=context)
        self.pod_inspector = PodInspector(self.kubectl)
        self.logs_collector = LogsCollector(self.kubectl)
        self.events_analyzer = EventsAnalyzer(self.kubectl)
        self.deployment_inspector = DeploymentInspector(self.kubectl)
        self.network_inspector = NetworkInspector(self.kubectl)
        self.node_inspector = NodeInspector(self.kubectl)
        self.storage_inspector = StorageInspector(self.kubectl)
        self.workload_inspector = WorkloadInspector(self.kubectl)

    def run(self) -> dict[str, Any]:
        logger.info("Starting Kubernetes investigation for context={context}", context=self.context)

        timeline = [self._timeline_event("Investigation Started")]
        scope = self._scope()
        pods = self.pod_inspector.inspect(
            namespace=self.namespace,
            pod_name=self.resource_name if self.resource_kind == "pod" else None,
        )
        timeline.append(self._timeline_event("Retrieved Pods"))
        logs = self.logs_collector.collect(pods.get("problematic_pods", []))
        timeline.append(self._timeline_event("Read Pod Logs"))
        events = self.events_analyzer.analyze(namespace=self.namespace)
        timeline.append(self._timeline_event("Retrieved Events"))
        deployments = self.deployment_inspector.inspect(
            namespace=self.namespace,
            deployment_name=self.resource_name if self.resource_kind == "deployment" else None,
        )
        timeline.append(self._timeline_event("Validated Deployments"))
        network = self.network_inspector.inspect(namespace=self.namespace)
        timeline.append(self._timeline_event("Checked Networking"))
        nodes = self.node_inspector.inspect()
        timeline.append(self._timeline_event("Checked Nodes"))
        storage = self.storage_inspector.inspect(namespace=self.namespace)
        timeline.append(self._timeline_event("Checked Storage"))
        workloads = self.workload_inspector.inspect(namespace=self.namespace)
        timeline.append(self._timeline_event("Checked Extended Workloads"))
        metrics = self._resource_usage()
        timeline.append(self._timeline_event("Collected Cluster Metrics"))
        security = self._security_analysis(pods)
        topology = self._cluster_topology(pods)
        health = self._health_summary(pods, events, deployments, network, nodes, storage, workloads)
        overview = self._cluster_overview(pods, events, deployments, network, metrics)
        severity = self._severity_summary(pods, events, deployments, network, nodes, storage, workloads)
        timeline.append(self._timeline_event("Evidence Collection Complete"))

        return {
            "context": self.context,
            "scope": scope,
            "health": health,
            "overview": overview,
            "severity": severity,
            "metrics": metrics,
            "security": security,
            "topology": topology,
            "timeline": timeline,
            "executed_commands": self.kubectl.executed_commands,
            "pods": pods,
            "logs": logs,
            "events": events,
            "deployments": deployments,
            "network": network,
            "nodes": nodes,
            "storage": storage,
            "workloads": workloads,
        }

    def _health_summary(
        self,
        pods: dict[str, Any],
        events: dict[str, Any],
        deployments: dict[str, Any],
        network: dict[str, Any],
        nodes: dict[str, Any],
        storage: dict[str, Any],
        workloads: dict[str, Any],
    ) -> dict[str, Any]:
        errors = [
            item.get("error")
            for item in [pods, events, deployments, network, nodes, storage, workloads]
            if item.get("error")
        ]
        if errors:
            return {
                "status": "error",
                "message": self._friendly_error(errors[0]),
            }

        unhealthy_count = (
            len(pods.get("problematic_pods", []))
            + len(events.get("findings", []))
            + len(deployments.get("unhealthy_deployments", []))
            + len(network.get("findings", []))
            + len(nodes.get("findings", []))
            + len(storage.get("findings", []))
            + len(workloads.get("findings", []))
        )
        if unhealthy_count == 0:
            return {
                "status": "healthy",
                "message": "No critical Kubernetes issues detected. Cluster appears healthy.",
            }

        return {
            "status": "issues_found",
            "message": f"Found {unhealthy_count} Kubernetes signals that need review.",
        }

    def _friendly_error(self, error: str) -> str:
        lowered = error.lower()
        if "not found on path" in lowered:
            return "kubectl is not installed or is not available on PATH."
        if "timed out" in lowered:
            return "Kubernetes investigation timed out. Verify cluster connectivity and try again."
        if "connection refused" in lowered or "unable to connect" in lowered:
            return "Unable to connect to Kubernetes cluster. Verify kubeconfig, cluster access, and kubectl permissions."
        if "forbidden" in lowered or "unauthorized" in lowered:
            return "kubectl authentication failed. Verify your Kubernetes credentials and permissions."
        if "no such file" in lowered or "kubeconfig" in lowered:
            return "Kubernetes kubeconfig could not be read. Verify KUBECONFIG_PATH or your default kubeconfig."
        return "Kubernetes investigation failed. Verify kubeconfig, cluster access, and kubectl permissions."

    def _cluster_overview(
        self,
        pods: dict[str, Any],
        events: dict[str, Any],
        deployments: dict[str, Any],
        network: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        nodes = self.kubectl.run(["get", "nodes", "-o", "json"], parse_json=True)
        total_nodes = 0
        healthy_nodes = 0

        if nodes.success and isinstance(nodes.data, dict):
            node_items = nodes.data.get("items", [])
            total_nodes = len(node_items)
            for node in node_items:
                conditions = node.get("status", {}).get("conditions", [])
                if any(
                    item.get("type") == "Ready" and item.get("status") == "True"
                    for item in conditions
                ):
                    healthy_nodes += 1

        alerts = len(events.get("findings", []))
        critical = (
            len(pods.get("problematic_pods", []))
            + len(deployments.get("unhealthy_deployments", []))
            + len(network.get("findings", []))
        )

        return {
            "nodes": f"{healthy_nodes}/{total_nodes} Healthy" if total_nodes else "Unavailable",
            "pods": f"{pods.get('running_pods', 0)} Running",
            "cpu_usage": metrics.get("cpu_usage", "N/A"),
            "memory_usage": metrics.get("memory_usage", "N/A"),
            "alerts": alerts,
            "critical_issues": critical,
        }

    def _resource_usage(self) -> dict[str, Any]:
        top_nodes = self.kubectl.run(["top", "nodes", "--no-headers"])
        top_pods = self.kubectl.run(["top", "pods", "-A", "--no-headers"])
        metrics = {
            "available": False,
            "cpu_usage": "N/A",
            "memory_usage": "N/A",
            "node_metrics": [],
            "top_pods": [],
            "message": "metrics-server is unavailable or kubectl top is not permitted.",
        }

        if not top_nodes.success:
            return metrics

        cpu_values = []
        memory_values = []
        for line in top_nodes.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                cpu_percent = self._percent_value(parts[2])
                memory_percent = self._percent_value(parts[4])
                cpu_values.append(cpu_percent)
                memory_values.append(memory_percent)
                metrics["node_metrics"].append(
                    {
                        "name": parts[0],
                        "cpu": parts[1],
                        "cpu_percent": f"{cpu_percent}%",
                        "memory": parts[3],
                        "memory_percent": f"{memory_percent}%",
                    }
                )

        cpu = round(sum(cpu_values) / len(cpu_values)) if cpu_values else None
        memory = round(sum(memory_values) / len(memory_values)) if memory_values else None
        metrics["available"] = cpu is not None or memory is not None
        metrics["cpu_usage"] = f"{cpu}%" if cpu is not None else "N/A"
        metrics["memory_usage"] = f"{memory}%" if memory is not None else "N/A"
        metrics["message"] = "Cluster metrics collected from metrics-server."

        if top_pods.success:
            pod_rows = []
            for line in top_pods.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    pod_rows.append(
                        {
                            "namespace": parts[0],
                            "name": parts[1],
                            "cpu": parts[2],
                            "memory": parts[3],
                        }
                    )
            metrics["top_pods"] = pod_rows[:8]

        return metrics

    def _percent_value(self, value: str) -> int:
        try:
            return int(value.replace("%", ""))
        except ValueError:
            return 0

    def _security_analysis(self, pods: dict[str, Any]) -> dict[str, Any]:
        inventory = pods.get("pod_inventory", [])
        findings = []
        privileged = []
        latest_tags = []
        missing_limits = []

        for pod in inventory:
            pod_ref = f"{pod.get('namespace', 'default')}/{pod.get('name', 'unknown')}"
            for container in pod.get("containers", []):
                container_ref = f"{pod_ref}:{container.get('name', 'container')}"
                security_context = container.get("security_context", {})
                image = container.get("image", "")

                if security_context.get("privileged") is True:
                    privileged.append(container_ref)
                if image.endswith(":latest") or ":" not in image.split("/")[-1]:
                    latest_tags.append(container_ref)
                if not container.get("has_limits"):
                    missing_limits.append(container_ref)

        findings.append(
            {
                "label": "No Privileged Containers",
                "status": "pass" if not privileged else "warning",
                "detail": "No privileged containers detected."
                if not privileged
                else f"{len(privileged)} privileged container(s) detected.",
            }
        )
        findings.append(
            {
                "label": "Latest Tag Used",
                "status": "warning" if latest_tags else "pass",
                "detail": "Images are pinned to explicit tags."
                if not latest_tags
                else f"{len(latest_tags)} container image(s) use latest or an implicit tag.",
            }
        )
        findings.append(
            {
                "label": "Missing Resource Limits",
                "status": "warning" if missing_limits else "pass",
                "detail": "All inspected containers define resource limits."
                if not missing_limits
                else f"{len(missing_limits)} container(s) are missing resource limits.",
            }
        )
        findings.append(
            {
                "label": "High CVEs Found",
                "status": "unknown",
                "detail": "Image vulnerability scan is not configured in this local evidence collection.",
            }
        )

        warning_count = len([item for item in findings if item["status"] == "warning"])
        return {
            "status": "warning" if warning_count else "pass",
            "warning_count": warning_count,
            "findings": findings,
        }

    def _cluster_topology(self, pods: dict[str, Any]) -> dict[str, Any]:
        nodes: dict[str, list[dict[str, str]]] = {}
        for pod in pods.get("pod_inventory", []):
            node = pod.get("node") or "Pending"
            nodes.setdefault(node, []).append(
                {
                    "name": pod.get("name", "unknown"),
                    "namespace": pod.get("namespace", "default"),
                    "phase": pod.get("phase", "Unknown"),
                }
            )

        return {
            "cluster": self.context or "Current Context",
            "nodes": [
                {
                    "name": node,
                    "pods": sorted(items, key=lambda item: item["name"])[:12],
                    "pod_count": len(items),
                }
                for node, items in sorted(nodes.items())
            ],
        }

    def _severity_summary(
        self,
        pods: dict[str, Any],
        events: dict[str, Any],
        deployments: dict[str, Any],
        network: dict[str, Any],
        nodes: dict[str, Any],
        storage: dict[str, Any],
        workloads: dict[str, Any],
    ) -> dict[str, Any]:
        problematic_pods = pods.get("problematic_pods", [])
        namespaces = {
            item.get("namespace", "unknown")
            for item in [*problematic_pods, *deployments.get("unhealthy_deployments", [])]
        }
        workload_count = (
            len(problematic_pods)
            + len(deployments.get("unhealthy_deployments", []))
            + len(workloads.get("findings", []))
        )
        event_count = len(events.get("findings", []))
        network_count = len(network.get("findings", []))
        node_count = len(nodes.get("findings", []))
        storage_count = len(storage.get("findings", []))

        if workload_count >= 3 or network_count > 0 or node_count > 0:
            severity = "Critical"
        elif workload_count > 0 or event_count > 0 or storage_count > 0:
            severity = "High"
        else:
            severity = "Healthy"

        return {
            "severity": severity,
            "impact": "Production" if severity in {"Critical", "High"} else "No active impact detected",
            "affected_workloads": workload_count,
            "affected_namespace": next(iter(namespaces), "none"),
        }

    def _timeline_event(self, message: str) -> dict[str, str]:
        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
        }

    def _scope(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace or "all",
            "resource_kind": self.resource_kind or "cluster",
            "resource_name": self.resource_name or "",
        }


def start_investigation(context: str | None = None) -> dict[str, Any]:
    return InvestigationService(context=context).run()
