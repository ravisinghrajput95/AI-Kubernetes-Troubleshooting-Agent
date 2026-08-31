"""Records must come back to the request that asked for them.

A soak against a real agent found the pod-log entries in one investigation
carrying another pod's result: `checkout-…-2vcx8`'s entry holding an error that
named `ledger-…-98tk5`. Over an hour, **5.5% of agent-path pod-log entries**
were mis-paired that way — and that number counts only the ones detectable
because the message happened to name a different pod. A successful read filed
against the wrong pod is the same defect and leaves no trace: the diagnosis
quotes one container's output under another's name, with a citation.

The cause was that `fetch_many` grouped records by **kind** and took them in
arrival order. A wave routinely holds several reads of one kind that differ only
by target — `LogsCollector` issues one `k8s.logs` per problematic pod — and
nothing obliges an agent to answer them in the order they were asked.

Everything needed to do it right was already on the wire: the agent copies
`spec.target` onto every record it returns, refusals included.
"""

from typing import ClassVar

import pytest

from app.providers.base import ReadVerb, ResourceRequest
from app.providers.remote_agent import RemoteAgentProvider
from app.wire.codec import _encode_payload
from app.wire.gen.agent.v1 import evidence_pb2


def record_for(spec, text: str = "", status=evidence_pb2.EVIDENCE_STATUS_OK, detail: str = ""):
    """What the agent sends back: the spec's own target, echoed."""
    record = evidence_pb2.EvidenceRecord(
        id=f"{spec.kind}:{spec.target.name}",
        kind=spec.kind,
        status=status,
        detail=detail,
    )
    record.target.CopyFrom(spec.target)
    if text:
        record.payload = _encode_payload({"text": text})
    return record


class Answering:
    """An agent that replies in an order of its choosing.

    `order` is applied to the specs before records are built, so the reply is a
    permutation of a correct answer — every record is right, only the sequence
    differs. That is precisely the case the platform must survive, and the one
    a fake that answers in request order can never exercise.
    """

    cluster_id = "prod-eu-1"

    def __init__(self, order=lambda specs: specs, status_for=None, detail_for=None):
        self._order = order
        self._status_for = status_for or (lambda spec: evidence_pb2.EVIDENCE_STATUS_OK)
        self._detail_for = detail_for or (lambda spec: "")
        self.seen: list = []

    async def collect(self, specs, investigation_id="", actor=None, budget=None, timeout=0):
        from app.gateway.session import PendingCollection

        self.seen = list(specs)
        pending = PendingCollection(request_id="r1", expected=len(specs))
        for spec in self._order(list(specs)):
            pending.add(
                record_for(
                    spec,
                    text=f"logs of {spec.target.namespace}/{spec.target.name}",
                    status=self._status_for(spec),
                    detail=self._detail_for(spec),
                )
            )
        return pending


def log_requests(*names: str) -> list[ResourceRequest]:
    return [
        ResourceRequest(
            verb=ReadVerb.LOGS,
            name=name,
            namespace="payments",
            options={"tail": 120, "all_containers": True},
        )
        for name in names
    ]


PODS = ("checkout-2vcx8", "checkout-69xqr", "ledger-98tk5", "notifier-dvgl9", "gateway-bf6h5")


