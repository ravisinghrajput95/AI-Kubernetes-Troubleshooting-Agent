"""Patch artifact generation.

Produces reviewable change artifacts in the formats teams actually apply
changes through. Nothing here executes: every result is text plus the command a
human would run after reviewing it.

On GitOps formats — ArgoCD and Flux both reconcile plain manifests and Kustomize
overlays from git. Emitting tool-specific wrappers would add ceremony without
adding information, so the YAML manifest and Kustomize overlay are the artifacts
for both, and are labelled as such.
"""

import json
from typing import Any

import yaml

from app.evidence.models import ResourceRef
from app.remediation.models import Patch, PatchFormat


def _yaml(payload: Any) -> str:
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False).strip()


def _kind_path(target: ResourceRef) -> str:
    return f"{target.kind.lower()}/{target.name}"


def _namespace_flag(target: ResourceRef) -> str:
    return f" -n {target.namespace}" if target.namespace else ""


def kubectl_patch(
    target: ResourceRef,
    patch: dict[str, Any],
    description: str,
    patch_type: str = "strategic",
) -> Patch:
    """A `kubectl patch` command, with a server-side dry run offered first."""
    payload = json.dumps(patch, separators=(",", ":"))
    base = (
        f"kubectl patch {target.kind.lower()} {target.name}"
        f"{_namespace_flag(target)} --type={patch_type} -p '{payload}'"
    )

    return Patch(
        format=PatchFormat.KUBECTL,
        filename=f"{target.name}-patch.json",
        content=json.dumps(patch, indent=2),
        description=description,
        # Dry run first so the change is validated by the API server before it lands.
        apply_command=f"{base} --dry-run=server\n{base}",
    )


def manifest(obj: dict[str, Any], filename: str, description: str) -> Patch:
    """A complete manifest, suitable for `kubectl apply` or a GitOps repository."""
    namespace = obj.get("metadata", {}).get("namespace")
    flag = f" -n {namespace}" if namespace else ""

    return Patch(
        format=PatchFormat.YAML_MANIFEST,
        filename=filename,
        content=_yaml(obj),
        description=description,
        apply_command=(
            f"kubectl apply -f {filename}{flag} --dry-run=server\nkubectl apply -f {filename}{flag}"
        ),
    )


def helm_values(values: dict[str, Any], description: str, release: str = "<release>") -> Patch:
    """A values fragment.

    Chart structures differ, so the path is a convention rather than a fact; the
    caveat is stated in the description instead of implying false precision.
    """
    return Patch(
        format=PatchFormat.HELM_VALUES,
        filename="values-override.yaml",
        content=_yaml(values),
        description=(
            f"{description} Verify the value path matches your chart — this uses the "
            f"conventional layout, which not every chart follows."
        ),
        apply_command=(
            f"helm upgrade {release} <chart> -f values-override.yaml --dry-run\n"
            f"helm upgrade {release} <chart> -f values-override.yaml"
        ),
    )


def kustomize_patch(
    target: ResourceRef,
    patch: dict[str, Any],
    description: str,
) -> Patch:
    """A strategic-merge overlay plus the kustomization entry that selects it.

    This is also the artifact to commit for ArgoCD or Flux.
    """
    body = dict(patch)
    body.setdefault("apiVersion", _api_version(target.kind))
    body.setdefault("kind", target.kind)
    body.setdefault("metadata", {"name": target.name})
    if target.namespace:
        body["metadata"]["namespace"] = target.namespace

    filename = f"patch-{target.name}.yaml"
    kustomization = _yaml({"patches": [{"path": filename, "target": _kustomize_target(target)}]})

    return Patch(
        format=PatchFormat.KUSTOMIZE,
        filename=filename,
        content=f"{_yaml(body)}\n\n---\n# Add to kustomization.yaml:\n{kustomization}",
        description=(f"{description} Commit this overlay for ArgoCD or Flux to reconcile."),
        apply_command="kubectl kustomize . | kubectl apply -f - --dry-run=server",
    )


def _kustomize_target(target: ResourceRef) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": target.kind, "name": target.name}
    if target.namespace:
        entry["namespace"] = target.namespace
    return entry


_API_VERSIONS = {
    "Deployment": "apps/v1",
    "StatefulSet": "apps/v1",
    "DaemonSet": "apps/v1",
    "Pod": "v1",
    "Service": "v1",
    "ConfigMap": "v1",
    "Secret": "v1",
    "ResourceQuota": "v1",
    "PersistentVolumeClaim": "v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "StorageClass": "storage.k8s.io/v1",
}


def _api_version(kind: str) -> str:
    return _API_VERSIONS.get(kind, "v1")


def container_patch(container: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Strategic-merge patch scoped to one container.

    Kubernetes merges the containers list by name, so naming the container is
    what keeps this from replacing the whole list.
    """
    return {"spec": {"template": {"spec": {"containers": [{"name": container, **changes}]}}}}
