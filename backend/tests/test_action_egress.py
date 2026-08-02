"""Announcing a finished investigation. M9's action egress.

Completes the milestone's exit criterion: an alert triggers an investigation
that opens a ticket, with no human involved. `test_event_ingress.py` covers the
first arrow; this covers the second, and the two meet in
`TestTheWholeChainRuns`.

Three properties are the whole point, and each fails silently if it regresses:
a notification must never be able to fail an investigation; a summary must
leave, never the result; and a destination must only ever hear about its own
tenant's work.
"""

import asyncio
import json

import pytest

from app.core.config import Settings, settings
from app.notify import (
    Destination,
    DestinationError,
    announce,
    build_summary,
    deliver,
    encode,
    parse_destinations,
    reset_destinations,
)
from app.tenancy import tenant_scope

INVESTIGATION = {
    "context": "prod-eu",
    "scope": {"namespace": "payments"},
    "severity": {"severity": "critical", "affected_workloads": 3},
    "health": {"status": "error"},
    # The parts that must not leave.
    "pods": {"items": [{"name": "web-0", "env": "DB_PASSWORD=hunter2"}]},
    "logs": {"web-0": "2026-08-02 ERROR token=abc123xyz"},
    "evidence": [{"id": "pod.crash:web-0", "command": "kubectl get pods -n payments"}],
    "executed_commands": ["kubectl get pods -n payments -o json"],
}

DIAGNOSIS = {
    "root_cause": "The web container exits before its readiness probe passes.",
    "confidence": 82,
    "ai_generated": True,
    "signals": [{"id": "pod.crash_loop:pod/payments/web-0", "summary": "internal"}],
}


@pytest.fixture(autouse=True)
def clean():
    reset_destinations()
    yield
    reset_destinations()