class TestARecordGoesToTheRequestThatAskedForIt:
    async def test_out_of_order_replies_still_land_on_the_right_pod(self):
        provider = RemoteAgentProvider(Answering(order=lambda specs: list(reversed(specs))))

        results = await provider.fetch_many(log_requests(*PODS))

        assert len(results) == len(PODS)
        for pod, result in zip(PODS, results, strict=True):
            assert result.success, result.error
            # The text names the pod the agent read. If it is not this pod's
            # name, this pod's entry is holding someone else's logs.
            assert result.text == f"logs of payments/{pod}", (
                f"the entry for {pod} came back holding {result.text!r}"
            )

    @pytest.mark.parametrize(
        "order",
        [
            pytest.param(lambda specs: list(reversed(specs)), id="reversed"),
            pytest.param(lambda specs: specs[2:] + specs[:2], id="rotated"),
            pytest.param(
                lambda specs: [specs[3], specs[0], specs[4], specs[1], specs[2]], id="shuffled"
            ),
            pytest.param(lambda specs: specs, id="in-order"),
        ],
    )
    async def test_any_reply_order_pairs_correctly(self, order):
        provider = RemoteAgentProvider(Answering(order=order))
        results = await provider.fetch_many(log_requests(*PODS))
        assert [result.text for result in results] == [f"logs of payments/{pod}" for pod in PODS]

    async def test_a_refusal_lands_on_the_pod_it_refused(self):
        """The half the soak could actually see.

        A failure's `detail` is the API server's own sentence and it names the
        object, so a mis-paired refusal is legible where a mis-paired success is
        not. That asymmetry is why the soak found this at all.
        """

        # Not the middle of the list: `PODS` has five entries, so reversing
        # leaves the third where it was and the assertion below would hold with
        # the defect present. A vacuous check is the failure mode this file
        # exists to guard against.
        refused = PODS[0]
        assert refused != PODS[len(PODS) - 1 - 0], "pick a pod that reversal moves"

        def refuse_ledger(spec):
            if spec.target.name == refused:
                return evidence_pb2.EVIDENCE_STATUS_FORBIDDEN
            return evidence_pb2.EVIDENCE_STATUS_OK

        provider = RemoteAgentProvider(
            Answering(
                order=lambda specs: list(reversed(specs)),
                status_for=refuse_ledger,
                detail_for=lambda spec: (
                    f'container in pod "{spec.target.name}" is waiting to start'
                    if spec.target.name == refused
                    else ""
                ),
            )
        )

        results = await provider.fetch_many(log_requests(*PODS))
        failed = [
            (pod, result) for pod, result in zip(PODS, results, strict=True) if not result.success
        ]
        assert len(failed) == 1, [r.error for r in results]
        pod, result = failed[0]
        assert pod == refused
        assert refused in result.error

    async def test_two_reads_of_one_pod_in_different_namespaces_do_not_collide(self):
        """The namespace is part of a read's identity, not decoration."""
        requests = [
            ResourceRequest(verb=ReadVerb.LOGS, name="api", namespace="payments"),
            ResourceRequest(verb=ReadVerb.LOGS, name="api", namespace="billing"),
        ]
        provider = RemoteAgentProvider(Answering(order=lambda specs: list(reversed(specs))))
        results = await provider.fetch_many(requests)
        assert [r.text for r in results] == [
            "logs of payments/api",
            "logs of billing/api",
        ]

    async def test_a_missing_record_is_a_gap_and_names_what_is_missing(self):
        """Never a guess — which is what the comment in `fetch_many` promised
        while the code took the next record of the same kind."""
        provider = RemoteAgentProvider(
            Answering(
                order=lambda specs: [spec for spec in specs if spec.target.name != "ledger-98tk5"]
            )
        )
        results = await provider.fetch_many(log_requests(*PODS))
        gaps = [
            (pod, result) for pod, result in zip(PODS, results, strict=True) if not result.success
        ]
        assert [pod for pod, _ in gaps] == ["ledger-98tk5"]
        assert "ledger-98tk5" in gaps[0][1].error
        # And the others are untouched: one absent record must not shift the
        # rest by one, which is what an order-based match does.
        for pod, result in zip(PODS, results, strict=True):
            if pod != "ledger-98tk5":
                assert result.text == f"logs of payments/{pod}"


class TestTheBaselineLogReadAsksForText:
    """`kubectl logs` prints text, and the request has to say so.

    `OutputFormat` defaults to JSON and that default is what decides whether
    `KubectlExecutor` calls `json.loads` on the output — so on the kubeconfig
    path this read failed for every pod that had anything to say and succeeded
    for the silent ones, whose empty output parsed as `{}`. The failure carried
    no reason: kubectl had exited 0 with an empty stderr.

    Asserted on the request the collector builds, because that is the field the
    provider reads. `PreviousPodLogsCollector` already had it right, which is
    what makes the pair worth checking together.
    """

    def test_the_baseline_read_is_text(self):
        from app.kubernetes.logs_collector import LogsCollector
        from app.providers.base import OutputFormat

        requests = LogsCollector().requests([{"name": "web-0", "namespace": "prod"}])
        assert requests, "the collector asked for nothing"
        assert requests[0].output is OutputFormat.TEXT

    async def test_a_pod_with_output_is_not_recorded_as_a_failed_read(self):
        """The inversion, end to end through the executor's parsing rule.

        A pod with logs must not be the one that fails. Driven through
        `LocalKubectlProvider` with a fake executor so the JSON decision — which
        lives in the executor and is taken from `request.output` — is the thing
        under test rather than a re-statement of it.
        """
        from app.kubernetes.logs_collector import LogsCollector
        from app.providers.local_kubectl import LocalKubectlProvider

        class Kubectl:
            """Returns log text for a talkative pod, nothing for a quiet one."""

            executed_commands: ClassVar[list[str]] = []
            truncations: ClassVar[list[dict]] = []

            def run(self, args, parse_json=False):
                import json

                from app.kubernetes.kubectl_executor import KubectlResult

                stdout = "panic: runtime error\n" if "talkative" in args else ""
                data = None
                success = True
                if parse_json:
                    try:
                        data = json.loads(stdout or "{}")
                    except json.JSONDecodeError:
                        success = False
                return KubectlResult(
                    command=args,
                    success=success,
                    stdout=stdout,
                    stderr="",
                    return_code=0,
                    data=data,
                )

        provider = LocalKubectlProvider(executor=Kubectl(), context="kind")
        pods = [{"name": "talkative", "namespace": "prod"}, {"name": "quiet", "namespace": "prod"}]
        results = await provider.fetch_many(LogsCollector().requests(pods))

        assert results[0].success, "a pod with logs was recorded as a failed read"
        assert "panic" in results[0].text
        assert results[1].success
