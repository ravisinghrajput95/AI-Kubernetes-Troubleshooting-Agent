from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor


class NetworkInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(self, namespace: str | None = None) -> dict[str, Any]:
        service_args = ["get", "services"]
        endpoint_args = ["get", "endpoints"]
        if namespace:
            service_args.extend(["-n", namespace])
            endpoint_args.extend(["-n", namespace])
        else:
            service_args.append("-A")
            endpoint_args.append("-A")
        service_args.extend(["-o", "json"])
        endpoint_args.extend(["-o", "json"])

        services_result = self.kubectl.run(service_args, parse_json=True)
        endpoints_result = self.kubectl.run(endpoint_args, parse_json=True)

        if not services_result.success or not isinstance(services_result.data, dict):
            return {
                "healthy": False,
                "findings": [],
                "error": services_result.stderr,
                "command": services_result.to_dict(),
            }

        if not endpoints_result.success or not isinstance(endpoints_result.data, dict):
            return {
                "healthy": False,
                "findings": [],
                "error": endpoints_result.stderr,
                "command": endpoints_result.to_dict(),
            }

        endpoints_by_key = self._endpoints_by_key(endpoints_result.data.get("items", []))
        findings = []
        has_dns_service = False

        for service in services_result.data.get("items", []):
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

        if not namespace and not has_dns_service:
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
            "total_services": len(services_result.data.get("items", [])),
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