class TestOnlyASummaryLeaves:
    """The stored result is megabytes of cluster interior. A ticket needs what
    happened, how bad, and where to read the rest."""

    def test_no_evidence_reaches_the_payload(self):
        body = encode(build_summary("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)).decode()

        for leaked in ("hunter2", "abc123xyz", "web-0", "kubectl", "pod.crash"):
            assert leaked not in body, f"{leaked!r} left the platform in a notification"

    def test_it_carries_what_a_ticket_needs(self):
        summary = build_summary("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)

        assert summary["investigation_id"] == "inv-1"
        assert summary["cluster"] == "prod-eu"
        assert summary["namespace"] == "payments"
        assert summary["severity"] == "critical"
        assert summary["root_cause"].startswith("The web container")
        assert summary["confidence"] == 82

    def test_it_links_rather_than_carries(self):
        summary = build_summary(
            "inv-1", "succeeded", INVESTIGATION, DIAGNOSIS, console_url="https://k8s.example.com/"
        )
        assert summary["url"] == "https://k8s.example.com/investigations/inv-1"

    def test_no_console_url_means_no_link_rather_than_a_guess(self):
        """A guessed hostname is a 404 from a ticket someone is reading during
        an incident."""
        assert build_summary("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)["url"] == ""

    def test_the_summary_is_an_allowlist_not_a_filter(self):
        """A denylist would leak whatever a future collector adds; this asserts
        the shape is fixed rather than derived from the result."""
        summary = build_summary("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)
        assert set(summary) == {
            "investigation_id",
            "outcome",
            "cluster",
            "namespace",
            "severity",
            "health",
            "root_cause",
            "confidence",
            "ai_generated",
            "affected_workloads",
            "url",
        }

    def test_a_new_investigation_key_does_not_appear(self):
        """The regression this guards: someone adds a section and it is
        announced to a third party the next day."""
        polluted = {**INVESTIGATION, "brand_new_section": {"secret": "leaked"}}
        assert "leaked" not in encode(build_summary("i", "succeeded", polluted, DIAGNOSIS)).decode()


class TestADestinationBelongsToATenant:
    """Announcing acme's incident into globex's Slack is M6's failure committed
    on the way out."""

    def test_another_tenants_destination_is_not_told(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.notify.dispatcher._spawn", lambda coro: (coro.close(), sent.append("sent"))
        )
        monkeypatch.setattr(
            settings, "notify_destinations", "globex-oncall|https://globex.example/x||globex"
        )
        reset_destinations()

        with tenant_scope("acme"):
            announce("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)

        assert sent == []

    def test_its_own_tenants_destination_is_told(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.notify.dispatcher._spawn", lambda coro: (coro.close(), sent.append("sent"))
        )
        monkeypatch.setattr(
            settings, "notify_destinations", "acme-oncall|https://acme.example/x||acme"
        )
        reset_destinations()

        with tenant_scope("acme"):
            announce("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)

        assert sent == ["sent"]


class TestANotificationCannotFailAnInvestigation:
    """The rule the dispatcher exists to enforce."""

    async def test_an_unreachable_receiver_is_not_an_exception(self):
        destination = Destination(name="x", url="http://127.0.0.1:9/none")
        assert await deliver(destination, {"investigation_id": "i"}) is False

    def test_a_broken_configuration_does_not_raise_at_announce_time(self, monkeypatch):
        monkeypatch.setattr(settings, "notify_destinations", "this is not a destination")
        reset_destinations()

        announce("inv-1", "succeeded", INVESTIGATION, DIAGNOSIS)  # must not raise

    def test_announce_returns_nothing_to_await(self, monkeypatch):
        """A caller that could observe delivery would eventually be written to
        depend on it."""
        monkeypatch.setattr(settings, "notify_destinations", "")
        reset_destinations()

        assert announce("i", "succeeded", None, None) is None

    async def test_a_rejecting_receiver_is_not_retried(self, monkeypatch):
        """Retrying a 4xx turns our bug into their rate limit."""
        attempts = []

        class Response:
            status_code = 422

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content=None, headers=None):
                attempts.append(url)
                return Response()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
        await deliver(Destination(name="x", url="https://example.com/x"), {"a": 1})

        assert len(attempts) == 1

    async def test_a_failing_receiver_is_retried_then_given_up_on(self, monkeypatch):
        attempts = []

        class Response:
            status_code = 503

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content=None, headers=None):
                attempts.append(url)
                return Response()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
        monkeypatch.setattr("app.notify.dispatcher.BACKOFF_SECONDS", (0.0, 0.0))
        await deliver(Destination(name="x", url="https://example.com/x"), {"a": 1})

        assert len(attempts) == 3


class TestDelivery:
    async def test_the_body_is_signed_when_a_secret_is_configured(self, monkeypatch):
        captured: dict = {}

        class Response:
            status_code = 200

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content=None, headers=None):
                captured["body"] = content
                captured["headers"] = headers
                return Response()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
        destination = Destination(name="x", url="https://example.com/x", secret="sh4red")
        summary = {"investigation_id": "inv-1"}

        assert await deliver(destination, summary) is True
        assert captured["headers"]["X-K8sagent-Signature"] == destination.signature(encode(summary))

    async def test_an_unsigned_destination_sends_no_signature(self, monkeypatch):
        captured: dict = {}

        class Response:
            status_code = 200

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content=None, headers=None):
                captured["headers"] = headers
                return Response()

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
        await deliver(Destination(name="x", url="https://example.com/x"), {"a": 1})

        assert "X-K8sagent-Signature" not in captured["headers"]

    def test_the_encoding_is_canonical(self):
        """The receiver verifies a signature over bytes; key order must not
        depend on dictionary insertion."""
        assert encode({"b": 1, "a": 2}) == encode({"a": 2, "b": 1})


class TestFiltering:
    def test_a_severity_floor_is_respected(self):
        destination = Destination(name="x", url="https://e/x", min_severity="high")

        assert destination.accepts("succeeded", "critical")
        assert destination.accepts("succeeded", "high")
        assert not destination.accepts("succeeded", "medium")

    def test_failures_are_not_announced_by_default(self):
        """A failed collection is the platform's problem; paging someone about
        it trains them to ignore the channel."""
        assert not Destination(name="x", url="https://e/x").accepts("failed", "critical")

    def test_failures_can_be_opted_into(self):
        destination = Destination(name="x", url="https://e/x", outcomes=("succeeded", "failed"))
        assert destination.accepts("failed", "critical")

    def test_an_unknown_severity_is_delivered_rather_than_dropped(self):
        """A notification nobody expected is recoverable; a silently withheld
        incident is not."""
        assert Destination(name="x", url="https://e/x", min_severity="high").accepts(
            "succeeded", "catastrophic"
        )


class TestConfiguration:
    def test_a_url_with_a_port_survives_parsing(self):
        """The URL contains colons, so a naive split would truncate every
        https:// destination to `https`."""
        destination = parse_destinations("x|https://hooks.example.com:8443/path|secret")[0]
        assert destination.url == "https://hooks.example.com:8443/path"
        assert destination.secret == "secret"

    def test_a_full_entry_parses(self):
        destination = parse_destinations(
            "oncall|https://hooks.example.com/x|sh4red|acme|high|succeeded+failed"
        )[0]

        assert destination.name == "oncall"
        assert destination.tenant == "acme"
        assert destination.min_severity == "high"
        assert destination.outcomes == ("succeeded", "failed")

    @pytest.mark.parametrize(
        "raw",
        [
            "noscheme|example.com/x",
            "badscheme|ftp://example.com/x",
            "nourl|",
            "|https://example.com/x",
            "bad|https://example.com/x||acme|nonsense",
        ],
    )
    def test_a_malformed_destination_is_refused(self, raw):
        with pytest.raises(DestinationError):
            parse_destinations(raw)

    def test_an_unusable_tenant_is_refused(self):
        from app.tenancy import TenantError

        with pytest.raises(TenantError):
            parse_destinations("x|https://e/x||Not A Tenant")

    def test_startup_refuses_a_malformed_destination(self):
        with pytest.raises(DestinationError):
            Settings(NOTIFY_DESTINATIONS="x|example.com/x").validate_notify_destinations()

    def test_the_shipped_default_announces_nothing(self):
        assert Settings().notify_destinations == ""
        assert parse_destinations("") == []


class TestTheWholeChainRuns:
    """M9's exit criterion, end to end: alert to investigation to announcement,
    with no human anywhere in it.
    """

    async def test_an_alert_produces_a_notification(self, monkeypatch, tmp_path):
        import hashlib
        import hmac
        import time

        from fastapi.testclient import TestClient

        import app.kubernetes.kubectl_executor as executor_module
        from app.events import InMemoryTriggerLedger, reset_sources, set_trigger_ledger
        from app.main import app
        from tests.test_investigation_service import FakeKubectl

        delivered: list[dict] = []

        async def capture(destination, summary):
            delivered.append(summary)
            return True

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
        monkeypatch.setattr(
            executor_module.KubectlExecutor, "failing_resources", set(), raising=False
        )
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
        monkeypatch.setattr(settings, "impersonate_users", False)
        monkeypatch.setattr(settings, "event_sources", "am:s3cr3t:alerts@acme.com:sre")
        monkeypatch.setattr(
            settings, "notify_destinations", "oncall|https://hooks.example.com/x||default|info"
        )
        monkeypatch.setattr("app.notify.dispatcher.deliver", capture)
        reset_sources()
        reset_destinations()
        set_trigger_ledger(InMemoryTriggerLedger())

        body = json.dumps(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "fingerprint": "chain-1",
                        "labels": {
                            "cluster": "test-cluster",
                            "alertname": "KubePodCrashLooping",
                        },
                    }
                ]
            }
        ).encode()
        timestamp = str(time.time())
        signature = hmac.new(
            b"s3cr3t", timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/events/am",
                    content=body,
                    headers={
                        "X-K8sagent-Timestamp": timestamp,
                        "X-K8sagent-Signature": signature,
                        "Content-Type": "application/json",
                    },
                )
                assert response.status_code == 202
                job_id = response.json()["started"][0]["id"]

                for _ in range(200):
                    await asyncio.sleep(0.05)
                    if delivered:
                        break
        finally:
            reset_sources()
            reset_destinations()
            set_trigger_ledger(None)

        assert delivered, "the alert never produced a notification"
        assert delivered[0]["investigation_id"] == job_id
        assert delivered[0]["cluster"] == "test-cluster"
        assert delivered[0]["root_cause"]
