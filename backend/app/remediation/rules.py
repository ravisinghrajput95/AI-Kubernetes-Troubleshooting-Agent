"""Remediation rules, one per hypothesis.

Each rule turns a hypothesis into a concrete plan: what to check first, what to
change, how to confirm it worked, and how to undo it. Rules are registered by
hypothesis id, so adding a failure mode means adding a rule — there is no
dispatcher to edit.
"""

import re
from typing import Any, Protocol

from app.analysis.models import SignalType
from app.evidence.models import ResourceRef
from app.remediation import patches
from app.remediation.context import RemediationContext
from app.remediation.models import (
    ChangeKind,
    Patch,
    Permission,
    RemediationPlan,
    RemediationRisk,
    RemediationStep,
    RiskLevel,
)

# Kubernetes quantity suffixes: binary (Ki, Mi, …) must be matched before the
# single-letter decimal ones, and `m` (milli) is lowercase while `M` (mega) is not.
QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)((?:[KMGTPE]i)|[numkKMGTPE])?$")


class RemediationRule(Protocol):
    hypothesis_id: str

    def build(self, context: RemediationContext) -> RemediationPlan: ...


def double_quantity(value: str) -> str | None:
    """Double a Kubernetes quantity, preserving its unit."""
    match = QUANTITY.match(str(value).strip())
    if not match:
        return None
    amount, unit = match.groups()
    doubled = int(float(amount) * 2)
    return f"{doubled}{unit or ''}"


def _workload_permissions(target: ResourceRef, verbs=("get", "patch")) -> tuple[Permission, ...]:
    return (
        Permission(
            verbs=verbs,
            resources=(f"{target.kind.lower()}s",),
            namespace=target.namespace,
        ),
    )


# Only these carry revision history, so only these can be rolled out or undone.
# A bare Pod has no controller: `rollout restart/undo/status` fail against it.
ROLLABLE_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})


def _rollable(target: ResourceRef) -> bool:
    return target.kind in ROLLABLE_KINDS


def _rollout_verification(target: ResourceRef) -> tuple[RemediationStep, ...]:
    flag = f" -n {target.namespace}" if target.namespace else ""
    kind = target.kind.lower()

    if not _rollable(target):
        return (
            RemediationStep(
                f"Confirm {kind} {target.name} is running and no longer restarting.",
                f"kubectl get {kind} {target.name}{flag} -o wide",
            ),
            RemediationStep(
                "Confirm the container's restart count has stopped increasing.",
                f"kubectl describe {kind} {target.name}{flag}",
            ),
        )

    return (
        RemediationStep(
            "Watch the rollout reach completion.",
            f"kubectl rollout status {kind} {target.name}{flag} --timeout=300s",
        ),
        RemediationStep(
            "Confirm the pods are running and no longer restarting.",
            f"kubectl get pods{flag} -l app={target.name} -o wide",
        ),
    )


def _rollout_undo(target: ResourceRef) -> tuple[RemediationStep, ...]:
    flag = f" -n {target.namespace}" if target.namespace else ""
    kind = target.kind.lower()

    if not _rollable(target):
        return (
            RemediationStep(
                f"This {kind} is not managed by a controller, so it has no revision "
                f"history to roll back to. Restore the previous definition from source "
                f"control and re-apply it.",
                None,
                manual=True,
            ),
        )

    return (
        RemediationStep(
            "Review the revision history before rolling back.",
            f"kubectl rollout history {kind} {target.name}{flag}",
        ),
        RemediationStep(
            "Roll back to the previous revision.",
            f"kubectl rollout undo {kind} {target.name}{flag}",
        ),
    )


def _restart_step(target: ResourceRef) -> RemediationStep:
    """Restart the workload so it picks up new configuration."""
    flag = f" -n {target.namespace}" if target.namespace else ""

    if not _rollable(target):
        return RemediationStep(
            f"Recreate {target.kind.lower()} {target.name} from its manifest so it "
            f"picks up the new configuration. It has no controller, so deleting it "
            f"will not bring it back on its own.",
            None,
            manual=True,
        )

    return RemediationStep(
        "Restart the workload so it picks up the new configuration.",
        f"kubectl rollout restart {target.kind.lower()} {target.name}{flag}",
    )


