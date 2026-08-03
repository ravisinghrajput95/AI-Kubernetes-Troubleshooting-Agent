from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, items, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest

RESOURCES = ("statefulsets", "daemonsets", "jobs", "cronjobs")


class WorkloadInspector:
    id = "k8s.workloads"
    kind = EvidenceKind.WORKLOADS
    label = "Checked Extended Workloads"

    def requests(self, scope) -> list[ResourceRequest]:
        # One request per resource, in RESOURCES order, so `analyse` can pair
        # each result with the kind it came from. Before M5 these were four
        # sequential kubectl calls; the provider now runs them as one batch,
        # which on a remote agent is one round trip instead of four.
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource=resource,
                namespace=scope.namespace,
                all_namespaces=not scope.namespace,
            )
            for resource in RESOURCES
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        findings = []
        inventory = []
        errors: list[ProviderResult] = []

        for resource, result in zip(RESOURCES, results, strict=True):
            if not usable(result):
                errors.append(result)
                findings.append(
                    {
                        "resource": resource,
                        "issue": "Unable to inspect resource",
                        "error": result.error,
                    }
                )
                continue

            for item in items(result):
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
            return failure(errors[0], findings=[], inventory=[])

        return {
            "healthy": len(findings) == 0,
            "findings": findings,
            "inventory": inventory,
        }

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
        if resource == "jobs":
            # A Job's `Failed` condition carries *why* it gave up, and the two
            # reasons need opposite fixes: `DeadlineExceeded` means the work
            # ran past `activeDeadlineSeconds` and may simply need longer,
            # while `BackoffLimitExceeded` means it failed repeatedly and the
            # work itself is broken. Reporting only "has failed pods" gave the
            # same sentence for both — and said nothing at all for a Job that
            # hit its deadline without any pod failing, which is the deadline
            # case's normal shape.
            for condition in status.get("conditions", []) or []:
                if condition.get("type") != "Failed" or condition.get("status") != "True":
                    continue
                reason = condition.get("reason", "")
                if reason == "DeadlineExceeded":
                    return "Job exceeded its active deadline"
                if reason == "BackoffLimitExceeded":
                    return "Job exhausted its backoff limit"
                return f"Job failed: {reason}" if reason else "Job failed"
            if status.get("failed", 0) > 0:
                return "Job has failed pods"
        if resource == "cronjobs" and spec.get("suspend") is True:
            return "CronJob is suspended"
        return ""
