"""An alert arrives; an investigation starts. M9's event ingress.

Three things are different about a request with no person behind it, and each
is a place this could be quietly wrong:

- **Impersonation.** `_impersonation_args` returns nothing for an absent or
  anonymous principal, so an alert-triggered investigation without a configured
  identity would read as the platform's *service account* — obtaining access no
  authenticated user could ask for, through the one door with no user behind
  it. That is the test that matters most here.
- **Replay.** A signature makes a body unforgeable, not un-replayable.
- **Repetition.** Alertmanager re-sends. Investigating every delivery turns one
  flapping alert into an unbounded series of production cluster reads.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.core.config import Settings, settings
from app.events import (
    SIGNATURE_TOLERANCE_SECONDS,
    EventSourceError,
    InMemoryTriggerLedger,
    parse_alertmanager,
    parse_sources,
    reset_sources,
    set_trigger_ledger,
)
from app.main import app
from tests.test_investigation_service import FakeKubectl

SECRET = "s3cr3t"
SOURCES = f"alertmanager:{SECRET}:alerts@acme.com:sre|platform"


def alert_body(
    cluster: str = "prod-eu", namespace: str = "payments", fingerprint: str = "abc123"
) -> bytes:
    return json.dumps(
        {
            "status": "firing",
            "commonLabels": {"alertname": "KubePodCrashLooping"},
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": fingerprint,
                    "labels": {
                        "cluster": cluster,
                        "namespace": namespace,
                        "severity": "critical",
                    },
                }
            ],
        }
    ).encode()


def signed(body: bytes, secret: str = SECRET, at: float | None = None) -> dict[str, str]:
    timestamp = str(at if at is not None else time.time())
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-K8sagent-Timestamp": timestamp,
        "X-K8sagent-Signature": signature,
        "Content-Type": "application/json",
    }


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.setattr(settings, "auth_mode", "disabled")
    monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
    monkeypatch.setattr(settings, "impersonate_users", False)
    monkeypatch.setattr(settings, "event_sources", SOURCES)
    monkeypatch.setattr(settings, "event_cooldown_seconds", 1800)
    reset_sources()
    set_trigger_ledger(InMemoryTriggerLedger())

    with TestClient(app) as client:
        yield client

    reset_sources()
    set_trigger_ledger(None)


class TestAnAlertTriggersAnInvestigation:
    """M9's exit criterion, at its smallest."""

    def test_a_signed_alert_starts_one(self, api):
        body = alert_body()
        response = api.post("/events/alertmanager", content=body, headers=signed(body))

        assert response.status_code == 202
        result = response.json()
        assert result["received"] == 1
        assert len(result["started"]) == 1
        assert "KubePodCrashLooping" in result["started"][0]["alert"]

    def test_the_investigation_targets_the_alert_s_cluster_and_namespace(self, api):
        body = alert_body(cluster="prod-us", namespace="checkout")
        started = api.post("/events/alertmanager", content=body, headers=signed(body)).json()

        from app.jobs.store import get_job_store

        job = get_job_store().get(started["started"][0]["id"])
        assert job.request["context"] == "prod-us"
        assert job.request["namespace"] == "checkout"


class TestTheIdentityIsConfiguredNotAbsent:
    """The sharp edge, and the reason a source is an identity rather than a
    secret.

    Impersonation is what makes "the platform cannot see more than you can"
    true. A trigger with no identity reads as the service account, which is
    strictly more than any user could ask for.
    """

    def test_the_investigation_carries_the_source_s_identity(self, api):
        body = alert_body()
        started = api.post("/events/alertmanager", content=body, headers=signed(body)).json()

        from app.jobs.store import get_job_store

        job = get_job_store().get(started["started"][0]["id"])
        assert job.principal["subject"] == "alerts@acme.com"
        assert job.principal["groups"] == ["sre", "platform"]

    def test_that_identity_is_actually_impersonated(self, monkeypatch):
        """Not merely stored: the flags must reach kubectl.

        Catches the whole class of bug where the principal is recorded on the
        job and then not used, which looks correct in every assertion except
        this one.
        """
        from app.kubernetes.kubectl_executor import KubectlExecutor

        monkeypatch.setattr(settings, "impersonate_users", True)
        source = parse_sources(SOURCES)["alertmanager"]
        executor = KubectlExecutor(context="prod-eu", principal=source.principal())

        assert executor._impersonation_args(["get", "pods"]) == [
            "--as",
            "alerts@acme.com",
            "--as-group",
            "sre",
            "--as-group",
            "platform",
        ]

    def test_a_source_without_a_subject_is_refused(self):
        """The failure mode this prevents: silent promotion to the service
        account."""
        with pytest.raises(EventSourceError, match="service account"):
            parse_sources("alertmanager:secret")

    def test_startup_refuses_a_malformed_source(self):
        with pytest.raises(EventSourceError):
            Settings(EVENT_SOURCES="alertmanager:secret").validate_event_sources()

    def test_the_identity_is_marked_as_automated(self):
        """So an audit line can tell a robot from a person holding the same
        subject."""
        assert parse_sources(SOURCES)["alertmanager"].principal().auth_method == "event"