def _unmanaged_caveat(target: ResourceRef) -> tuple[str, ...]:
    if _rollable(target):
        return ()
    return (
        f"No controller owns {target.kind.lower()}/{target.name}, so this change "
        f"cannot be rolled out or undone automatically. Prefer changing the workload "
        f"that should own it.",
    )


def _capture_current(target: ResourceRef) -> RemediationStep:
    flag = f" -n {target.namespace}" if target.namespace else ""
    return RemediationStep(
        "Capture the current manifest so the change can be reverted exactly.",
        f"kubectl get {target.kind.lower()} {target.name}{flag} -o yaml > "
        f"{target.name}-before.yaml",
    )


def _derived_caveats(context: RemediationContext) -> tuple[str, ...]:
    if context.workload_derived():
        return (
            "The owning Deployment name was derived from the ReplicaSet name; "
            "confirm it before applying.",
        )
    return ()


class OutOfMemoryRule:
    hypothesis_id = "workload.out_of_memory"

    def build(self, context: RemediationContext) -> RemediationPlan:
        workload = context.workload_ref()
        container = context.container() or {}
        name = container.get("name", "<container>")
        current = (container.get("limits") or {}).get("memory")
        proposed = double_quantity(current) if current else None

        if current and proposed:
            summary = (
                f"Container {name} was OOM-killed against a {current} memory limit. "
                f"Raising it to {proposed} is a starting point; size it from observed "
                f"usage rather than doubling indefinitely."
            )
            change: dict[str, Any] = {"resources": {"limits": {"memory": proposed}}}
            caveats: tuple[str, ...] = (
                "Doubling is a heuristic. If the container leaks memory, a higher "
                "limit delays the failure rather than fixing it.",
            )
        else:
            summary = (
                f"Container {name} was OOM-killed and declares no memory limit, so it "
                f"competes for node memory and is evicted under pressure. Set a limit "
                f"sized from observed usage."
            )
            change = {"resources": {"limits": {"memory": "<size-from-observed-usage>"}}}
            caveats = (
                "No current limit was recorded, so no value can be proposed from "
                "evidence. Size it from the container's working set before applying.",
            )

        patch_body = patches.container_patch(name, change)

        return RemediationPlan(
            id="raise-memory-limit",
            hypothesis_id=self.hypothesis_id,
            title="Raise the container memory limit",
            summary=summary,
            target=workload,
            risk=RemediationRisk(
                level=RiskLevel.MEDIUM,
                change_kind=ChangeKind.RESOURCE_LIMITS,
                restart_required=True,
                estimated_downtime="Rolling restart; brief per-pod unavailability",
                blast_radius=f"All pods of {workload.kind.lower()}/{workload.name}",
                reversible=True,
                notes=("A higher limit increases the node memory this workload can claim.",),
            ),
            requires_approval=True,
            preconditions=(
                _capture_current(workload),
                RemediationStep(
                    "Confirm the node has memory headroom for the new limit.",
                    "kubectl top nodes",
                ),
            ),
            remediation=(
                RemediationStep(
                    f"Raise the memory limit for container {name}.",
                    patches.kubectl_patch(workload, patch_body, "").apply_command,
                ),
            ),
            verification=_rollout_verification(workload),
            rollback=_rollout_undo(workload),
            required_permissions=_workload_permissions(workload),
            patches=(
                patches.kubectl_patch(workload, patch_body, f"Raise the memory limit for {name}."),
                patches.kustomize_patch(
                    workload, patch_body, f"Raise the memory limit for {name}."
                ),
                patches.helm_values(
                    {"resources": {"limits": {"memory": proposed or "<size>"}}},
                    "Raise the container memory limit.",
                ),
            ),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=caveats + _derived_caveats(context) + _unmanaged_caveat(context.workload_ref()),
        )


