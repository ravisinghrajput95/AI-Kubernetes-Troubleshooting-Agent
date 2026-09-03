"""The `MAX_LIST_ITEMS` cap, in one place because it belongs to both providers.

F5's built half is a ceiling on how many objects one list read may retain, plus
a *record* that it happened — a truncation is a citable evidence gap, so an
investigation that saw the first 2,000 of 50,000 pods says so rather than
reading as a complete picture of a small cluster.

It lived in `KubectlExecutor`, which is the kubeconfig path and only the
kubeconfig path. `RemoteAgentProvider` carried a `_truncations` list that was
initialised and never appended to — it existed to satisfy the protocol — so
through an agent **no cap was applied at all** and `collection_limits.truncated`
came back `false` for a read that had never been bounded.

Measured with `MAX_LIST_ITEMS=3` against a ten-pod namespace: the kubeconfig
path reported `total_pods: 3` with four truncation records naming returned and
retained; the agent path reported `total_pods: 10`, `truncated: false`, and no
records. So the memory envelope the platform publishes did not hold on the
transport the platform is built around, and the same cluster investigated two
ways disagreed about how many pods it has.

It lives under `app/kubernetes/` rather than `app/providers/` for a mechanical
reason worth stating so nobody moves it back: `app/providers/__init__` imports
`local_kubectl`, which imports the executor, so an executor importing anything
from `app.providers` is a circular import. `app/kubernetes/__init__` is empty.

A pure function rather than a method on either provider, because two
implementations of one rule drift — the same argument that made the history
index call the renderer's derivations instead of repeating them, and that
`tests/test_metrics_parity.py` exists to enforce for `kubectl top`.

**What it bounds is the payload, not the spike.** Both callers cap a document
that has already been built in full — `json.loads` on the kubeconfig path, the
decoded protobuf payload on the agent path — which is exactly what
`docs/PRODUCTION_READINESS.md` records about F5's remaining half. Capping here
does not change that, and is not claimed to.
"""

from typing import Any


def cap_items(data: Any, command: str, limit: int) -> tuple[Any, dict[str, Any] | None, int]:
    """Cap a list response.

    Returns the (possibly capped) data, a truncation record when one happened,
    and the number of items the cluster actually returned.

    Anything that is not a list response passes through untouched, so the
    caller does not have to decide whether a read was a list — the shape does.
    """
    if not isinstance(data, dict):
        return data, None, 0

    items = data.get("items")
    if not isinstance(items, list):
        return data, None, 0

    total = len(items)
    if limit <= 0 or total <= limit:
        return data, None, total

    record = {"command": command, "returned": total, "retained": limit}
    return {**data, "items": items[:limit]}, record, total
