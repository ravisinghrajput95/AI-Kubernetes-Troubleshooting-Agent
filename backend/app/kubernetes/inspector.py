"""What an inspector is, now that it no longer fetches anything.

Until M5 each inspector held a `KubectlExecutor`, built argv, ran it, and
analysed the result — three jobs in one class, the middle of which was the only
thing standing between the investigation engine and a remote cluster.

The split is along that seam and nowhere else:

- **`requests()` says what to read**, as `ResourceRequest`s. A provider decides
  how: `LocalKubectlProvider` shells out to kubectl, `RemoteAgentProvider` names
  an evidence kind on a stream an agent already opened. Neither is visible here.
- **`analyse()` says what it means**, as a plain dict, and is pure. It receives
  the results positionally, in the order `requests()` asked for them.

The analysis bodies were moved across unchanged. That is deliberate: they are
the part with production behaviour behind them, and a differential suite can
only prove parity if the code on both sides of the comparison is the same code.

`analyse()` keeps the established `{"error": ...}` contract for a failed read,
because severity, health and overview logic all key off exactly those dicts —
see `_health_summary` and `_severity_summary` in `investigation_service.py`.

(This module previously held two unused stubs, one of which claimed node
inspection was unimplemented while `node_inspector.py` implemented it. They had
no importers and are gone.)
"""

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from app.providers.base import ProviderResult, ResourceRequest


@runtime_checkable
class Inspector(Protocol):
    """A named piece of cluster analysis, independent of how the cluster is reached."""

    # Collector id, evidence kind, and the progress message operators see.
    id: str
    kind: str
    label: str

    def requests(self, scope: Any) -> list[ResourceRequest]:
        """The reads this inspector needs, in the order `analyse` expects them."""
        ...

    def analyse(self, results: Sequence[ProviderResult], scope: Any) -> dict[str, Any]:
        """Turn those reads into findings. Pure: no I/O, no clock, no cluster.

        Takes the scope as well as the results, mirroring `requests(scope)`.
        Some conclusions depend on what was *asked for* rather than what came
        back — "no cluster DNS service exists" is only sayable when the whole
        cluster was scanned, and is not a finding about one namespace.
        """
        ...


def failure(result: ProviderResult, **empty: Any) -> dict[str, Any]:
    """The shape an inspector returns when the read it needed failed.

    Centralised because the exact keys matter. `_status_for` maps `error`
    through `classify_error`, and the health summary counts the empty
    collections that come with it. An inspector inventing its own failure shape
    gets recorded as healthy evidence for a cluster nobody could read — which
    is a bug `WorkloadInspector` shipped once already.
    """
    return {
        "healthy": False,
        "error": result.error,
        "command": {"command": result.equivalent_command},
        **empty,
    }


def items(result: ProviderResult) -> list[dict[str, Any]]:
    """The `items` of a list read, or an empty list if it is not shaped like one."""
    if not isinstance(result.data, dict):
        return []
    found = result.data.get("items")
    return found if isinstance(found, list) else []


def usable(result: ProviderResult) -> bool:
    """Whether a read came back with a JSON object to analyse."""
    return result.success and isinstance(result.data, dict)
