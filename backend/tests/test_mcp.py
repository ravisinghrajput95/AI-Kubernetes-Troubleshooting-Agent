"""The platform's capabilities as tools, and the authorisation that survives it.

The last roadmap item, and the one with the most obvious way to go wrong: a
second entry point into the same capabilities. M6.5 made authorisation
impossible to forget for HTTP by putting one check in a router-level dependency
and denying any route absent from `ROUTE_PERMISSIONS`. **A tool call is not a
route, so none of that applies to it** — an MCP surface reaching
`run_investigation` directly would be a complete authorisation bypass wearing a
different protocol.

So the tests that matter are the ones asserting the same guarantees hold on
this side: every tool has a permission, a viewer cannot start an investigation
through a tool any more than through a route, and the rate limit is the same
budget rather than a second one.
"""

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.dependencies import reset_authenticator
from app.authz.models import Permission, Role
from app.authz.resolver import reset_resolver
from app.authz.routes import COSTED_PERMISSIONS
from app.authz.store import FileMemberStore, set_member_store
from app.core.config import settings
from app.main import app
from app.mcp import TOOLS, Tool, get_tool
from app.ratelimit import InMemoryRateLimiter, set_rate_limiter
from tests.test_investigation_service import FakeKubectl

TOKENS = "admin-tok:alice@example.com,viewer-tok:victor@example.com"
ADMIN = {"Authorization": "Bearer admin-tok"}
VIEWER = {"Authorization": "Bearer viewer-tok"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "impersonate_users", False)
    monkeypatch.setattr(settings, "rbac_default_role", "none")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 60)

    store = FileMemberStore(tmp_path / "members.json")
    store.upsert("alice@example.com", Role.ADMIN)
    store.upsert("victor@example.com", Role.VIEWER)
    set_member_store(store)
    set_rate_limiter(InMemoryRateLimiter())
    reset_authenticator()
    reset_resolver()

    with TestClient(app) as client:
        yield client

    set_member_store(None)
    set_rate_limiter(None)
    reset_authenticator()
    reset_resolver()


def rpc(client, method: str, headers: dict, params: dict | None = None, request_id: int = 1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/mcp", json=message, headers=headers).json()


def call(client, tool: str, headers: dict, arguments: dict | None = None):
    return rpc(client, "tools/call", headers, {"name": tool, "arguments": arguments or {}})


class TestEveryToolHasAPermission:
    """The property that makes this surface safe to expose at all.

    `ROUTE_PERMISSIONS` denies a route with no entry; the router-level
    dependency cannot see a tool, so the registry has to carry the same default
    and this has to assert it.
    """

    def test_the_registry_is_not_empty(self):
        """A bug in the derivation would make everything below vacuous."""
        assert len(TOOLS) >= 4

    @pytest.mark.parametrize("name", sorted(TOOLS))
    def test_each_tool_declares_one(self, name):
        assert isinstance(TOOLS[name].permission, Permission), name

    def test_an_unregistered_tool_cannot_be_resolved(self, api):
        """Catches: a dispatcher that falls back to calling by name."""
        assert get_tool("run_arbitrary_thing") is None

        reply = call(api, "run_arbitrary_thing", ADMIN)
        assert reply["error"]["code"] == -32601

    def test_no_tool_mutates_the_fleet(self):
        """Enrolment, revocation and member management need `admin` and are the
        operations M6.5 identified as destructive. Handing them to an
        autonomous agent is a decision a customer should make explicitly."""
        forbidden = {
            Permission.CLUSTER_ENROL,
            Permission.CLUSTER_REVOKE,
            Permission.MEMBER_MANAGE,
            Permission.MEMBER_MANAGE_OWNER,
        }
        exposed = {tool.permission for tool in TOOLS.values()}
        assert not (exposed & forbidden), sorted(str(one) for one in exposed & forbidden)


class TestAuthorisationSurvivesTheProtocol:
    def test_a_viewer_cannot_start_an_investigation(self, api):
        reply = call(api, "start_investigation", VIEWER, {"cluster": "test-cluster"})

        assert "result" not in reply
        assert reply["error"]["code"] == -32000
        assert "investigation.run" in reply["error"]["message"]

    def test_an_admin_can(self, api):
        reply = call(api, "start_investigation", ADMIN, {"cluster": "test-cluster"})

        assert "error" not in reply
        assert "investigation_id" in reply["result"]["content"][0]["text"]

    def test_a_viewer_can_still_read(self, api):
        assert "error" not in call(api, "list_investigations", VIEWER)

    def test_tools_list_hides_what_the_caller_cannot_use(self, api):
        """Listing a tool every call would refuse teaches an agent to keep
        trying it, and burns a turn each time."""
        listed = {tool["name"] for tool in rpc(api, "tools/list", VIEWER)["result"]["tools"]}

        assert "start_investigation" not in listed
        assert "list_investigations" in listed

    def test_tools_list_shows_an_admin_more(self, api):
        listed = {tool["name"] for tool in rpc(api, "tools/list", ADMIN)["result"]["tools"]}
        assert "start_investigation" in listed

    def test_the_endpoint_still_needs_authentication(self, api):
        response = api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401

    def test_a_permission_denial_is_not_a_method_not_found(self, api):
        """A client must be able to tell "no such tool" from "not for you",
        or it will retry the wrong fix."""
        denied = call(api, "start_investigation", VIEWER, {"cluster": "x"})
        missing = call(api, "no_such_tool", VIEWER)

        assert denied["error"]["code"] != missing["error"]["code"]


class TestTheRateLimitIsTheSameBudget:
    """A second entry point with its own budget would double the quota an
    operator thought they had configured."""

    def test_costed_tools_are_the_costed_permissions(self):
        costed = {tool.name for tool in TOOLS.values() if tool.permission in COSTED_PERMISSIONS}
        assert costed == {"start_investigation"}

    def test_a_tool_call_spends_the_http_budget(self, api, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
        set_rate_limiter(InMemoryRateLimiter())

        assert "error" not in call(api, "start_investigation", ADMIN, {"cluster": "test-cluster"})
        # Spent through the HTTP surface, not the tool one.
        api.post("/investigations", json={"context": "test-cluster"}, headers=ADMIN)

        reply = call(api, "start_investigation", ADMIN, {"cluster": "test-cluster"})
        assert reply["error"]["code"] == -32000
        assert "per minute" in reply["error"]["message"]

    def test_reads_are_not_limited(self, api, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        set_rate_limiter(InMemoryRateLimiter())

        for _ in range(5):
            assert "error" not in call(api, "list_investigations", ADMIN)


class TestTheProtocol:
    def test_initialize_reports_the_version_and_tools_capability(self, api):
        result = rpc(api, "initialize", ADMIN)["result"]

        assert result["protocolVersion"]
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"]

    def test_a_notification_gets_no_reply(self, api):
        """Replying to a notification is a protocol violation some clients
        treat as fatal."""
        response = api.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=ADMIN,
        )
        assert response.json() is None

    def test_a_batch_is_answered_as_a_batch(self, api):
        replies = api.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            headers=ADMIN,
        ).json()

        assert [reply["id"] for reply in replies] == [1, 2]

    def test_a_batch_of_only_notifications_answers_with_nothing(self, api):
        replies = api.post(
            "/mcp",
            json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}],
            headers=ADMIN,
        ).json()
        assert replies is None

    def test_an_unknown_method_is_method_not_found(self, api):
        assert rpc(api, "resources/list", ADMIN)["error"]["code"] == -32601

    def test_a_non_jsonrpc_message_is_rejected(self, api):
        reply = api.post("/mcp", json={"method": "tools/list"}, headers=ADMIN).json()
        assert reply["error"]["code"] == -32600

    def test_unparseable_input_is_a_parse_error(self, api):
        reply = api.post(
            "/mcp", content=b"{not json", headers={**ADMIN, "Content-Type": "application/json"}
        ).json()
        assert reply["error"]["code"] == -32700

    def test_bad_arguments_are_invalid_params(self, api):
        reply = rpc(api, "tools/call", ADMIN, {"name": "list_clusters", "arguments": "nope"})
        assert reply["error"]["code"] == -32602

    def test_an_unexpected_argument_is_a_client_error_not_a_crash(self, api):
        reply = call(api, "list_investigations", ADMIN, {"nonsense": 1})
        # Tools accept **_ so an extra argument is tolerated rather than fatal;
        # what must not happen is a 500.
        assert "error" not in reply or reply["error"]["code"] == -32602