class MissingConfigurationRule:
    hypothesis_id = "workload.missing_configuration"

    def build(self, context: RemediationContext) -> RemediationPlan:
        signal = context.first(SignalType.CONFIG_KEY_MISSING, SignalType.CONFIG_REFERENCE_MISSING)
        attributes = signal.attributes if signal else {}

        # Whether evidence actually named the object, rather than this rule
        # falling back to its defaults.
        #
        # The hypothesis can fire from `pod.config_error` alone — a pod in
        # CreateContainerConfigError — which carries the pod and the namespace
        # and nothing about *what* it references. Both values below then
        # defaulted silently, and the plan asserted "ConfigMap payments/<name>
        # is referenced by the pod but does not exist" as fact, generated a
        # `<name>-configmap.yaml` manifest containing `name: <name>`, and
        # handed the operator `kubectl get configmap <name> -n payments`. The
        # `kind` was a guess too, and a wrong one costs the Secret branch its
        # "values are never generated" note.
        #
        # `MemoryLimitRule` already had the shape for this: when the evidence
        # is absent it says so in the summary, uses a placeholder that names
        # what is missing rather than the thing itself, and carries a caveat.
        identified = bool(attributes.get("name"))
        kind = attributes.get("kind", "ConfigMap")
        name = attributes.get("name") or "<name-from-the-pod-spec>"
        missing_keys = attributes.get("missing_keys") or []
        namespace = context.namespace or "<namespace>"
        exists = signal is not None and signal.type == SignalType.CONFIG_KEY_MISSING

        ref = ResourceRef(kind=kind, name=name, namespace=context.namespace)
        flag = f" -n {namespace}"

        if exists and missing_keys:
            keys = ", ".join(missing_keys)
            summary = (
                f"{kind} {namespace}/{name} exists but is missing the key(s) the "
                f"container reads: {keys}. The container cannot start until they resolve."
            )
            title = f"Add the missing key(s) to {kind} {name}"
        elif identified:
            summary = (
                f"{kind} {namespace}/{name} is referenced by the pod but does not exist "
                f"in the namespace. Create it, or correct the reference in the pod template."
            )
            title = f"Create the missing {kind} {name}"
        else:
            summary = (
                "The pod cannot start because a referenced ConfigMap or Secret is "
                "missing or incomplete, but no collected evidence names which one. "
                "Read the pod's `envFrom`, `env.valueFrom` and volume references to "
                "identify it, then create or correct that object."
            )
            title = "Identify the configuration the pod references"

        # Nothing is generated for an object this rule cannot name. A manifest
        # built around a placeholder is not appliable — `<name>` is not a legal
        # Kubernetes name — and offering one implies the platform knows which
        # object to create when it does not. Same refusal as the Secret values
        # below and the memory limit that has no observed current value.
        generated: tuple[Patch, ...] = ()
        if kind == "ConfigMap" and identified:
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace},
                "data": dict.fromkeys(missing_keys, "<value>") or {"<key>": "<value>"},
            }
            generated = (
                patches.manifest(
                    body, f"{name}-configmap.yaml", f"Supply the missing {kind} key(s)."
                ),
            )

        # Secret values are never generated: the platform does not read them and
        # must not invent them.
        unidentified_note: tuple[str, ...] = (
            (
                "No collected evidence named the referenced object, so the kind and "
                "name below are placeholders rather than findings, no manifest is "
                "generated, and the commands need the real name substituted. Read the "
                "pod spec to identify it.",
            )
            if not identified
            else ()
        )

        secret_note: tuple[str, ...] = (
            (
                "Secret values are deliberately not generated. Create the key with "
                "your own value; the platform never reads or writes secret contents.",
            )
            if kind == "Secret"
            else ()
        )

        return RemediationPlan(
            id="supply-missing-configuration",
            hypothesis_id=self.hypothesis_id,
            title=title,
            summary=summary,
            target=ref,
            risk=RemediationRisk(
                level=RiskLevel.LOW,
                change_kind=ChangeKind.CONFIG,
                restart_required=True,
                estimated_downtime="None; the pod is already failing",
                blast_radius=f"Workloads referencing {kind}/{name} in {namespace}",
                reversible=True,
                notes=("Adding a key affects every workload that mounts this object.",),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    f"Confirm whether {kind} {name} exists and what it contains.",
                    f"kubectl describe {kind.lower()} {name}{flag}"
                    if kind == "Secret"
                    else f"kubectl get configmap {name}{flag} -o json",
                ),
                RemediationStep(
                    "Confirm the value the application expects.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    f"Create or update {kind} {name} with the required key(s).",
                    f"kubectl create {kind.lower()} {name}{flag} "
                    f"--from-literal=<key>=<value> --dry-run=client -o yaml | "
                    f"kubectl apply -f -",
                ),
                _restart_step(context.workload_ref()),
            ),
            verification=(
                RemediationStep(
                    "Confirm the key is now present.",
                    f"kubectl describe {kind.lower()} {name}{flag}",
                ),
                *_rollout_verification(context.workload_ref()),
            ),
            rollback=_rollout_undo(context.workload_ref()),
            required_permissions=(
                Permission(("get", "create", "patch"), (f"{kind.lower()}s",), context.namespace),
                *_workload_permissions(context.workload_ref()),
            ),
            patches=generated,
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=secret_note
            + unidentified_note
            + _derived_caveats(context)
            + _unmanaged_caveat(context.workload_ref()),
        )


