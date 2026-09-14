"""A named object that does not exist reads the same through either provider.

The reference resolver recognised absence by kubectl's wording — `"not found"`
in stderr. An agent reports the same 404 as an EMPTY status with no error text
(`statusFor` in agent/internal/collectors), so through an agent a missing
ConfigMap was indistinguishable from a failed read. Live, on the same cluster
in the same minute: the kubeconfig investigation named
`notifier-config-does-not-exist`; the agent one said no collected evidence
named which object, and offered placeholder commands.

Each provider now sets `not_found` from what it knows, and these tests put the
two real shapes — kubectl's stderr, the agent's wire record — through the same
resolver and require the same answer.
"""

import pytest

from app.collectors.base import CollectionBudget, CollectionContext, InvestigationScope
from app.collectors.targeted import ConfigReferenceCollector
from app.evidence.models import Evidence, EvidenceKind, EvidenceStatus, ResourceRef
from app.kubernetes.kubectl_executor import KubectlResult
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest
from app.providers.local_kubectl import LocalKubectlProvider
from app.providers.remote_agent import RemoteAgentProvider
from app.wire.gen.agent.v1 import evidence_pb2

POD = ResourceRef(kind="Pod", name="notifier-0", namespace="payments")
READ = ResourceRequest(
    verb=ReadVerb.GET, resource="configmap", name="notifier-config", namespace="payments"
)


def kubectl_answer(stderr: str) -> ProviderResult:
    class Executor:
        def run(self, args, parse_json=False):
            return KubectlResult(["kubectl", *args], False, "", stderr, 1)

    provider = LocalKubectlProvider(context="t", executor=Executor())
    return provider._to_result(Executor().run([]), named=True)


def agent_answer(status) -> ProviderResult:
    provider = RemoteAgentProvider.__new__(RemoteAgentProvider)
    provider._executed = []
    provider._truncations = []
    record = evidence_pb2.EvidenceRecord(
        id="k8s.configmap:payments/notifier-config", kind="k8s.configmap", status=status
    )
    return provider._to_result(record, READ)


class TestBothProvidersSayAbsent:
    def test_kubectl_not_found(self):
        answer = kubectl_answer(
            'Error from server (NotFound): configmaps "notifier-config" not found'
        )
        assert answer.not_found is True

    def test_the_agents_named_empty(self):
        assert agent_answer(evidence_pb2.EVIDENCE_STATUS_EMPTY).not_found is True


class TestNeitherCallsAFailureAbsent:
    def test_kubectl_forbidden(self):
        answer = kubectl_answer(
            'Error from server (Forbidden): configmaps "notifier-config" is forbidden'
        )
        assert answer.not_found is False

    @pytest.mark.parametrize(
        "status",
        [
            evidence_pb2.EVIDENCE_STATUS_FORBIDDEN,
            evidence_pb2.EVIDENCE_STATUS_FAILED,
            evidence_pb2.EVIDENCE_STATUS_OK,
        ],
    )
    def test_agent_statuses_other_than_named_empty(self, status):
        assert agent_answer(status).not_found is False


class Answering:
    """A provider that answers the pod spec store lookup and one read."""

    def __init__(self, answer: ProviderResult):
        self.answer = answer
        self.cluster_id = "t"

    async def fetch(self, request):
        return self.answer

    async def fetch_many(self, requests):
        return [self.answer for _ in requests]


async def resolve(answer: ProviderResult) -> dict:
    context = CollectionContext(
        scope=InvestigationScope(context="t"), provider=Answering(answer), budget=CollectionBudget()
    )
    context.store.add(
        Evidence.create(
            kind=EvidenceKind.POD_SPEC,
            target=POD,
            status=EvidenceStatus.OK,
            data={
                "containers": [
                    {"config_refs": [{"kind": "ConfigMap", "name": "notifier-config", "key": "K"}]}
                ]
            },
        )
    )
    evidence = await ConfigReferenceCollector(POD).collect(context)
    return evidence[0].data["references"][0]


@pytest.mark.parametrize(
    "answer",
    [
        kubectl_answer('Error from server (NotFound): configmaps "notifier-config" not found'),
        agent_answer(evidence_pb2.EVIDENCE_STATUS_EMPTY),
    ],
    ids=["kubeconfig", "agent"],
)
async def test_the_resolver_names_the_missing_object_on_both_paths(answer):
    reference = await resolve(answer)
    assert reference["exists"] is False
    assert reference["detail"] == "ConfigMap does not exist."


def test_absence_is_never_served_from_cache():
    # Through an agent a named 404 is a *successful* EMPTY read, which is the
    # one shape the cache stores. The ConfigMap an investigation reports
    # missing is the one the operator is about to create.
    from app.providers.cache import CollectionCache

    cache = CollectionCache(ttl_seconds=60, max_bytes=1_000_000)
    absent = agent_answer(evidence_pb2.EVIDENCE_STATUS_EMPTY)
    assert absent.success and absent.not_found

    cache.put("key", absent)
    assert cache.get("key") is None