class TestSignatures:
    def test_an_unsigned_request_is_refused(self, api):
        body = alert_body()
        assert api.post("/events/alertmanager", content=body).status_code == 401

    def test_a_wrong_secret_is_refused(self, api):
        body = alert_body()
        response = api.post("/events/alertmanager", content=body, headers=signed(body, "wrong"))
        assert response.status_code == 401

    def test_a_tampered_body_is_refused(self, api):
        """The signature covers the body, so changing the cluster invalidates it."""
        headers = signed(alert_body(cluster="staging"))
        response = api.post(
            "/events/alertmanager", content=alert_body(cluster="prod-eu"), headers=headers
        )
        assert response.status_code == 401

    def test_an_unknown_source_answers_like_a_bad_signature(self, api):
        """A caller must not be able to enumerate configured source names."""
        body = alert_body()
        unknown = api.post("/events/nope", content=body, headers=signed(body))
        bad = api.post("/events/alertmanager", content=body, headers=signed(body, "wrong"))

        assert unknown.status_code == bad.status_code == 401
        assert unknown.json() == bad.json()

    def test_a_replayed_request_expires(self, api):
        """A signature makes a body unforgeable, not un-replayable."""
        body = alert_body()
        stale = signed(body, at=time.time() - SIGNATURE_TOLERANCE_SECONDS - 60)

        assert api.post("/events/alertmanager", content=body, headers=stale).status_code == 401

    def test_the_comparison_is_constant_time(self):
        """Source inspection, because timing is not observable in a functional
        test — and a control with no guard at all is worse than a white-box one.

        `==` on a digest leaks how many leading bytes matched, which is enough
        to forge a signature byte by byte.
        """
        import inspect

        from app.events.sources import EventSource

        source = inspect.getsource(EventSource.verify)
        assert "compare_digest" in source, (
            "signature comparison is no longer constant time; `==` on a digest "
            "leaks a prefix match and lets a signature be forged byte by byte."
        )

    def test_the_timestamp_is_inside_the_signature(self):
        """Otherwise a captured body could be replayed with a fresh timestamp
        and keep a valid signature."""
        source = parse_sources(SOURCES)["alertmanager"]
        body = alert_body()

        assert source.signature(body, "1000") != source.signature(body, "2000")


class TestDeduplication:
    """Alertmanager re-sends. Investigating each delivery would turn one
    flapping alert into an unbounded series of production cluster reads."""

    def test_the_same_fingerprint_does_not_trigger_twice(self, api):
        body = alert_body()

        first = api.post("/events/alertmanager", content=body, headers=signed(body)).json()
        second = api.post("/events/alertmanager", content=body, headers=signed(body)).json()

        assert len(first["started"]) == 1
        assert second["started"] == []
        assert second["skipped"] == 1

    def test_a_repeat_still_returns_202(self, api):
        """Alertmanager retries a non-2xx, so reporting a duplicate as an error
        would produce exactly the storm deduplication exists to prevent."""
        body = alert_body()
        api.post("/events/alertmanager", content=body, headers=signed(body))

        assert (
            api.post("/events/alertmanager", content=body, headers=signed(body)).status_code == 202
        )

    def test_a_different_fingerprint_does_trigger(self, api):
        first = alert_body(fingerprint="aaa")
        second = alert_body(fingerprint="bbb", namespace="orders")

        api.post("/events/alertmanager", content=first, headers=signed(first))
        result = api.post("/events/alertmanager", content=second, headers=signed(second)).json()

        assert len(result["started"]) == 1

    def test_the_cooldown_expires(self):
        ledger = InMemoryTriggerLedger()
        assert ledger.claim("acme:abc", 1)
        assert not ledger.claim("acme:abc", 1)
        time.sleep(1.1)
        assert ledger.claim("acme:abc", 1)

    def test_the_ledger_fails_closed(self):
        """Opposite of the rate limiter, deliberately: a missed deduplication
        is an unbounded series of cluster reads, a missed investigation is one
        alert the operator still sees in their own alerting."""
        from app.events import RedisTriggerLedger

        class Broken:
            prefix = "test"

            def set_if_absent(self, key, ttl_seconds):
                raise RuntimeError("redis is gone")

        assert RedisTriggerLedger(Broken()).claim("acme:abc", 60) is False