class ImagePullRule:
    hypothesis_id = "image.pull_failure"

    def build(self, context: RemediationContext) -> RemediationPlan:
        workload = context.workload_ref()
        container = context.container() or {}
        image = container.get("image", "<image>")
        unauthorized = context.first(SignalType.IMAGE_PULL_UNAUTHORIZED)
        not_found = context.first(SignalType.IMAGE_NOT_FOUND)
        no_secret = context.first(SignalType.IMAGE_NO_PULL_SECRET)

        if unauthorized or no_secret:
            title = "Attach registry credentials to the workload"
            summary = f"The registry rejected the pull of {image} as unauthorized" + (
                " and the pod has no imagePullSecrets, nor does its service account."
                if no_secret
                else "."
            )
            change: dict[str, Any] = {
                "spec": {"template": {"spec": {"imagePullSecrets": [{"name": "<secret>"}]}}}
            }
            step = RemediationStep(
                "Create a registry credential and attach it to the workload.",
                "kubectl create secret docker-registry <secret> "
                "--docker-server=<registry> --docker-username=<user> "
                "--docker-password=<password>"
                + (f" -n {context.namespace}" if context.namespace else ""),
            )
        elif not_found:
            title = "Correct the image reference"
            summary = (
                f"The registry reports that {image} does not exist. The tag was likely "
                f"never pushed, or was deleted after the manifest was written."
            )
            change = {}
            step = RemediationStep(
                "Set an image tag that exists in the registry.",
                None,
                manual=True,
            )
        else:
            title = "Resolve the image pull failure"
            summary = (
                f"The kubelet could not pull {image}. The pull error distinguishes a "
                f"wrong reference from a credential problem."
            )
            change = {}
            step = RemediationStep(
                "Read the pull error and decide between reference and credentials.",
                None,
                manual=True,
            )

        generated: tuple[Patch, ...] = ()
        if change:
            generated = (
                patches.kubectl_patch(workload, change, "Attach an imagePullSecret."),
                patches.kustomize_patch(workload, change, "Attach an imagePullSecret."),
            )

        return RemediationPlan(
            id="fix-image-pull",
            hypothesis_id=self.hypothesis_id,
            title=title,
            summary=summary,
            target=workload,
            risk=RemediationRisk(
                level=RiskLevel.LOW,
                change_kind=ChangeKind.IMAGE,
                restart_required=True,
                estimated_downtime="None; the pods are already not running",
                blast_radius=f"All pods of {workload.kind.lower()}/{workload.name}",
                reversible=True,
            ),
            requires_approval=True,
            preconditions=(
                _capture_current(workload),
                RemediationStep(
                    "Read the exact pull error from the pod's events.",
                    f"kubectl describe pod {context.target.name}"
                    + (f" -n {context.namespace}" if context.namespace else ""),
                ),
            ),
            remediation=(step,),
            verification=_rollout_verification(workload),
            rollback=_rollout_undo(workload),
            required_permissions=(
                Permission(("get", "create"), ("secrets",), context.namespace),
                *_workload_permissions(workload),
            ),
            patches=generated,
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=_derived_caveats(context) + _unmanaged_caveat(context.workload_ref()),
        )


