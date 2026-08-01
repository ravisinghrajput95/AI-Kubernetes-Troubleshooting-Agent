"""The provider seam between the engine and a cluster.

Two properties are load-bearing here. First, `ResourceRequest` → argv is a
faithful translation, because migrating a collector to the seam must not change
what it reads. Second, no request can produce a mutating command — that is what
makes it safe to hand a request to a remote agent, which is the whole reason the
seam exists.
"""

import pytest

from app.kubernetes.command_policy import UnsafeKubectlCommand, assert_read_only
from app.kubernetes.kubectl_executor import KubectlExecutor, KubectlResult
from app.providers.base import (
    ClusterProvider,
    OutputFormat,
    ProviderResult,
    ReadVerb,
    ResourceRequest,
)
from app.providers.local_kubectl import LocalKubectlProvider


class FakeExecutor(KubectlExecutor):
    """Records argv instead of shelling out."""

    def __init__(self, result: KubectlResult | None = None) -> None:
        super().__init__(context="test")
        self.calls: list[tuple[list[str], bool]] = []
        self._result = result

    def run(self, args: list[str], parse_json: bool = True) -> KubectlResult:
        self.calls.append((list(args), parse_json))
        return self._result or KubectlResult(
            success=True, stdout="", stderr="", data={}, command=["kubectl", *args], return_code=0
        )


def provider(result: KubectlResult | None = None) -> LocalKubectlProvider:
    return LocalKubectlProvider(executor=FakeExecutor(result))


# Every request the collectors issue, paired with the command it must produce.
# These are the exact reads performed before the provider seam existed; a change
# to any line is a change in cluster behaviour, not a refactor.
TRANSLATIONS = [
    (
        ResourceRequest(verb=ReadVerb.GET, resource="pod", name="web-0", namespace="prod"),
        ["get", "pod", "web-0", "-n", "prod", "-o", "json"],
    ),
    (
        ResourceRequest(
            verb=ReadVerb.LOGS,
            name="web-0",
            namespace="prod",
            output=OutputFormat.TEXT,
            options={"previous": True, "tail": 200, "all_containers": True},
        ),
        ["logs", "web-0", "-n", "prod", "--previous", "--tail=200", "--all-containers=true"],
    ),
    (
        ResourceRequest(
            verb=ReadVerb.GET,
            resource="events",
            namespace="prod",
            field_selector="involvedObject.name=web-0",
        ),
        [
            "get",
            "events",
            "-n",
            "prod",
            "--field-selector=involvedObject.name=web-0",
            "-o",
            "json",
        ],
    ),
    (
        ResourceRequest(
            verb=ReadVerb.DESCRIBE,
            resource="secret",
            name="db",
            namespace="prod",
            output=OutputFormat.TEXT,
        ),
        ["describe", "secret", "db", "-n", "prod"],
    ),
    (
        ResourceRequest(verb=ReadVerb.GET, resource="storageclasses", all_namespaces=True),
        ["get", "storageclasses", "-A", "-o", "json"],
    ),
    (
        ResourceRequest(
            verb=ReadVerb.GET,
            resource="pods",
            namespace="kube-system",
            label_selector="k8s-app=kube-dns",
        ),
        ["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "-o", "json"],
    ),
    (
        ResourceRequest(
            verb=ReadVerb.TOP, resource="pods", all_namespaces=True, output=OutputFormat.TEXT
        ),
        ["top", "pods", "-A", "--no-headers"],
    ),
]


class TestTranslation:
    @pytest.mark.parametrize("request_, expected", TRANSLATIONS)
    def test_requests_translate_to_the_commands_they_replaced(self, request_, expected):
        assert LocalKubectlProvider.to_args(request_) == expected

    @pytest.mark.parametrize("request_, _expected", TRANSLATIONS)
    def test_every_translation_passes_the_read_only_policy(self, request_, _expected):
        assert_read_only(LocalKubectlProvider.to_args(request_))

    async def test_output_format_decides_whether_the_response_is_parsed(self):
        executor = FakeExecutor()
        local = LocalKubectlProvider(executor=executor)

        await local.fetch(ResourceRequest(verb=ReadVerb.GET, resource="pods"))
        await local.fetch(
            ResourceRequest(verb=ReadVerb.GET, resource="pods", output=OutputFormat.TEXT)
        )

        assert [parse_json for _args, parse_json in executor.calls] == [True, False]


class TestNoRequestCanMutate:
    """The closed verb set is the security property, not a validation step."""

    def test_a_mutating_verb_cannot_be_expressed(self):
        with pytest.raises(ValueError):
            ResourceRequest(verb=ReadVerb("delete"), resource="namespace")

    def test_a_command_smuggled_into_a_field_is_still_only_data(self):
        """Hostile values become one argv element; they never become a verb."""
        hostile = ResourceRequest(
            verb=ReadVerb.GET,
            resource="pods",
            name="web-0; kubectl delete ns kube-system",
            namespace="prod",
        )
        args = LocalKubectlProvider.to_args(hostile)

        assert args[0] == "get"
        assert "web-0; kubectl delete ns kube-system" in args
        assert_read_only(args)  # the verb is what the policy judges, and it is `get`

    def test_the_policy_still_rejects_a_hand_built_mutation(self):
        with pytest.raises(UnsafeKubectlCommand):
            assert_read_only(["delete", "pod", "web-0"])


class TestProviderContract:
    async def test_fetch_records_the_equivalent_command(self):
        local = provider()
        result = await local.fetch(ResourceRequest(verb=ReadVerb.GET, resource="nodes"))

        assert isinstance(result, ProviderResult)
        assert result.equivalent_command == "kubectl get nodes -o json"

    async def test_fetch_many_preserves_request_order(self):
        local = provider()
        requests = [
            ResourceRequest(verb=ReadVerb.GET, resource=name) for name in ("nodes", "pods", "svc")
        ]
        results = await local.fetch_many(requests)

        assert len(results) == 3
        assert [r.equivalent_command.split()[2] for r in results] == ["nodes", "pods", "svc"]

    async def test_a_failed_read_becomes_a_result_not_an_exception(self):
        local = provider(
            KubectlResult(
                success=False,
                stdout="",
                stderr="Error from server (Forbidden)",
                data=None,
                command=["kubectl", "get", "nodes"],
                return_code=1,
            )
        )
        result = await local.fetch(ResourceRequest(verb=ReadVerb.GET, resource="nodes"))

        assert result.success is False
        assert "Forbidden" in result.error

    def test_the_local_provider_implements_every_protocol_member(self):
        """Structural, not nominal — a future remote provider need not subclass."""
        required = [name for name in dir(ClusterProvider) if not name.startswith("_")]

        assert set(required) == {
            "cluster_id",
            "executed_commands",
            "fetch",
            "fetch_many",
            "truncations",
        }
        assert all(hasattr(provider(), name) for name in required)

    def test_the_migration_escape_hatch_is_gone(self):
        """M5's exit criterion, expressed as an assertion.

        `raw_executor()` existed so collectors that still built kubectl argv
        could keep working while the rest moved to `ResourceRequest`. Every one
        of them has now moved, so the hatch is gone from the protocol *and*
        from both implementations — which is what makes "the engine cannot tell
        which provider it has" true rather than merely intended.
        """
        from app.providers.remote_agent import RemoteAgentProvider

        assert not hasattr(ClusterProvider, "raw_executor")
        assert not hasattr(LocalKubectlProvider, "raw_executor")
        assert not hasattr(RemoteAgentProvider, "raw_executor")