class TestParsing:
    def test_resolved_alerts_are_ignored(self):
        payload = {
            "alerts": [
                {"status": "resolved", "fingerprint": "a", "labels": {"cluster": "prod"}},
                {"status": "firing", "fingerprint": "b", "labels": {"cluster": "prod"}},
            ]
        }
        assert [trigger.fingerprint for trigger in parse_alertmanager(payload)] == ["b"]

    def test_an_alert_with_no_cluster_is_ignored(self):
        """Guessing the current kubeconfig context would let an alert from one
        cluster start an investigation of another."""
        payload = {"alerts": [{"status": "firing", "labels": {"namespace": "prod"}}]}
        assert parse_alertmanager(payload) == []

    @pytest.mark.parametrize(
        "label", ["cluster", "cluster_name", "kubernetes_cluster", "k8s_cluster"]
    )
    def test_common_cluster_label_conventions_are_accepted(self, label):
        payload = {"alerts": [{"status": "firing", "fingerprint": "a", "labels": {label: "prod"}}]}
        assert parse_alertmanager(payload)[0].cluster == "prod"

    def test_common_labels_are_merged(self):
        payload = {
            "commonLabels": {"cluster": "prod", "alertname": "Shared"},
            "alerts": [{"status": "firing", "fingerprint": "a", "labels": {"namespace": "web"}}],
        }
        trigger = parse_alertmanager(payload)[0]
        assert trigger.cluster == "prod"
        assert trigger.alert_name == "Shared"

    def test_a_per_alert_label_beats_a_common_one(self):
        payload = {
            "commonLabels": {"cluster": "shared"},
            "alerts": [{"status": "firing", "fingerprint": "a", "labels": {"cluster": "specific"}}],
        }
        assert parse_alertmanager(payload)[0].cluster == "specific"

    def test_a_missing_fingerprint_is_derived_not_dropped(self):
        """Otherwise deduplication is silently disabled for any producer that
        adopts this shape without Alertmanager's fingerprint."""
        payload = {"alerts": [{"status": "firing", "labels": {"cluster": "prod"}}]}
        triggers = parse_alertmanager(payload)

        assert triggers[0].fingerprint
        assert parse_alertmanager(payload)[0].fingerprint == triggers[0].fingerprint

    def test_rubbish_is_not_an_exception(self):
        assert parse_alertmanager({}) == []
        assert parse_alertmanager({"alerts": "nonsense"}) == []
        assert parse_alertmanager({"alerts": [None, 3, "x"]}) == []


class TestTenancy:
    def test_the_tenant_comes_from_configuration_not_the_payload(self, monkeypatch):
        """An alert is attacker-adjacent: anything that can write an alert rule
        can influence its labels. A payload-supplied tenant would be a
        cross-tenant trigger."""
        source = parse_sources(f"am:{SECRET}:alerts@acme.com:sre:acme")["am"]
        assert source.tenant == "acme"
        assert source.principal().tenant == "acme"

    def test_a_payload_cannot_choose_the_tenant(self, api, monkeypatch):
        """The mutation that survived a first pass.

        Testing `parse_sources` proved the *configuration* carries a tenant and
        said nothing about which tenant the handler actually enters — so
        reading it from the body would have passed. This observes the ambient
        tenant at the moment the handler uses it, with a payload claiming
        another.
        """
        from app.events import set_trigger_ledger
        from app.tenancy import current_tenant

        monkeypatch.setattr(settings, "event_sources", f"am:{SECRET}:alerts@acme.com:sre:acme")
        reset_sources()

        seen: list[str] = []

        class Recording:
            def claim(self, key, cooldown_seconds):
                seen.append(current_tenant())
                return True

        set_trigger_ledger(Recording())
        try:
            body = json.dumps(
                {
                    # A system that can write an alert rule can write this.
                    "tenant": "globex",
                    "alerts": [
                        {
                            "status": "firing",
                            "fingerprint": "x",
                            "labels": {"cluster": "prod", "tenant": "globex"},
                        }
                    ],
                }
            ).encode()
            assert api.post("/events/am", content=body, headers=signed(body)).status_code == 202
        finally:
            set_trigger_ledger(InMemoryTriggerLedger())

        assert seen == ["acme"], (
            f"the handler ran as {seen}; a payload chose its own tenant, which is a "
            f"cross-tenant trigger from a system anyone who can write an alert rule "
            f"can influence."
        )

    def test_an_unusable_tenant_is_refused_at_startup(self):
        from app.tenancy import TenantError

        with pytest.raises(TenantError):
            parse_sources(f"am:{SECRET}:alerts@acme.com:sre:Not A Tenant")

    def test_two_sources_cannot_share_a_name(self):
        with pytest.raises(EventSourceError, match="twice"):
            parse_sources("am:a:x@y.com,am:b:z@y.com")


class TestDisabledByDefault:
    def test_no_sources_means_nothing_can_trigger(self, monkeypatch, tmp_path):
        """A platform that reads production clusters should not gain an inbound
        trigger by accident."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(settings, "auth_mode", "disabled")
        monkeypatch.setattr(settings, "allow_insecure_no_auth", True)
        monkeypatch.setattr(settings, "event_sources", "")
        reset_sources()
        try:
            with TestClient(app) as client:
                body = alert_body()
                assert (
                    client.post(
                        "/events/alertmanager", content=body, headers=signed(body)
                    ).status_code
                    == 401
                )
        finally:
            reset_sources()

    def test_the_shipped_default_is_empty(self):
        assert Settings().event_sources == ""
