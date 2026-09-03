"""`MAX_LIST_ITEMS` must mean the same thing through either provider.

F5's built half is a ceiling on how many objects one list read retains, plus a
*record* that it happened — a truncation is a citable evidence gap, so an
investigation that saw the first 2,000 of 50,000 pods says so instead of
reading as a complete picture of a small cluster. `docs/PERFORMANCE_ENVELOPE.md`
sizes the platform's memory on that ceiling holding.

It held on the kubeconfig path only. The cap lived inside `KubectlExecutor`,
and `RemoteAgentProvider` carried a `_truncations` list that was initialised
and never appended to — it existed to satisfy the protocol. So through an agent
**no cap was applied at all**, and `collection_limits.truncated` came back
`false` for a read that had never been bounded.

Measured against a live cluster with `MAX_LIST_ITEMS=3` and a ten-pod
namespace: the kubeconfig path reported `total_pods: 3` and four truncation
records naming returned and retained; the agent path reported `total_pods: 10`,
`truncated: false`, and no records. The same cluster investigated two ways
disagreed about how many pods it has, and the agent — the transport the
platform is built around for real fleets — was the unbounded one.

Found by diffing the *recorded commands* of an agent-served investigation
against a kubeconfig-served one, after the status diff came back clean: the
kubeconfig reads carried `--chunk-size=500` and the agent's carried no limit at
all, which is what prompted the question of whether anything capped them.
"""

import pytest

from app.core.config import settings
from app.kubernetes.list_limit import cap_items
from app.providers.base import OutputFormat, ReadVerb, ResourceRequest
from app.providers.remote_agent import RemoteAgentProvider
from app.wire.codec import _encode_payload
from app.wire.gen.agent.v1 import evidence_pb2

COMMAND = "kubectl get pods -n payments -o json"

# A list read: `get` with no name, which is what `ResourceRequest.is_list` means
# and what the executor's `_is_list_read` recognises on the other path.
LIST_READ = ResourceRequest(verb=ReadVerb.GET, resource="pods", namespace="payments")


def pod_list(count: int) -> dict:
    return {"kind": "PodList", "items": [{"metadata": {"name": f"pod-{i}"}} for i in range(count)]}


def agent_result(payload: dict, limit: int, monkeypatch, request=LIST_READ):
    """Run a payload back through the agent provider's own decode path."""
    monkeypatch.setattr(settings, "max_list_items", limit)
    provider = RemoteAgentProvider.__new__(RemoteAgentProvider)
    provider._executed = []
    provider._truncations = []
    record = evidence_pb2.EvidenceRecord(
        id="k8s.pods:payments",
        kind="k8s.pods",
        status=evidence_pb2.EVIDENCE_STATUS_OK,
        equivalent_command=COMMAND,
        payload=_encode_payload(payload),
    )
    return provider._to_result(record, request), provider.truncations


class TestBothProvidersCapTheSameWay:
    def test_the_agent_path_caps_at_all(self, monkeypatch):
        """The regression. Before this, 10 items came back from a limit of 3."""
        result, _truncations = agent_result(pod_list(10), 3, monkeypatch)

        assert len(result.data["items"]) == 3, (
            f"the agent path retained {len(result.data['items'])} items against a "
            f"MAX_LIST_ITEMS of 3, so the ceiling the platform sizes its memory "
            f"on does not apply to the transport it is built around"
        )

    def test_the_agent_path_records_the_truncation(self, monkeypatch):
        """Capping silently is the half that makes the evidence lie.

        A capped read that reports nothing looks exactly like a complete read of
        a smaller cluster, which is what `truncated: false` was saying.
        """
        _result, truncations = agent_result(pod_list(10), 3, monkeypatch)

        assert truncations == [{"command": COMMAND, "returned": 10, "retained": 3}]

    def test_both_providers_retain_the_same_items(self, monkeypatch):
        """Parity, asserted against the shared rule the executor also uses.

        Comparing the agent against `cap_items` rather than against a hand-written
        expectation is what makes this a *parity* test: if the rule changes, both
        sides move together or this fails.
        """
        payload = pod_list(10)
        expected, expected_record, _ = cap_items(payload, COMMAND, 3)

        result, truncations = agent_result(payload, 3, monkeypatch)

        assert result.data["items"] == expected["items"]
        assert truncations == [expected_record]

    def test_a_list_within_the_ceiling_is_untouched_and_records_nothing(self, monkeypatch):
        """The vacuity guard.

        A provider that capped everything to zero, or recorded a truncation on
        every read, would satisfy both tests above while destroying every
        investigation of a normal cluster.
        """
        result, truncations = agent_result(pod_list(2), 3, monkeypatch)

        assert len(result.data["items"]) == 2
        assert truncations == []

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_disabled_ceiling_caps_nothing(self, limit, monkeypatch):
        """`MAX_LIST_ITEMS=0` disables the cap; it must not mean "retain none"."""
        result, truncations = agent_result(pod_list(10), limit, monkeypatch)

        assert len(result.data["items"]) == 10
        assert truncations == []

    def test_a_text_read_is_not_mistaken_for_a_list(self, monkeypatch):
        """Logs travel as a one-key object and must pass through untouched."""
        monkeypatch.setattr(settings, "max_list_items", 1)
        provider = RemoteAgentProvider.__new__(RemoteAgentProvider)
        provider._executed = []
        provider._truncations = []
        record = evidence_pb2.EvidenceRecord(
            id="k8s.logs:web",
            kind="k8s.logs",
            status=evidence_pb2.EVIDENCE_STATUS_OK,
            equivalent_command="kubectl logs web -n payments",
            payload=_encode_payload({"text": "line one\nline two\nline three"}),
        )

        result = provider._to_result(record)

        assert result.text == "line one\nline two\nline three"
        assert provider.truncations == []

    def test_a_metrics_read_is_capped_on_neither_provider(self, monkeypatch):
        """The divergence the first version of this fix introduced.

        `kubectl top` is *text* on the kubeconfig path, so `_cap_items` never
        sees it and never caps it. Through an agent the same read is a
        metrics.k8s.io list that does have an `items` key, so a cap gated on
        payload shape alone truncated pod metrics on one provider and not the
        other — precisely the disagreement `tests/test_metrics_parity.py` exists
        to prevent, reintroduced by the fix for the opposite problem.

        Caught by a live run reporting five truncation records against the
        kubeconfig path's four, not by any test here; this is what stops it
        coming back.
        """
        top_read = ResourceRequest(verb=ReadVerb.TOP, resource="pods", all_namespaces=True)
        assert not top_read.is_list, "a TOP read is not a list read"

        result, truncations = agent_result(
            {"items": [{"metadata": {"name": f"pod-{i}"}} for i in range(10)]},
            3,
            monkeypatch,
            request=top_read,
        )

        assert len(result.data["items"]) == 10
        assert truncations == []

    def test_a_named_read_is_not_capped(self, monkeypatch):
        """A named read returns one object; only list reads can be unbounded.

        The same rule the executor states in `_is_list_read`, held here so the
        two providers bound the same set of reads rather than each their own.
        """
        named = ResourceRequest(
            verb=ReadVerb.GET,
            resource="pods",
            name="web-0",
            namespace="payments",
            output=OutputFormat.JSON,
        )
        assert not named.is_list

        result, truncations = agent_result(pod_list(10), 3, monkeypatch, request=named)

        assert len(result.data["items"]) == 10
        assert truncations == []
