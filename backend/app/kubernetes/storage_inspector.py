from collections.abc import Sequence
from typing import Any

from app.evidence.models import EvidenceKind
from app.kubernetes.inspector import failure, items, usable
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest


class StorageInspector:
    id = "k8s.storage"
    kind = EvidenceKind.STORAGE
    label = "Checked Storage"

    def requests(self, scope) -> list[ResourceRequest]:
        return [
            ResourceRequest(
                verb=ReadVerb.GET,
                resource="pvc",
                namespace=scope.namespace,
                all_namespaces=not scope.namespace,
            ),
            # Cluster-scoped, and deliberately optional: a failed PV read
            # degrades this inspector's findings without failing it, because
            # claims are still analysable without volumes.
            ResourceRequest(verb=ReadVerb.GET, resource="pv"),
        ]

    def analyse(self, results: Sequence[ProviderResult], scope) -> dict[str, Any]:
        pvc_result, pv_result = results[0], results[1]

        if not usable(pvc_result):
            return failure(pvc_result, findings=[])

        findings = []
        claims = []
        for pvc in items(pvc_result):
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
        if usable(pv_result):
            for pv in items(pv_result):
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
