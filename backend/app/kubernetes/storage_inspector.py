from typing import Any

from app.kubernetes.kubectl_executor import KubectlExecutor


class StorageInspector:
    def __init__(self, kubectl: KubectlExecutor | None = None) -> None:
        self.kubectl = kubectl or KubectlExecutor()

    def inspect(self, namespace: str | None = None) -> dict[str, Any]:
        pvc_args = ["get", "pvc"]
        if namespace:
            pvc_args.extend(["-n", namespace])
        else:
            pvc_args.append("-A")
        pvc_args.extend(["-o", "json"])

        pvc_result = self.kubectl.run(pvc_args, parse_json=True)
        pv_result = self.kubectl.run(["get", "pv", "-o", "json"], parse_json=True)

        if not pvc_result.success or not isinstance(pvc_result.data, dict):
            return {
                "healthy": False,
                "findings": [],
                "error": pvc_result.stderr,
                "command": pvc_result.to_dict(),
            }

        findings = []
        claims = []
        for pvc in pvc_result.data.get("items", []):
            metadata = pvc.get("metadata", {})
            status = pvc.get("status", {})
            spec = pvc.get("spec", {})
            phase = status.get("phase", "Unknown")
            claim = {
                "namespace": metadata.get("namespace", "default"),
                "name": metadata.get("name", "unknown"),
                "phase": phase,
                "storage_class": spec.get("storageClassName", "none"),
                "volume": spec.get("volumeName", ""),
            }
            claims.append(claim)
            if phase != "Bound":
                findings.append(
                    {
                        **claim,
                        "issue": "PersistentVolumeClaim is not bound",
                    }
                )

        pv_findings = []
        if pv_result.success and isinstance(pv_result.data, dict):
            for pv in pv_result.data.get("items", []):
                metadata = pv.get("metadata", {})
                phase = pv.get("status", {}).get("phase", "Unknown")
                if phase in {"Failed", "Released"}:
                    pv_findings.append(
                        {
                            "name": metadata.get("name", "unknown"),
                            "phase": phase,
                            "issue": "PersistentVolume is not available for normal use",
                        }
                    )

        findings.extend(pv_findings)
        return {
            "healthy": len(findings) == 0,
            "findings": findings,
            "claims": claims,
            "total_claims": len(claims),
        }
