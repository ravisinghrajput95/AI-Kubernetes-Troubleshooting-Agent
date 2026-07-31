import asyncio
from datetime import datetime
from typing import Any

from loguru import logger

from app.ai.evidence_redactor import EvidenceRedactor
from app.auth.models import Principal
from app.collectors.base import (
    CollectionBudget,
    CollectionContext,
    InvestigationScope,
    NullProgressReporter,
    ProgressReporter,
)
from app.collectors.kubernetes import build_default_collectors
from app.core.config import settings
from app.evidence.models import EvidenceKind
from app.evidence.store import EvidenceStore
from app.kubernetes.errors import friendly_error
from app.playbooks.kubernetes import DEFAULT_PLAYBOOKS
from app.playbooks.orchestrator import DEFAULT_MAX_ROUNDS, InvestigationOrchestrator
from app.playbooks.registry import PlaybookRegistry
from app.providers.local_kubectl import LocalKubectlProvider

# Evidence kinds already surfaced through a named investigation section.
BASELINE_KINDS = frozenset(
    {
        EvidenceKind.PODS,
        EvidenceKind.POD_LOGS,
        EvidenceKind.EVENTS,
        EvidenceKind.DEPLOYMENTS,
        EvidenceKind.NETWORK,
        EvidenceKind.NODES,
        EvidenceKind.NODES_RAW,
        EvidenceKind.STORAGE,
        EvidenceKind.WORKLOADS,
        EvidenceKind.METRICS_NODES,
        EvidenceKind.METRICS_PODS,
    }
)

# Canonical timeline labels, in the order operators expect to read them.
# Collection itself is concurrent; ordering here is presentational only and the
# timestamps are each collector's real completion time.
TIMELINE_LABELS: tuple[tuple[str, str], ...] = (
    (EvidenceKind.PODS, "Retrieved Pods"),
    (EvidenceKind.POD_LOGS, "Read Pod Logs"),
    (EvidenceKind.EVENTS, "Retrieved Events"),
    (EvidenceKind.DEPLOYMENTS, "Validated Deployments"),
    (EvidenceKind.NETWORK, "Checked Networking"),
    (EvidenceKind.NODES, "Checked Nodes"),
    (EvidenceKind.STORAGE, "Checked Storage"),
    (EvidenceKind.WORKLOADS, "Checked Extended Workloads"),
    (EvidenceKind.METRICS_NODES, "Collected Cluster Metrics"),
)

# Deep-investigation steps, appended in the order they actually completed.
DEEP_TIMELINE_LABELS: dict[str, str] = {
    EvidenceKind.POD_SPEC: "Inspected Pod Specifications",
    EvidenceKind.POD_LOGS_PREVIOUS: "Read Previous Container Logs",
    EvidenceKind.RESOURCE_EVENTS: "Read Resource Events",
    EvidenceKind.CONFIG_REFS: "Resolved Config References",
    EvidenceKind.QUOTAS: "Checked Resource Quotas",
    EvidenceKind.LIMIT_RANGES: "Checked Limit Ranges",
    EvidenceKind.STORAGE_CLASSES: "Checked Storage Classes",
    EvidenceKind.VOLUME_ATTACHMENTS: "Checked Volume Attachments",
    EvidenceKind.ENDPOINT_SLICES: "Checked Endpoint Slices",
    EvidenceKind.NETWORK_POLICIES: "Checked Network Policies",
    EvidenceKind.INGRESSES: "Checked Ingresses",
    EvidenceKind.DNS_WORKLOAD: "Checked Cluster DNS",
    EvidenceKind.SERVICE_ACCOUNT: "Checked Service Account",
}


