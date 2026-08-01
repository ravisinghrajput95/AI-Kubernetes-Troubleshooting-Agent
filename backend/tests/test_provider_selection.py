"""Which provider an investigation gets, and why.

M5's last piece. Migrating the collectors made the agent path *possible*; this
is what makes it *reachable* — until now `RemoteAgentProvider` was only ever
constructed by tests.

The rule: an agent connected for this cluster wins; otherwise the local
kubeconfig. Nothing above the selector knows which it got, and the answer is
reported on the investigation so an operator does not have to infer it.
"""

import pytest

from app.core.config import settings
from app.providers.local_kubectl import LocalKubectlProvider
from app.services.investigation_service import select_provider


class FakeSession:
    """Enough of an `AgentSession` for the provider to be built around it."""

    def __init__(self, cluster_id: str) -> None:
        self.cluster_id = cluster_id


@pytest.fixture
def registry(monkeypatch):
    """A real registry, empty, installed as the process-wide one."""
    from app.gateway import session as session_module

    registry = session_module.AgentRegistry()
    monkeypatch.setattr(session_module, "_registry", registry)
    return registry


class TestSelection:
    def test_without_a_gateway_the_local_provider_is_used(self, monkeypatch):
        """The getting-started path: a kubeconfig and nothing else."""
        monkeypatch.setattr(settings, "agent_gateway_port", 0)

        provider = select_provider("prod-eu-1", None)

        assert isinstance(provider, LocalKubectlProvider)

    def test_a_connected_agent_is_preferred(self, monkeypatch, registry):
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)
        registry._sessions["prod-eu-1"] = FakeSession("prod-eu-1")

        provider = select_provider("prod-eu-1", None)

        assert type(provider).__name__ == "RemoteAgentProvider"
        assert provider.cluster_id == "prod-eu-1"

    def test_a_cluster_with_no_agent_falls_back(self, monkeypatch, registry):
        """A gateway being enabled does not mean every cluster has an agent."""
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)
        registry._sessions["prod-eu-1"] = FakeSession("prod-eu-1")

        provider = select_provider("staging", None)

        assert isinstance(provider, LocalKubectlProvider)

    def test_selection_does_not_load_grpc_when_no_gateway_is_configured(self, monkeypatch):
        """The lazy-import discipline, asserted rather than trusted.

        `app/gateway/` imports grpc. A deployment reading a local kubeconfig
        must not pay for it, and the only way that stays true is if nothing on
        this path imports it unconditionally.
        """
        monkeypatch.setattr(settings, "agent_gateway_port", 0)

        imported = []
        import builtins

        real_import = builtins.__import__

        def watched(name, *args, **kwargs):
            imported.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", watched)
        select_provider("prod-eu-1", None)

        assert not [name for name in imported if name.startswith("app.gateway")]


class TestTheChosenRouteIsVisible:
    async def test_a_local_investigation_says_so(self):
        from tests.test_investigation_service import FakeKubectl, build_service

        investigation = await build_service(FakeKubectl()).run()

        assert investigation["cluster_access"]["provider"] == "kubeconfig"
        assert investigation["cluster_access"]["cluster_id"] == "test-cluster"
