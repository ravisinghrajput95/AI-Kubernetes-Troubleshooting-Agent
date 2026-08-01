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
from app.tenancy import tenant_scope


class FakeSession:
    """Enough of an `AgentSession` for the provider to be built around it."""

    def __init__(self, cluster_id: str, tenant: str = "default") -> None:
        self.cluster_id = cluster_id
        self.tenant = tenant

    @property
    def key(self) -> tuple[str, str]:
        return (self.tenant, self.cluster_id)

    def cancel_all(self, reason: str) -> None:
        """Registered sessions may be evicted; nothing to cancel here."""


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
        registry.register(FakeSession("prod-eu-1"))

        provider = select_provider("prod-eu-1", None)

        assert type(provider).__name__ == "RemoteAgentProvider"
        assert provider.cluster_id == "prod-eu-1"

    def test_a_cluster_with_no_agent_falls_back(self, monkeypatch, registry):
        """A gateway being enabled does not mean every cluster has an agent."""
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)
        registry.register(FakeSession("prod-eu-1"))

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


class TestAnAgentBelongsToOneTenant:
    """A cluster id is not a name another tenant can use to reach an agent."""

    def test_another_tenants_agent_is_not_selected(self, monkeypatch, registry):
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)
        registry.register(FakeSession("prod", tenant="acme"))

        # Same cluster id, different tenant. Falling back to the local
        # kubeconfig is the correct answer; reaching acme's agent is not.
        with tenant_scope("globex"):
            provider = select_provider("prod", None)

        assert isinstance(provider, LocalKubectlProvider)

    def test_its_own_tenants_agent_is_selected(self, monkeypatch, registry):
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)
        registry.register(FakeSession("prod", tenant="acme"))

        with tenant_scope("acme"):
            provider = select_provider("prod", None)

        assert type(provider).__name__ == "RemoteAgentProvider"

    def test_two_tenants_may_use_the_same_cluster_name(self, registry):
        """Neither evicts the other, which keying on cluster id alone would."""
        acme = FakeSession("prod", tenant="acme")
        globex = FakeSession("prod", tenant="globex")
        registry.register(acme)
        registry.register(globex)

        with tenant_scope("acme"):
            assert registry.get("prod") is acme
            assert [session.tenant for session in registry.sessions()] == ["acme"]

        with tenant_scope("globex"):
            assert registry.get("prod") is globex


class TestTheChosenRouteIsVisible:
    async def test_a_local_investigation_says_so(self):
        from tests.test_investigation_service import FakeKubectl, build_service

        investigation = await build_service(FakeKubectl()).run()

        assert investigation["cluster_access"]["provider"] == "kubeconfig"
        assert investigation["cluster_access"]["cluster_id"] == "test-cluster"
