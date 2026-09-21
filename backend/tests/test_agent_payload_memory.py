"""An agent list read is decoded as it is read, not built and then trimmed.

`MAX_LIST_ITEMS` bounded what the agent path *kept* and never what it *held*:
`decode_payload` built the whole document and `cap_items` dropped items after,
so peak allocation scaled with the cluster while the kubeconfig path had
streamed since F5. Measured on one pod list — peak allocation for the decode,
`tracemalloc`, cap at 2,000:

| pods   | before   | after    | kubeconfig |
|--------|----------|----------|------------|
| 5,000  | 25.7 MB  | 12.2 MB  | 12.4 MB    |
| 10,000 | 51.3 MB  | 12.2 MB  | 12.4 MB    |
| 25,000 | 128.4 MB | 12.2 MB  | 12.4 MB    |

**What this does not bound is the transport.** The payload still arrives whole
in one protobuf message — 21.9 MB of JSON at 25,000 pods — so the worker holds
those bytes either way. What is gone is the decoded expansion of every item
past the cap, which was the term that grew.

The assertion is a *ratio measured in this process*, not a megabyte constant: a
threshold in megabytes measures the machine, and a test that passes because the
runner is roomy is the vacuous pass this repository keeps finding.
"""

from __future__ import annotations

import json
import tracemalloc

import pytest

from app.core.config import settings
from app.kubernetes.list_limit import cap_items
from app.providers.base import ReadVerb, ResourceRequest
from app.providers.remote_agent import RemoteAgentProvider, _decode_streaming, _truncation
from app.wire.codec import decode_payload
from app.wire.gen.agent.v1 import evidence_pb2

CAP = 2_000
POD = {
    "metadata": {
        "name": "checkout-5b5fd56dbf-4cnmv",
        "namespace": "payments",
        "labels": {"app": "checkout", "pod-template-hash": "5b5fd56dbf"},
    },
    "spec": {
        "nodeName": "node-1",
        "containers": [{"name": "app", "image": "example/checkout:1.4"}],
    },
    "status": {"phase": "Running", "containerStatuses": [{"name": "app", "restartCount": 3}]},
}


def pod_list(count: int) -> bytes:
    return json.dumps({"apiVersion": "v1", "kind": "PodList", "items": [POD] * count}).encode()


def peak(call) -> float:
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_the_cap_bounds_what_is_held_not_only_what_is_kept():
    payload = pod_list(20 * CAP)

    def streaming():
        return _decode_streaming(payload, CAP)

    def whole_document():
        return cap_items(decode_payload(payload), "kubectl get pods -o json", CAP)

    streamed, built = peak(streaming), peak(whole_document)

    assert streamed < built / 3, (
        f"decoding held {streamed / 1e6:.1f} MB against {built / 1e6:.1f} MB for the whole "
        f"document — the cap is bounding what is kept, not what is held"
    )


def test_a_list_far_above_the_cap_costs_what_one_at_the_cap_costs():
    """Flat, which is the property that makes the ceiling mean something.

    Both payloads are built *before* measuring: the first version built them
    inside, so it measured `json.dumps` of a 40,000-pod list — 26.6 MB of
    harness, reported as the decoder failing to be flat.
    """
    small, large = pod_list(CAP), pod_list(20 * CAP)
    at_cap = peak(lambda: _decode_streaming(small, CAP))
    far_above = peak(lambda: _decode_streaming(large, CAP))

    assert far_above < at_cap * 1.5, (
        f"{far_above / 1e6:.1f} MB at twenty times the cap against {at_cap / 1e6:.1f} MB at it: "
        f"decoding still scales with the cluster"
    )


def test_what_it_keeps_and_what_it_reports_are_the_cluster_s_numbers():
    """The control. A decoder that kept nothing would pass both tests above."""
    data, returned = _decode_streaming(pod_list(5 * CAP), CAP)

    assert len(data["items"]) == CAP
    assert returned == 5 * CAP, "a truncation record must quote what the cluster returned"
    assert data["kind"] == "PodList", "the envelope around the items must survive"
    assert _truncation("kubectl get pods -o json", returned, CAP) == {
        "command": "kubectl get pods -o json",
        "returned": 5 * CAP,
        "retained": CAP,
    }


@pytest.mark.parametrize("count", [0, 1, CAP - 1, CAP])
def test_a_list_at_or_under_the_cap_is_not_truncated(count):
    data, returned = _decode_streaming(pod_list(count), CAP)

    assert len(data["items"]) == count
    assert returned == count
    assert _truncation("kubectl get pods -o json", returned, CAP) is None


def test_a_read_that_is_not_a_list_keeps_everything():
    """`limit <= 0` is how a non-list read reaches this, and `kubectl top`
    through an agent is a list that must not be capped — F25's parity rule."""
    data, returned = _decode_streaming(pod_list(5 * CAP), 0)

    assert len(data["items"]) == 5 * CAP
    assert returned == 5 * CAP


LIST_READ = ResourceRequest(verb=ReadVerb.GET, resource="pods", namespace="payments")
COMMAND = "kubectl get pods -n payments -o json"


def through_the_provider(payload: bytes):
    """The provider's own path, which is what a mutation of the call site moves.

    Asserting on `_decode_streaming` alone leaves the call site free to go back
    to building the whole document — the helper would still stream, and the test
    would still pass. That mutation was watched to survive before this existed.
    """
    provider = RemoteAgentProvider.__new__(RemoteAgentProvider)
    provider._executed = []
    provider._truncations = []
    record = evidence_pb2.EvidenceRecord(
        id="k8s.pods:payments",
        kind="k8s.pods",
        status=evidence_pb2.EVIDENCE_STATUS_OK,
        equivalent_command=COMMAND,
        payload=payload,
    )
    return provider, lambda: provider._to_result(record, LIST_READ)


def test_the_provider_holds_no_more_than_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_list_items", CAP)
    payload = pod_list(20 * CAP)
    _provider, decode = through_the_provider(payload)

    streamed = peak(decode)
    built = peak(lambda: cap_items(decode_payload(payload), COMMAND, CAP))

    assert streamed < built / 3, (
        f"the provider held {streamed / 1e6:.1f} MB against {built / 1e6:.1f} MB for the "
        f"whole document: it is building the list and trimming it afterwards"
    )

    # A fresh provider: the measurements above decoded more than once, and each
    # capped read appends its own truncation.
    fresh, decode_once = through_the_provider(payload)
    result = decode_once()

    assert len(result.data["items"]) == CAP, "the cap must still apply"
    assert fresh.truncations == [{"command": COMMAND, "returned": 20 * CAP, "retained": CAP}], (
        "a capped read must record what the cluster returned"
    )