class QuotaExhaustedRule:
    hypothesis_id = "scheduling.quota_exhausted"

    def build(self, context: RemediationContext) -> RemediationPlan:
        signal = context.first(SignalType.QUOTA_EXCEEDED)
        quota_name = (signal.attributes.get("quota") if signal else None) or "<quota>"
        exhausted = (signal.attributes.get("exhausted") if signal else None) or []
        namespace = context.namespace or "<namespace>"
        ref = ResourceRef(kind="ResourceQuota", name=quota_name, namespace=context.namespace)

        return RemediationPlan(
            id="raise-resource-quota",
            hypothesis_id=self.hypothesis_id,
            title=f"Raise or free the {quota_name} quota",
            summary=(
                f"ResourceQuota {namespace}/{quota_name} is fully consumed for "
                f"{', '.join(exhausted) or 'one or more resources'}, so new pods are "
                f"rejected before scheduling. Either raise the quota or reduce what the "
                f"namespace already requests."
            ),
            target=ref,
            risk=RemediationRisk(
                level=RiskLevel.MEDIUM,
                change_kind=ChangeKind.QUOTA,
                restart_required=False,
                estimated_downtime="None",
                blast_radius=f"Every workload in namespace {namespace}",
                reversible=True,
                notes=(
                    "Raising a quota permits more cluster resource consumption; confirm "
                    "capacity exists before increasing it.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Review current quota usage against its limits.",
                    f"kubectl describe resourcequota {quota_name} -n {namespace}",
                ),
                RemediationStep(
                    "Identify which workloads consume the quota.",
                    f"kubectl get pods -n {namespace} -o custom-columns="
                    f"NAME:.metadata.name,CPU:.spec.containers[*].resources.requests.cpu,"
                    f"MEM:.spec.containers[*].resources.requests.memory",
                ),
                RemediationStep(
                    "Decide whether the quota or the requests should change.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    "Raise the exhausted quota values.",
                    f"kubectl edit resourcequota {quota_name} -n {namespace}",
                ),
            ),
            verification=(
                RemediationStep(
                    "Confirm headroom now exists.",
                    f"kubectl describe resourcequota {quota_name} -n {namespace}",
                ),
                RemediationStep(
                    "Confirm the pending pods schedule.",
                    f"kubectl get pods -n {namespace} --field-selector=status.phase=Pending",
                ),
            ),
            rollback=(
                RemediationStep(
                    "Restore the previous quota values from the captured manifest.",
                    f"kubectl apply -f {quota_name}-before.yaml",
                ),
            ),
            required_permissions=(
                Permission(("get", "patch"), ("resourcequotas",), context.namespace),
            ),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
        )


class DefaultStorageClassRule:
    hypothesis_id = "storage.no_default_storage_class"

    def build(self, context: RemediationContext) -> RemediationPlan:
        signal = context.first(SignalType.STORAGE_NO_DEFAULT_CLASS)
        classes = (signal.attributes.get("classes") if signal else None) or []
        candidate = classes[0] if classes else "<storageclass>"
        ref = ResourceRef(kind="StorageClass", name=candidate)

        annotation = {
            "metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}
        }

        return RemediationPlan(
            id="set-default-storage-class",
            hypothesis_id=self.hypothesis_id,
            title=f"Mark StorageClass {candidate} as default",
            summary=(
                "No StorageClass is marked default, so a claim that does not name one "
                "will never be provisioned. Either mark a class default, or set "
                "storageClassName explicitly on the claim."
            ),
            target=ref,
            risk=RemediationRisk(
                level=RiskLevel.HIGH,
                change_kind=ChangeKind.STORAGE,
                restart_required=False,
                estimated_downtime="None",
                blast_radius="Cluster-wide: affects every future claim without an explicit class",
                reversible=True,
                notes=(
                    "This is a cluster-scoped default. Every subsequent claim without "
                    "an explicit storageClassName will use it.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Review the available storage classes and their provisioners.",
                    "kubectl get storageclass -o wide",
                ),
                RemediationStep(
                    "Confirm which class should be the cluster default.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    f"Mark {candidate} as the default storage class.",
                    patches.kubectl_patch(ref, annotation, "").apply_command,
                ),
            ),
            verification=(
                RemediationStep(
                    "Confirm exactly one class is marked default.",
                    "kubectl get storageclass",
                ),
                RemediationStep(
                    "Confirm the pending claim binds.",
                    f"kubectl get pvc -n {context.namespace or '<namespace>'}",
                ),
            ),
            rollback=(
                RemediationStep(
                    "Remove the default annotation.",
                    f"kubectl patch storageclass {candidate} --type=merge -p "
                    f'\'{{"metadata":{{"annotations":'
                    f'{{"storageclass.kubernetes.io/is-default-class":"false"}}}}}}\'',
                ),
            ),
            required_permissions=(Permission(("get", "patch"), ("storageclasses",)),),
            patches=(
                patches.kubectl_patch(
                    ref, annotation, f"Mark {candidate} as the default storage class."
                ),
            ),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
        )


