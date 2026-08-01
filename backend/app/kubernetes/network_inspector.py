from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, items, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest


class NetworkInspector:
    id = "k8s.network"
    kind = EvidenceKind.NETWORK
    label = "Checked Networking"

    def requests(self, scope) -> list[ResourceRequest]:
        # Order matters: `analyse` reads these positionally.
        cluster_wide = not scope.namespace
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="services",
                namespace=scope.namespace,
                all_namespaces=cluster_wide,
            ),
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="endpoints",
                namespace=scope.namespace,
                all_namespaces=cluster_wide,
            ),
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        services_result, endpoints_result = results[0], results[1]

        if not usable(services_result):
            return failure(services_result, findings=[])
        if not usable(endpoints_result):
            return failure(endpoints_result, findings=[])

        services = items(services_result)
        endpoints_by_key = self._endpoints_by_key(items(endpoints_result))
        findings = []
        has_dns_service = False

        for service in services:
            metadata = service.get("metadata", {})
            spec = service.get("spec", {})
            namespace = metadata.get("namespace", "default")
            name = metadata.get("name", "unknown")
            service_type = spec.get("type", "ClusterIP")

            if namespace == "kube-system" and name in {"kube-dns", "coredns"}:
                has_dns_service = True

            if service_type == "ExternalName":
                continue

            if not spec.get("selector"):
                findings.append(
                    {
                        "namespace": namespace,
                        "service": name,
                        "issue": "Service has no selector",
                    }
                )
                continue

            endpoint_count = endpoints_by_key.get((namespace, name), 0)
            if endpoint_count == 0:
                findings.append(
                    {
                        "namespace": namespace,
                        "service": name,
                        "issue": "Service has no ready endpoints; selector may not match any pods",
                    }
                )

        # Only sayable when the whole cluster was scanned: a namespaced
        # investigation that cannot see kube-system has learned nothing about
        # cluster DNS.
        #
        # This is a **fix, not a migration**. Before M5 the loop above assigned
        # to the same `namespace` local the check read, so after any non-empty
        # service list it held the last service's namespace — always truthy,
        # and the check never fired. A cluster with no DNS service reported
        # nothing wrong with its DNS. Keyed on the scope, it fires again.
        if not scope.namespace and not has_dns_service:
            findings.append(
                {
                    "namespace": "kube-system",
                    "service": "kube-dns/coredns",
                    "issue": "Cluster DNS service was not found",
                }
            )

        return {
            "healthy": len(findings) == 0,
            "findings": findings,
            "total_services": len(services),
        }

    def _endpoints_by_key(self, endpoints: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
        counts = {}

        for endpoint in endpoints:
            metadata = endpoint.get("metadata", {})
            namespace = metadata.get("namespace", "default")
            name = metadata.get("name", "unknown")
            ready_addresses = 0

            for subset in endpoint.get("subsets", []):
                ready_addresses += len(subset.get("addresses", []))

            counts[(namespace, name)] = ready_addresses

        return counts
