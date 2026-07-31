from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor

RESOURCES = ("statefulsets", "daemonsets", "jobs", "cronjobs")


class WorkloadInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(self, namespace: str | None = None) -> dict[str, Any]:
        findings = []
        inventory = []
        errors: list[str] = []

        for resource in RESOURCES:
            result = self.kubectl.run(self._args(resource, namespace), parse_json=True)
            if not result.success or not isinstance(result.data, dict):
                errors.append(result.stderr)
                findings.append(
                    {
                        "resource": resource,
                        "issue": "Unable to inspect resource",
                        "error": result.stderr,
                    }
                )
                continue

            for item in result.data.get("items", []):
                summary = self._summary(resource, item)
                inventory.append(summary)
                issue = self._issue(resource, item)
                if issue:
                    findings.append({**summary, "issue": issue})

        if len(errors) == len(RESOURCES):
            # Nothing about workloads was observed, so this is a collection
            # failure and not a set of findings. Reporting it as findings put
            # `ok` evidence in the store for a cluster that could not be read
            # at all, which was enough to make a wholly failed investigation
            # count as partial degradation and report itself as succeeded.
            return {
                "error": errors[0],
                "healthy": False,
                "findings": [],
                "inventory": [],
            }

        return {
            "healthy": len(findings) == 0,
            "findings": findings,
            "inventory": inventory,
        }

    def _args(self, resource: str, namespace: str | None) -> list[str]:
        args = ["get", resource]
        if namespace:
            args.extend(["-n", namespace])
        else:
            args.append("-A")
        args.extend(["-o", "json"])
        return args

    def _summary(self, resource: str, item: dict[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        spec = item.get("spec", {})
        return {
            "kind": resource,
            "namespace": metadata.get("namespace", "default"),
            "name": metadata.get("name", "unknown"),
            "desired": spec.get("replicas", spec.get("completions", 0)),
            "ready": status.get("readyReplicas", status.get("succeeded", 0)),
            "failed": status.get("failed", 0),
        }

    def _issue(self, resource: str, item: dict[str, Any]) -> str:
        status = item.get("status", {})
        spec = item.get("spec", {})

        if resource == "statefulsets":
            desired = spec.get("replicas", 0)
            ready = status.get("readyReplicas", 0)
            if ready < desired:
                return "StatefulSet has unavailable replicas"
        if resource == "daemonsets":
            desired = status.get("desiredNumberScheduled", 0)
            ready = status.get("numberReady", 0)
            if ready < desired:
                return "DaemonSet is not ready on all scheduled nodes"
        if resource == "jobs" and status.get("failed", 0) > 0:
            return "Job has failed pods"
        if resource == "cronjobs" and spec.get("suspend") is True:
            return "CronJob is suspended"
        return ""
