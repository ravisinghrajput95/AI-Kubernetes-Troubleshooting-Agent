# Remediation and Patch Generation

Turning a diagnosis into a reviewable, reversible plan — and the change
artifacts to carry it out.

Builds on [REASONING_ARCHITECTURE.md](REASONING_ARCHITECTURE.md) and
[PLAYBOOKS.md](PLAYBOOKS.md).

## The platform never applies changes

This is structural, not a convention:

- `KubectlExecutor.run()` validates every command against a read-only verb
  allowlist. The commands a remediation plan produces — `patch`, `apply`,
  `rollout undo`, `drain` — are **rejected** by that policy.
- `tests/test_remediation_safety.py` asserts exactly this: for every rule, the
  mutating commands it generates raise `UnsafeKubectlCommand` when passed to the
  executor's policy. There is no code path by which the platform can run its own
  recommendation.
- Every generated patch offers a `--dry-run=server` invocation before the real
  one, and every change-bearing plan sets `requires_approval`.

The same tests assert the inverse for preconditions and verification steps:
those must pass the read-only policy, because an operator runs them before
committing to anything.

## Plans are derived from the hypothesis

Remediation is keyed on the hypothesis, not on investigation heuristics. The
hypothesis already knows what is wrong and on which resource, so the plan can
name the actual container, its current limits, and the workload that owns it.

```
Hypothesis  →  RemediationRule  →  RemediationPlan  →  Patches
(what is wrong)  (what to do)       (how, safely)      (artifacts)
```

Each plan is ordered the way an operator works:

| Section | Content | Safety |
|---|---|---|
| `preconditions` | Capture current state, confirm capacity, check permissions | Read-only |
| `remediation` | The actual change | Requires approval |
| `verification` | Confirm it worked | Read-only |
| `rollback` | Undo it | — |

Plus `risk`, `required_permissions`, `caveats`, and provenance (`signal_ids`,
`evidence_ids`) linking back to the evidence that justified it.

## Risk is stated, not implied

```python
RemediationRisk(
    level=RiskLevel.CRITICAL,
    change_kind=ChangeKind.INFRASTRUCTURE,
    restart_required=True,
    estimated_downtime="Every pod on the node is rescheduled",
    blast_radius="All workloads currently running on node-1",
    reversible=True,
    notes=("Draining evicts running pods. Confirm remaining capacity…",),
)
```

Risk reflects what the change actually does. Adding a missing ConfigMap key is
`Low`. Raising a memory limit is `Medium` — it restarts pods and claims more node
memory. Setting a default StorageClass is `High` because it is cluster-scoped and
silently affects every future claim. Draining a node is `Critical`.

`required_permissions` render as `kubectl auth can-i` checks, so an operator can
confirm access before starting rather than failing halfway through.

## Patch formats

| Format | Artifact |
|---|---|
| `kubectl` | Strategic-merge patch, with a server-side dry run first |
| `yaml` | Complete manifest for `kubectl apply` or a GitOps repository |
| `kustomize` | Overlay plus the kustomization entry that selects it |
| `helm-values` | Values fragment |

On ArgoCD and Flux: both reconcile plain manifests and Kustomize overlays from
git. Emitting tool-specific wrappers would add ceremony without information, so
the YAML manifest and Kustomize overlay **are** the artifacts for both, and are
labelled as such.

YAML is serialised with PyYAML rather than string formatting. These artifacts get
applied to production clusters; a quoting bug in a hand-rolled emitter is not a
risk worth taking for one saved dependency.

## What the platform refuses to generate

- **Secret values.** When a referenced Secret is missing, the plan says so and
  stops. It never invents a value, and never reads one — Secrets are inspected
  with `describe`, which prints key names only.
- **A memory limit with no evidence.** If the OOM-killed container declared no
  limit, there is nothing to scale from. The plan says so and asks for the value
  to be sized from observed usage rather than proposing a number.
- **Network policy selectors.** The platform cannot infer which sources should
  legitimately reach a service, so the generated manifest carries placeholders
  and a caveat saying exactly that.

## Two correctness details

**Remediation targets the controller, not the pod.** A pod's owner is usually a
ReplicaSet, but the resource an operator changes is the Deployment above it.
`PodSpecCollector` captures `ownerReferences`, and the Deployment name is derived
by dropping the ReplicaSet's pod-template-hash suffix. The derivation is
**flagged as a caveat** and the ReplicaSet name retained, so it can be verified
rather than trusted.

**Bare pods cannot be rolled out.** `rollout restart`, `rollout undo` and
`rollout status` fail against a Pod — only Deployments, StatefulSets and
DaemonSets carry revision history. When no controller owns the workload, the plan
substitutes pod-appropriate verification, makes rollback a manual step
("restore from source control"), and adds a caveat. Generating a command that
errors on first use would undermine trust in every other command in the plan.

## When no rule matches

Hypotheses without a specific rule get a **diagnostic plan**: read-only steps
built from the hypothesis's own `missing_evidence`, `ChangeKind.NONE`, and no
patches. Inventing a fix for a failure mode the platform does not model would be
worse than saying it does not have one.

A rule that raises degrades to the same diagnostic plan rather than losing the
diagnosis.

## Adding a rule

```python
class MyRule:
    hypothesis_id = "network.some_failure"

    def build(self, context: RemediationContext) -> RemediationPlan:
        workload = context.workload_ref()
        ...
```

Append it to `DEFAULT_REMEDIATION_RULES`. `RemediationContext` gives access to
the hypothesis, its supporting signals, the targeted evidence a playbook
collected, and the owning workload.

The safety tests are parameterised over every registered rule, so a new rule is
automatically held to the same guarantees: complete plan, verification present,
permissions declared, mutating commands rejected by the read-only policy, and
read-only preconditions.

## Backward compatibility

`remediation_risk` and `remediation_plan` keep their original shapes — they are
projected from the new plan via `to_legacy()` and `to_legacy_plan()`. The richer
`remediation` object and `patches` list are additive.

Both are computed deterministically from the hypothesis and **overwrite whatever
the model returned**, in line with the existing rule that remediation is never
model-authored.
