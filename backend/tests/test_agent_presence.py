"""What the console sees when the platform is more than one process.

`AgentRegistry` is per-process by necessity — a gRPC stream belongs to the
worker holding the socket. On a managed deployment that meant `GET /agents`
answered from whichever replica the load balancer picked, so a fleet of thirty
clusters behind three replicas showed about ten, and a different ten on the
next refresh. Nothing errored; the page simply under-reported.

These tests pin the two halves of the fix: every worker sees the whole fleet,
and each record says which worker can actually collect through it.
"""

import json

import pytest

from app.gateway.presence import PRESENCE_TTL_SECONDS, AgentPresence
from app.security.identity import AgentIdentity


class FakeBus:
    """Enough Redis to exercise presence, including expiry, deterministically."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.prefix = "test"

    def set_expiring(self, key: str, value: str, ttl_seconds: int) -> None:
        self.values[key] = value
        self.ttls[key] = ttl_seconds

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def scan_values(self, pattern: str) -> list[str]:
        head = pattern.rstrip("*")
        return [value for key, value in self.values.items() if key.startswith(head)]

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def expire(self, key: str) -> None:
        """What Redis does on its own when a worker stops refreshing."""
        self.delete(key)


class FakeSession:
    def __init__(self, cluster_id: str, tenant: str = "default") -> None:
        self.cluster_id = cluster_id
        self.tenant = tenant

    def describe(self) -> dict:
        return {"cluster_id": self.cluster_id, "tenant": self.tenant, "online": True}


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


class TestTheWholeFleetIsVisible:
    def test_a_worker_sees_agents_held_by_other_workers(self, bus):
        AgentPresence(bus, "worker-a").announce(FakeSession("prod-eu"))
        AgentPresence(bus, "worker-b").announce(FakeSession("prod-us"))

        seen = AgentPresence(bus, "worker-a").fleet("default")

        assert sorted(item["cluster_id"] for item in seen) == ["prod-eu", "prod-us"]

    def test_each_record_says_whether_this_worker_can_collect_through_it(self, bus):
        """Visibility is fleet-wide; collection is not.

        Presenting a remote agent as though an investigation could be started
        against it here would produce a silent fallback to a kubeconfig the
        platform may not even have.
        """
        AgentPresence(bus, "worker-a").announce(FakeSession("prod-eu"))
        AgentPresence(bus, "worker-b").announce(FakeSession("prod-us"))

        seen = {
            item["cluster_id"]: item for item in AgentPresence(bus, "worker-a").fleet("default")
        }

        assert seen["prod-eu"]["local"] is True
        assert seen["prod-eu"]["worker"] == "worker-a"
        assert seen["prod-us"]["local"] is False
        assert seen["prod-us"]["worker"] == "worker-b"

    def test_one_tenant_never_sees_anothers_agents(self, bus):
        AgentPresence(bus, "worker-a").announce(FakeSession("prod", tenant="acme"))
        AgentPresence(bus, "worker-a").announce(FakeSession("prod", tenant="globex"))

        presence = AgentPresence(bus, "worker-a")

        assert [item["tenant"] for item in presence.fleet("acme")] == ["acme"]
        assert [item["tenant"] for item in presence.fleet("globex")] == ["globex"]


class TestADeadWorkerLeavesNoPhantoms:
    def test_presence_is_written_with_an_expiry(self, bus):
        """Expiry rather than explicit removal, because a killed worker cannot
        remove its own entries and a fleet index full of ghosts is worse than
        one that is briefly stale."""
        AgentPresence(bus, "worker-a").announce(FakeSession("prod-eu"))

        assert set(bus.ttls.values()) == {PRESENCE_TTL_SECONDS}

    def test_an_unrefreshed_entry_disappears(self, bus):
        presence = AgentPresence(bus, "worker-a")
        presence.announce(FakeSession("prod-eu"))
        presence.announce(FakeSession("prod-us"))

        # worker-b crashed; nothing refreshes prod-us.
        bus.expire("test:agents:default:prod-us")

        assert [item["cluster_id"] for item in presence.fleet("default")] == ["prod-eu"]

    def test_a_clean_disconnect_withdraws_immediately(self, bus):
        """The TTL is the backstop, not the mechanism."""
        presence = AgentPresence(bus, "worker-a")
        session = FakeSession("prod-eu")
        presence.announce(session)
        presence.withdraw(session)

        assert presence.fleet("default") == []


class TestVisibilityNeverBreaksCollection:
    def test_an_unreachable_index_degrades_to_empty_rather_than_raising(self):
        """A console that under-reports is bad; an investigation that fails
        because the console's index was down is worse."""

        class BrokenBus:
            prefix = "test"

            def set_expiring(self, *args):
                raise ConnectionError("redis is gone")

            def delete(self, *args):
                raise ConnectionError("redis is gone")

            def scan_values(self, *args):
                raise ConnectionError("redis is gone")

        presence = AgentPresence(BrokenBus(), "worker-a")

        presence.announce(FakeSession("prod-eu"))
        presence.withdraw(FakeSession("prod-eu"))
        assert presence.fleet("default") == []

    def test_a_corrupt_record_is_skipped_not_fatal(self, bus):
        AgentPresence(bus, "worker-a").announce(FakeSession("prod-eu"))
        bus.values["test:agents:default:broken"] = "{not json"

        seen = AgentPresence(bus, "worker-a").fleet("default")

        assert [item["cluster_id"] for item in seen] == ["prod-eu"]


class TestTheSingleProcessDeploymentNeedsNone:
    def test_presence_is_absent_without_redis(self):
        """None rather than a stub: with one process the registry *is* the
        fleet, and pretending otherwise would put Redis on the
        getting-started path."""
        from app.gateway.presence import get_agent_presence, set_agent_presence

        set_agent_presence(None)
        assert get_agent_presence() is None

    def test_the_api_falls_back_to_the_local_registry(self, monkeypatch):
        from app.api.investigate import connected_agents
        from app.core.config import settings
        from app.gateway import session as session_module
        from app.gateway.presence import set_agent_presence

        set_agent_presence(None)
        monkeypatch.setattr(settings, "agent_gateway_port", 5551)

        registry = session_module.AgentRegistry()
        monkeypatch.setattr(session_module, "_registry", registry)
        registry.register(
            session_module.AgentSession(
                AgentIdentity(cluster_id="prod-eu", tenant="default"),
                session_module.agent_pb2.AgentHello(cluster_id="prod-eu"),
            )
        )

        items = connected_agents()

        assert [item["cluster_id"] for item in items] == ["prod-eu"]
        # A single process holds every stream it knows about, by definition.
        assert items[0]["local"] is True


def test_the_record_survives_a_json_round_trip(bus):
    """Presence crosses a process boundary as text; anything the session
    describes has to survive that."""
    AgentPresence(bus, "worker-a").announce(FakeSession("prod-eu"))

    raw = next(iter(bus.values.values()))
    assert json.loads(raw)["cluster_id"] == "prod-eu"