class TestToolResultsDoNotLeak:
    def test_get_investigation_returns_the_diagnosis_not_the_investigation(self, api):
        """The stored result is megabytes of cluster interior. An agent asking
        what is wrong wants the conclusion — same allowlist reasoning as
        `app/notify`."""
        import json
        import time

        started = json.loads(
            call(api, "start_investigation", ADMIN, {"cluster": "test-cluster"})["result"][
                "content"
            ][0]["text"]
        )
        job_id = started["investigation_id"]

        for _ in range(200):
            time.sleep(0.05)
            reply = call(api, "get_investigation", ADMIN, {"investigation_id": job_id})
            payload = json.loads(reply["result"]["content"][0]["text"])
            if payload["status"] in {"succeeded", "failed"}:
                break

        assert set(payload) == {
            "investigation_id",
            "status",
            "error",
            "cluster",
            "severity",
            "health",
            "root_cause",
            "explanation",
            "fix",
            "commands",
            "confidence",
            "ai_generated",
            "evidence_coverage",
        }

    def test_an_unknown_investigation_is_not_an_oracle(self, api):
        import json

        reply = call(api, "get_investigation", ADMIN, {"investigation_id": "does-not-exist"})
        assert "No such investigation" in json.loads(reply["result"]["content"][0]["text"])["error"]

    def test_a_tool_failure_does_not_echo_its_exception(self, api, monkeypatch):
        """A tool failure can carry cluster text, and this surface exists to be
        consumed by a model."""

        async def explode(principal, **kwargs):
            raise RuntimeError("secret-cluster-detail token=abc123")

        # A frozen dataclass, so the whole entry is replaced rather than
        # mutated — which is also what a real registration would do.
        monkeypatch.setitem(
            TOOLS,
            "list_investigations",
            Tool(
                name="list_investigations",
                description="x",
                permission=Permission.INVESTIGATION_READ,
                schema={"type": "object"},
                handler=explode,
            ),
        )

        reply = call(api, "list_investigations", ADMIN)
        assert reply["error"]["code"] == -32603
        assert "abc123" not in reply["error"]["message"]
        assert "secret-cluster-detail" not in reply["error"]["message"]
