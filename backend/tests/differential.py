"""Telling churn apart from divergence in a two-provider comparison.

`tests/test_agent_transport.py` proves M4's exit criterion by running the
baseline collector graph twice against one cluster — once through an agent,
once through a kubeconfig — and comparing what each produced. Comparing
*values* is what makes that worth running: it is how the agent's `statusFor`
was caught mapping every 404 to `EMPTY`.

It is also why the suite could not tell a defect from a cluster that moved.
Two collections seconds apart see two different clusters, so a Deployment
mid-rollout reports `unavailable_replicas: 1` to one read and `0` to the
other and the comparison calls that a divergence, in the exact words it would
use for a real one. That reached CI: the required `integration-verify` job
failed on da5de44 with `k8s.deployments.unhealthy_deployments differs`, while
its 48 deployment checks all passed — the platform's own Deployment happened
to be replacing a pod between the two reads. A harness that cries wolf gets
skipped exactly like a flaky required job.

**A third read is what separates the two.** Collect through the first provider,
then the second, then the *first again*: any value that moved between the two
reads of one provider was moving in the cluster, and cannot be evidence that
the providers disagree. Values stable across the bracketing reads and different
in the middle one are a divergence, and still fail.

That bracket catches **every one-time change**, which is the arithmetic worth
stating. With samples at t1 < t2 < t3 and a value changing once at T: if T
falls anywhere inside [t1, t3] the two bracketing reads disagree and the path
is excluded; outside it, all three agree. Only a value that changes *and
changes back* between t1 and t3 can still be reported as a divergence — and
that is the same residue `scripts/provider_diff.py` handles by running twice
and asking whether the difference swaps sides.

**Excluding churn must not become excluding everything**, which is the failure
mode this file would otherwise introduce: on a cluster churning hard enough,
every path is unstable, nothing is compared, and the suite passes while
proving nothing. `Comparison.refusal()` is the guard, and it refuses rather
than skips — a run that tested nothing must not read as a run that found
nothing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# The fields that name *which* object a list item is about. Used to address a
# list item by identity rather than by position, so one pod appearing does not
# shift every path after it and make the whole list look like a divergence.
IDENTITY_FIELDS = ("kind", "namespace", "name", "service", "container", "node")

_MISSING = object()


def leaves(payload: Any) -> dict[str, Any]:
    """Flatten a payload to `{path: scalar}`.

    Dicts descend by key. Lists of objects are addressed by the identity of
    each item (`pod_inventory[churn-lab/web-0].phase`); anything else falls
    back to the index. An empty container is a leaf of its own, so "no
    findings" and "one finding" are different at the same path rather than
    silently absent from both sides.
    """
    out: dict[str, Any] = {}
    _walk(payload, "", out)
    return out


def _walk(node: Any, path: str, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        if not node:
            if path:
                out[path] = {}
            return
        for key in sorted(node):
            _walk(node[key], f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        if not node:
            out[path] = []
            return
        for label, item in _labelled(node):
            _walk(item, f"{path}[{label}]", out)
    else:
        out[path] = node


def _labelled(items: list[Any]) -> list[tuple[str, Any]]:
    """Pair each list item with a path label, by identity where there is one.

    Items sharing an identity — two findings about one Service — are ordered by
    their own content rather than by their position in the list, so a provider
    that returns the pair the other way round does not report both as changed.
    """
    out: list[tuple[str, Any]] = []
    groups: dict[str, list[Any]] = defaultdict(list)
    for index, item in enumerate(items):
        identity = _identity(item)
        if identity is None:
            out.append((str(index), item))
        else:
            groups[identity].append(item)

    for identity, group in groups.items():
        ordered = sorted(group, key=lambda item: json.dumps(item, sort_keys=True, default=str))
        for occurrence, item in enumerate(ordered, start=1):
            out.append((identity if occurrence == 1 else f"{identity}#{occurrence}", item))
    return out


def _identity(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    parts = [f"{name}={item[name]}" for name in IDENTITY_FIELDS if isinstance(item.get(name), str)]
    return "/".join(parts) or None


def unstable(first: Any, second: Any) -> set[str]:
    """Paths that moved between two reads through the *same* provider."""
    a, b = leaves(first), leaves(second)
    return {path for path in set(a) | set(b) if a.get(path, _MISSING) != b.get(path, _MISSING)}


@dataclass(frozen=True)
class Comparison:
    """What a bracketed comparison of two providers established."""

    divergences: list[str] = field(default_factory=list)
    churned: list[str] = field(default_factory=list)
    stable: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.divergences) + len(self.churned) + len(self.stable)

    def refusal(self) -> str | None:
        """Why this comparison established too little to be believed, if so.

        Silence is not a refusal: two providers that both found nothing agree,
        and a projection that is legitimately empty (no problematic pods on a
        healthy cluster) has nothing to churn. What is refused is a comparison
        where values *were* there and the cluster moved most of them, because
        that is indistinguishable from a passing run and is not one.
        """
        if self.total == 0:
            return None
        if not self.stable:
            return (
                f"every one of the {self.total} value(s) moved while they were being "
                f"compared, so nothing was actually held against anything"
            )
        if len(self.churned) * 2 > self.total:
            return (
                f"{len(self.churned)} of {self.total} values moved while they were being "
                f"compared; too little was stable to call this a comparison"
            )
        return None

    def report(self, limit: int = 12) -> str:
        lines = [f"{len(self.divergences)} divergence(s) that are not the cluster moving:"]
        lines += [f"  {path}" for path in self.divergences[:limit]]
        if len(self.divergences) > limit:
            lines.append(f"  ... and {len(self.divergences) - limit} more")
        lines.append(f"({len(self.stable)} value(s) agreed, {len(self.churned)} were churning)")
        return "\n".join(lines)


def compare(subject: Any, other: Any, control: Any) -> Comparison:
    """Compare two providers' payloads, discounting what the cluster moved.

    `subject` and `control` are the same provider read either side of `other`,
    so paths on which they disagree were moving in the cluster and are reported
    as churn rather than as a difference between the providers.
    """
    churning = unstable(subject, control)
    a, b = leaves(subject), leaves(other)

    divergences, stable, churned = [], [], []
    for path in sorted(set(a) | set(b)):
        if path in churning:
            churned.append(path)
        elif a.get(path, _MISSING) == b.get(path, _MISSING):
            stable.append(path)
        else:
            divergences.append(
                f"{path}: {a.get(path, '<absent>')!r} vs {b.get(path, '<absent>')!r}"
            )

    return Comparison(divergences=divergences, churned=churned, stable=stable)