class ServiceEndpointsRule:
    hypothesis_id = "network.service_without_endpoints"

    def build(self, context: RemediationContext) -> RemediationPlan:
        service = context.target
        namespace = context.namespace or "<namespace>"
        flag = f" -n {namespace}"

        return RemediationPlan(
            id="align-service-selector",
            hypothesis_id=self.hypothesis_id,
            title=f"Restore endpoints for service {service.name}",
            summary=(
                f"Service {namespace}/{service.name} resolves but routes to no ready "
                f"pods. Either its selector matches nothing, or the pods it selects are "
                f"failing readiness. Confirm which before changing anything."
            ),
            target=service,
            risk=RemediationRisk(
                level=RiskLevel.MEDIUM,
                change_kind=ChangeKind.CONFIG,
                restart_required=False,
                estimated_downtime="None if the selector is corrected; traffic is already failing",
                blast_radius=f"All clients of service {namespace}/{service.name}",
                reversible=True,
                notes=(
                    "Changing a selector reroutes live traffic. Confirm the intended "
                    "backend pods first.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Read the service's current selector.",
                    f"kubectl get service {service.name}{flag} -o jsonpath='{{.spec.selector}}'",
                ),
                RemediationStep(
                    "Compare it against the labels on the intended pods.",
                    f"kubectl get pods{flag} --show-labels",
                ),
                RemediationStep(
                    "Check whether matching pods exist but are not ready.",
                    f"kubectl get endpointslices{flag} -o wide",
                ),
            ),
            remediation=(
                RemediationStep(
                    "If the selector is wrong, correct it to match the intended pods.",
                    f"kubectl edit service {service.name}{flag}",
                ),
                RemediationStep(
                    "If the pods are unready instead, fix readiness rather than the selector.",
                    None,
                    manual=True,
                ),
            ),
            verification=(
                RemediationStep(
                    "Confirm the service now has ready endpoints.",
                    f"kubectl get endpoints {service.name}{flag}",
                ),
            ),
            rollback=(
                RemediationStep(
                    "Restore the previous service definition.",
                    f"kubectl apply -f {service.name}-before.yaml",
                ),
            ),
            required_permissions=(Permission(("get", "patch"), ("services",), context.namespace),),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
        )


class NetworkPolicyRule:
    hypothesis_id = "network.ingress_denied_by_policy"

    def build(self, context: RemediationContext) -> RemediationPlan:
        policy = context.target
        namespace = context.namespace or "<namespace>"

        body = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"allow-{policy.name}-ingress", "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": {"app": "<app>"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"namespaceSelector": {"matchLabels": {"name": "<source-namespace>"}}}
                        ]
                    }
                ],
            },
        }

        return RemediationPlan(
            id="permit-required-ingress",
            hypothesis_id=self.hypothesis_id,
            title="Permit the required ingress traffic",
            summary=(
                f"NetworkPolicy {namespace}/{policy.name} denies all ingress and no rule "
                f"admits the expected traffic. Add a rule scoped to the sources that "
                f"legitimately need access — do not remove the default-deny."
            ),
            target=policy,
            risk=RemediationRisk(
                level=RiskLevel.HIGH,
                change_kind=ChangeKind.NETWORK_POLICY,
                restart_required=False,
                estimated_downtime="None",
                blast_radius=f"Network reachability for selected pods in {namespace}",
                reversible=True,
                notes=(
                    "This widens network access. Scope the rule to the specific sources "
                    "that need it rather than deleting the default-deny policy.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Review the policies currently selecting these pods.",
                    f"kubectl get networkpolicy -n {namespace} -o yaml",
                ),
                RemediationStep(
                    "Establish which sources are expected to reach this service.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    "Apply a scoped ingress allowance.",
                    "kubectl apply -f allow-ingress.yaml --dry-run=server",
                ),
            ),
            verification=(
                RemediationStep(
                    "Confirm the policy is in effect.",
                    f"kubectl describe networkpolicy -n {namespace}",
                ),
                RemediationStep(
                    "Confirm traffic now reaches the service from an expected source.",
                    None,
                    manual=True,
                ),
            ),
            rollback=(
                RemediationStep(
                    "Remove the added allowance.",
                    f"kubectl delete networkpolicy allow-{policy.name}-ingress -n {namespace}",
                ),
            ),
            required_permissions=(
                Permission(("get", "create", "delete"), ("networkpolicies",), context.namespace),
            ),
            patches=(patches.manifest(body, "allow-ingress.yaml", "Scoped ingress allowance."),),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=(
                "The selectors in this manifest are placeholders — the platform cannot "
                "infer which sources should be permitted.",
            ),
        )