class InvestigationService:
    """Orchestrates evidence collection and derives the investigation summary.

    Collection is delegated to the collector graph; this class is responsible
    only for turning the resulting evidence into the investigation payload the
    API and reports consume.
    """

    def __init__(
        self,
        context: str | None = None,
        namespace: str | None = None,
        resource_kind: str | None = None,
        resource_name: str | None = None,
        budget: CollectionBudget | None = None,
        reporter: ProgressReporter | None = None,
        max_playbook_rounds: int = DEFAULT_MAX_ROUNDS,
        principal: Principal | None = None,
    ) -> None:
        self.max_playbook_rounds = max_playbook_rounds
        self.principal = principal
        self.budget_max_items = settings.max_list_items
        self.context = context
        self.namespace = namespace
        self.resource_kind = resource_kind.lower() if resource_kind else None
        self.resource_name = resource_name
        self.budget = budget or CollectionBudget()
        self.reporter = reporter or NullProgressReporter()
        # Every cluster read runs as the calling user when impersonation is on.
        # The engine reaches the cluster only through the provider; the executor
        # is exposed for collectors not yet migrated to ResourceRequest.
        self.provider = LocalKubectlProvider(context=context, principal=principal)
        self.scope = InvestigationScope(
            context=context,
            namespace=namespace,
            resource_kind=self.resource_kind,
            resource_name=resource_name,
        )

    async def run(self) -> dict[str, Any]:
        logger.info("Starting Kubernetes investigation for context={context}", context=self.context)

        started_at = datetime.now()
        store, rounds = await self._collect()
        investigation = self._build_view(store)
        investigation["playbook_rounds"] = rounds
        investigation["collection_limits"] = self._collection_limits()
        investigation["timeline"] = self._timeline(store, started_at)
        investigation["executed_commands"] = self.kubectl.executed_commands
        return investigation

    @property
    def kubectl(self):
        """Executor behind the provider, for the audit trail and legacy collectors."""
        return self.provider.raw_executor()

    def _collection_limits(self) -> dict[str, Any]:
        """Where the cluster was larger than this investigation looked.

        A partial view is a legitimate outcome on a large cluster, but it has to
        be visible: a diagnosis drawn from the first 2000 of 40000 pods is not
        the same claim as one drawn from all of them.
        """
        truncations = list(self.kubectl.truncations)
        return {
            "max_list_items": self.budget_max_items,
            "truncated": bool(truncations),
            "reads": truncations,
        }

    def _build_view(self, store: EvidenceStore) -> dict[str, Any]:
        """Assemble the investigation payload from collected evidence.

        Called after every collection round so the analysis engine always sees
        the latest evidence, including whatever the playbooks just gathered.
        """
        pods = self._payload(store, EvidenceKind.PODS)
        logs = self._payload(store, EvidenceKind.POD_LOGS)
        events = self._payload(store, EvidenceKind.EVENTS)
        deployments = self._payload(store, EvidenceKind.DEPLOYMENTS)
        network = self._payload(store, EvidenceKind.NETWORK)
        nodes = self._payload(store, EvidenceKind.NODES)
        storage = self._payload(store, EvidenceKind.STORAGE)
        workloads = self._payload(store, EvidenceKind.WORKLOADS)

        metrics = self._resource_usage(store)
        security = self._security_analysis(pods)
        topology = self._cluster_topology(pods)
        health = self._health_summary(pods, events, deployments, network, nodes, storage, workloads)
        overview = self._cluster_overview(store, pods, events, deployments, network, metrics)
        severity = self._severity_summary(
            pods, events, deployments, network, nodes, storage, workloads
        )

        return {
            "context": self.context,
            "scope": self._scope(),
            "health": health,
            "overview": overview,
            "severity": severity,
            "metrics": metrics,
            "security": security,
            "topology": topology,
            "evidence": store.index(),
            "evidence_coverage": store.coverage(),
            "deep_evidence": self._deep_evidence(store),
            "pods": pods,
            "logs": logs,
            "events": events,
            "deployments": deployments,
            "network": network,
            "nodes": nodes,
            "storage": storage,
            "workloads": workloads,
        }

    async def _collect(self) -> tuple[EvidenceStore, list[dict[str, Any]]]:
        context = CollectionContext(
            scope=self.scope,
            provider=self.provider,
            budget=self.budget,
            reporter=self.reporter,
        )
        orchestrator = InvestigationOrchestrator(
            playbooks=PlaybookRegistry(DEFAULT_PLAYBOOKS),
            redactor=EvidenceRedactor(),
            max_rounds=self.max_playbook_rounds,
        )
        result = await orchestrator.run(
            context,
            baseline=build_default_collectors(self.kubectl),
            build_view=self._build_view,
        )
        return result.store, result.to_dict()

    def _deep_evidence(self, store: EvidenceStore) -> dict[str, list[dict[str, Any]]]:
        """Payloads for evidence kinds that have no legacy investigation section.

        Baseline evidence is already carried in `pods`, `logs`, and friends;
        including it again would double the size of every report.
        """
        deep: dict[str, list[dict[str, Any]]] = {}

        for evidence in store:
            if evidence.kind in BASELINE_KINDS or evidence.data is None:
                continue
            deep.setdefault(evidence.kind, []).append(
                {
                    "id": evidence.id,
                    "target": evidence.target.to_dict(),
                    "status": str(evidence.status),
                    "data": evidence.data,
                }
            )

        return deep

    def _payload(self, store: EvidenceStore, kind: str) -> dict[str, Any]:
        """Legacy inspector payload for `kind`.

        Degraded evidence is surfaced through the same `{"error": ...}` shape
        the inspectors already use, so downstream summaries need no special
        handling for a collector that failed, timed out, or was skipped.
        """
        for evidence in store.by_kind(kind):
            if evidence.usable:
                data = evidence.data
                return data if isinstance(data, dict) else {}
            return {
                "healthy": False,
                "error": evidence.detail or f"Evidence '{kind}' was not collected.",
                "evidence_id": evidence.id,
                "evidence_status": str(evidence.status),
            }
        return {}

    def _timeline(self, store: EvidenceStore, started_at: datetime) -> list[dict[str, str]]:
        timeline = [{"time": started_at.strftime("%H:%M:%S"), "message": "Investigation Started"}]

        latest = started_at
        for kind, message in TIMELINE_LABELS:
            evidence = store.first(kind)
            if evidence is None:
                continue
            completed = evidence.collected_at.astimezone()
            latest = max(latest, completed.replace(tzinfo=None))
            timeline.append({"time": completed.strftime("%H:%M:%S"), "message": message})

        deep_steps = []
        for kind, message in DEEP_TIMELINE_LABELS.items():
            evidence = store.first(kind)
            if evidence is None:
                continue
            completed = evidence.collected_at.astimezone()
            latest = max(latest, completed.replace(tzinfo=None))
            deep_steps.append((completed, message))

        if deep_steps:
            timeline.append(
                {
                    "time": min(item[0] for item in deep_steps).strftime("%H:%M:%S"),
                    "message": "Deep Investigation Started",
                }
            )
            for completed, message in sorted(deep_steps, key=lambda item: item[0]):
                timeline.append({"time": completed.strftime("%H:%M:%S"), "message": message})

        timeline.append(
            {"time": latest.strftime("%H:%M:%S"), "message": "Evidence Collection Complete"}
        )
        return timeline

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
        errors = self._collector_errors(
            pods, events, deployments, network, nodes, storage, workloads
        )
        if errors:
            return {
                "status": "error",
                "message": self._friendly_error(str(errors[0])),
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
        return friendly_error(error)

    def _cluster_overview(
        self,
        store: EvidenceStore,
        pods: dict[str, Any],
        events: dict[str, Any],
        deployments: dict[str, Any],
        network: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        node_items = (store.data(EvidenceKind.NODES_RAW, {}) or {}).get("items", [])
        total_nodes = len(node_items)
        healthy_nodes = 0

        for node in node_items:
            conditions = node.get("status", {}).get("conditions", [])
            if any(
                item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
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

    def _resource_usage(self, store: EvidenceStore) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "available": False,
            "cpu_usage": "N/A",
            "memory_usage": "N/A",
            "node_metrics": [],
            "top_pods": [],
            "message": "metrics-server is unavailable or kubectl top is not permitted.",
        }

        node_lines = (store.data(EvidenceKind.METRICS_NODES, {}) or {}).get("lines")
        if not node_lines:
            return metrics

        cpu_values = []
        memory_values = []
        for line in node_lines:
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

        pod_rows = []
        for line in (store.data(EvidenceKind.METRICS_PODS, {}) or {}).get("lines", []):
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
        elif self._collector_errors(pods, events, deployments, network, nodes, storage, workloads):
            # No findings, but the collectors that produce findings did not all
            # run. Absence of evidence is not evidence of absence: reporting
            # "Healthy" here claims a cluster is well on the strength of reads
            # that failed. The report said Healthy while the history entry said
            # Critical, because `history_service` patched the same hole further
            # downstream — one investigation with two severities.
            severity = "Unknown"
        else:
            severity = "Healthy"

        impact = {
            "Critical": "Production",
            "High": "Production",
            "Unknown": "Not established — the cluster could not be fully inspected",
        }.get(severity, "No active impact detected")

        return {
            "severity": severity,
            "impact": impact,
            "affected_workloads": workload_count,
            "affected_namespace": next(iter(namespaces), "none"),
        }

    def _collector_errors(self, *sections: dict[str, Any]) -> list[str]:
        """Errors from the collectors whose findings decide severity."""
        return [str(item.get("error")) for item in sections if item.get("error")]

    def _scope(self) -> dict[str, Any]:
        return self.scope.to_dict()


def start_investigation(context: str | None = None) -> dict[str, Any]:
    """Synchronous entry point retained for non-async callers."""
    return asyncio.run(InvestigationService(context=context).run())