class RolloutStalledRule:
    hypothesis_id = "rollout.stalled"

    def build(self, context: RemediationContext) -> RemediationPlan:
        workload = context.workload_ref()
        flag = f" -n {workload.namespace}" if workload.namespace else ""

        return RemediationPlan(
            id="roll-back-deployment",
            hypothesis_id=self.hypothesis_id,
            title=f"Roll back {workload.kind.lower()}/{workload.name}",
            summary=(
                f"The rollout of {workload.name} cannot reach its desired replica count. "
                f"Rolling back to the last healthy revision restores service while the "
                f"underlying failure is investigated."
            ),
            target=workload,
            risk=RemediationRisk(
                level=RiskLevel.MEDIUM,
                change_kind=ChangeKind.ROLLOUT,
                restart_required=True,
                estimated_downtime="Rolling; the previous revision is restored progressively",
                blast_radius=f"All pods of {workload.kind.lower()}/{workload.name}",
                reversible=True,
                notes=("Rolling back reverts application code as well as configuration.",),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Identify the revision to roll back to.",
                    f"kubectl rollout history {workload.kind.lower()} {workload.name}{flag}",
                ),
                RemediationStep(
                    "Capture why the new revision failed, before it is replaced.",
                    f"kubectl describe {workload.kind.lower()} {workload.name}{flag}",
                ),
            ),
            remediation=(
                RemediationStep(
                    "Roll back to the previous revision.",
                    f"kubectl rollout undo {workload.kind.lower()} {workload.name}{flag}",
                ),
            ),
            verification=_rollout_verification(workload),
            rollback=(
                RemediationStep(
                    "Re-apply the newer revision once the failure is fixed.",
                    f"kubectl rollout undo {workload.kind.lower()} {workload.name}{flag} "
                    f"--to-revision=<revision>",
                ),
            ),
            required_permissions=_workload_permissions(workload, ("get", "patch", "update")),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=_derived_caveats(context) + _unmanaged_caveat(context.workload_ref()),
        )


class ProbeRule:
    hypothesis_id = "probe.failing"

    def build(self, context: RemediationContext) -> RemediationPlan:
        workload = context.workload_ref()
        container = context.container() or {}
        name = container.get("name", "<container>")
        probes = container.get("probes", {})
        liveness = probes.get("liveness", {})
        delay = liveness.get("initial_delay_seconds", 0)

        change = patches.container_patch(
            name,
            {
                "startupProbe": {
                    "httpGet": {
                        "path": liveness.get("path") or "/healthz",
                        "port": liveness.get("port") or 8080,
                    },
                    "failureThreshold": 30,
                    "periodSeconds": 10,
                }
            },
        )

        return RemediationPlan(
            id="adjust-health-probe",
            hypothesis_id=self.hypothesis_id,
            title="Give the container time to start before liveness applies",
            summary=(
                f"Container {name} has a liveness probe starting after {delay}s and no "
                f"startup probe, so a slow start is treated as a failure and the "
                f"container is restarted before it can become healthy. A startup probe "
                f"separates 'still starting' from 'unhealthy'."
            ),
            target=workload,
            risk=RemediationRisk(
                level=RiskLevel.LOW,
                change_kind=ChangeKind.CONFIG,
                restart_required=True,
                estimated_downtime="Rolling restart",
                blast_radius=f"All pods of {workload.kind.lower()}/{workload.name}",
                reversible=True,
                notes=(
                    "A startup probe delays liveness enforcement; it does not mask a "
                    "genuinely unhealthy container.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                _capture_current(workload),
                RemediationStep(
                    "Confirm the application is slow to start rather than broken.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    f"Add a startup probe to container {name}.",
                    patches.kubectl_patch(workload, change, "").apply_command,
                ),
            ),
            verification=_rollout_verification(workload),
            rollback=_rollout_undo(workload),
            required_permissions=_workload_permissions(workload),
            patches=(
                patches.kubectl_patch(workload, change, "Add a startup probe."),
                patches.kustomize_patch(workload, change, "Add a startup probe."),
            ),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=_derived_caveats(context) + _unmanaged_caveat(context.workload_ref()),
        )


class NodeUnhealthyRule:
    hypothesis_id = "node.unhealthy"

    def build(self, context: RemediationContext) -> RemediationPlan:
        node = context.target

        return RemediationPlan(
            id="drain-unhealthy-node",
            hypothesis_id=self.hypothesis_id,
            title=f"Cordon and drain node {node.name}",
            summary=(
                f"Node {node.name} is not Ready or is under resource pressure, so pods "
                f"scheduled to it cannot run reliably. Cordon it to stop new placement, "
                f"then drain to move existing workloads."
            ),
            target=node,
            risk=RemediationRisk(
                level=RiskLevel.CRITICAL,
                change_kind=ChangeKind.INFRASTRUCTURE,
                restart_required=True,
                estimated_downtime="Every pod on the node is rescheduled",
                blast_radius=f"All workloads currently running on {node.name}",
                reversible=True,
                notes=(
                    "Draining evicts running pods. Confirm the remaining nodes have "
                    "capacity, or the evicted pods will simply go Pending.",
                    "Respect PodDisruptionBudgets; a drain that ignores them can take a "
                    "service fully offline.",
                ),
            ),
            requires_approval=True,
            preconditions=(
                RemediationStep(
                    "Confirm remaining capacity can absorb the evicted pods.",
                    "kubectl top nodes",
                ),
                RemediationStep(
                    "List what is currently running on the node.",
                    f"kubectl get pods -A --field-selector spec.nodeName={node.name}",
                ),
                RemediationStep(
                    "Review disruption budgets that the drain must respect.",
                    "kubectl get poddisruptionbudgets -A",
                ),
                RemediationStep(
                    "Obtain change approval; this is a cluster-level operation.",
                    None,
                    manual=True,
                ),
            ),
            remediation=(
                RemediationStep(
                    "Stop new pods being scheduled to the node.",
                    f"kubectl cordon {node.name}",
                ),
                RemediationStep(
                    "Move existing workloads off the node.",
                    f"kubectl drain {node.name} --ignore-daemonsets "
                    f"--delete-emptydir-data --dry-run=server",
                ),
            ),
            verification=(
                RemediationStep(
                    "Confirm the node is cordoned and empty.",
                    f"kubectl get node {node.name} -o wide",
                ),
                RemediationStep(
                    "Confirm the evicted pods are running elsewhere.",
                    "kubectl get pods -A --field-selector=status.phase=Pending",
                ),
            ),
            rollback=(
                RemediationStep(
                    "Return the node to service once it is healthy.",
                    f"kubectl uncordon {node.name}",
                ),
            ),
            required_permissions=(
                Permission(("get", "patch"), ("nodes",)),
                Permission(("create",), ("pods/eviction",)),
            ),
            signal_ids=context.hypothesis.supporting_signal_ids,
            evidence_ids=context.evidence_ids(),
            caveats=(
                "Draining a node is disruptive and is not reversible in the sense that "
                "evicted pods do not return to this node automatically.",
            ),
        )


DEFAULT_REMEDIATION_RULES: tuple[RemediationRule, ...] = (
    OutOfMemoryRule(),
    MissingConfigurationRule(),
    ImagePullRule(),
    QuotaExhaustedRule(),
    DefaultStorageClassRule(),
    ServiceEndpointsRule(),
    NetworkPolicyRule(),
    RolloutStalledRule(),
    ProbeRule(),
    NodeUnhealthyRule(),
)
