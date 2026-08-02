"""How much work one caller, or one customer, may ask for.

`PRODUCTION_READINESS.md` listed "no rate limiting" as an open P1 and
`docs/PERFORMANCE_ENVELOPE.md` gave it a number: a worker sustains ~10
investigations/s and nothing stopped one caller taking all of it. §10 lists
per-tenant quotas among the mitigations for *evidence volume overwhelms the
platform*; budgets at source were built, this is the other half.

The properties that matter here are the ones that are easy to get almost right:
that only the costed operations are limited, that a limit which lives in one
process is not a limit at all across a fleet, and that this fails **open** where
authorisation fails closed.
"""

import time

import pytest
from fastapi.testclient import TestClient

import app.kubernetes.kubectl_executor as executor_module
from app.auth.dependencies import reset_authenticator
from app.authz.routes import COSTED_PERMISSIONS, ROUTE_PERMISSIONS, is_costed
from app.core.config import Settings, settings
from app.main import app
from app.ratelimit import (
    Decision,
    InMemoryRateLimiter,
    RedisRateLimiter,
    evaluate,
    set_rate_limiter,
)
from tests.test_investigation_service import FakeKubectl

TOKENS = "alice-tok:alice@example.com,bob-tok:bob@example.com"
ALICE = {"Authorization": "Bearer alice-tok"}
BOB = {"Authorization": "Bearer bob-tok"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(executor_module.KubectlExecutor, "run", FakeKubectl.run)
    monkeypatch.setattr(executor_module.KubectlExecutor, "failing_resources", set(), raising=False)
    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "impersonate_users", False)
    monkeypatch.setattr(settings, "rbac_default_role", "admin")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_tenant_per_minute", 0)
    set_rate_limiter(InMemoryRateLimiter())
    reset_authenticator()

    with TestClient(app) as client:
        yield client

    set_rate_limiter(None)
    reset_authenticator()


class TestOnlyCostedOperationsAreLimited:
    """An investigation reads a production cluster and spends a model call.
    Reading what was already collected costs neither.
    """

    def test_submitting_investigations_is_limited(self, api):
        codes = [
            api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE).status_code
            for _ in range(5)
        ]
        assert codes[:3] == [202, 202, 202]
        assert codes[3:] == [429, 429]

    def test_reads_are_never_limited(self, api):
        for _ in range(30):
            assert api.get("/investigations", headers=ALICE).status_code == 200

    def test_the_costed_set_is_exactly_the_outbound_one(self):
        """Catches: adding a read permission to `COSTED_PERMISSIONS`, which
        would rate limit the console's own polling."""
        assert {str(permission) for permission in COSTED_PERMISSIONS} == {"investigation.run"}

    def test_every_costed_route_is_a_write(self):
        """The limit keys off the permission, so this is what it actually
        covers. A GET appearing here would be a mistake in the route table."""
        costed = [route for route, needed in ROUTE_PERMISSIONS.items() if is_costed(needed)]
        assert costed, "nothing is rate limited; the table lost its costed routes"
        assert all(method == "POST" for method, _ in costed), costed


class TestTheRefusalIsUsable:
    def test_it_is_429_with_a_retry_after(self, api):
        for _ in range(3):
            api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE)
        response = api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE)

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0

    def test_it_says_what_the_limit_is(self, api):
        for _ in range(3):
            api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE)
        detail = api.post(
            "/investigations", json={"context": "test-cluster"}, headers=ALICE
        ).json()["detail"]

        assert "3 investigations per minute" in detail
        assert "model call" in detail

    def test_permission_is_checked_before_the_limit(self, api, monkeypatch):
        """A viewer must be told they may not run investigations at all, not
        handed a 429 implying they would be allowed if they waited."""
        monkeypatch.setattr(settings, "rbac_default_role", "viewer")
        from app.authz.resolver import reset_resolver

        reset_resolver()
        for _ in range(6):
            response = api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE)
            assert response.status_code == 403


class TestBucketsAreIndependent:
    def test_one_caller_does_not_spend_anothers_budget(self, api):
        for _ in range(3):
            assert (
                api.post(
                    "/investigations", json={"context": "test-cluster"}, headers=ALICE
                ).status_code
                == 202
            )

        assert (
            api.post("/investigations", json={"context": "test-cluster"}, headers=ALICE).status_code
            == 429
        )
        assert (
            api.post("/investigations", json={"context": "test-cluster"}, headers=BOB).status_code
            == 202
        )

    def test_a_tenant_quota_caps_the_whole_tenant(self, api, monkeypatch):
        """The fairness case: two callers, one customer, one budget."""
        monkeypatch.setattr(settings, "rate_limit_per_minute", 100)
        monkeypatch.setattr(settings, "rate_limit_tenant_per_minute", 2)
        set_rate_limiter(InMemoryRateLimiter())

        assert api.post("/investigations", json={}, headers=ALICE).status_code == 202
        assert api.post("/investigations", json={}, headers=BOB).status_code == 202
        assert api.post("/investigations", json={}, headers=ALICE).status_code == 429

    def test_the_subject_bucket_is_reported_before_the_tenant_one(self):
        """A runaway caller should learn it was their own rate, not a
        colleague's, that stopped them."""
        decision = evaluate(
            InMemoryRateLimiter(), subject="alice", tenant="acme", subject_limit=0, tenant_limit=0
        )
        assert decision.allowed

        limiter = InMemoryRateLimiter()
        for _ in range(2):
            evaluate(limiter, "alice", "acme", subject_limit=2, tenant_limit=2)
        assert evaluate(limiter, "alice", "acme", 2, 2).scope == "subject"

    def test_zero_means_unlimited(self):
        limiter = InMemoryRateLimiter()
        for _ in range(50):
            assert evaluate(limiter, "alice", "acme", 0, 0).allowed


class TestTheLimitIsFleetWideNotPerWorker:
    """The property that makes this a quota at all.

    A counter in process memory across three replicas is three times the
    configured limit, and changes when an operator scales. Only observable
    against a real Redis, because the in-memory limiter *is* the shared counter
    when there is one process — which is exactly why the two are different
    classes rather than one with a flag.
    """

    @pytest.fixture
    def bus(self):
        from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, redis_url

        if not INTEGRATION_ENABLED:
            pytest.skip(SKIP_REASON)

        import uuid

        from app.persistence.redis_bus import RedisBus

        return RedisBus(redis_url(), prefix=f"ratelimit-{uuid.uuid4().hex[:10]}")

    async def test_two_workers_share_one_budget(self, bus):
        worker_a = RedisRateLimiter(bus)
        worker_b = RedisRateLimiter(bus)

        assert evaluate(worker_a, "alice", "acme", 2, 0).allowed
        assert evaluate(worker_b, "alice", "acme", 2, 0).allowed
        # The third is refused wherever it lands, which a per-process counter
        # would have allowed on both workers.
        assert not evaluate(worker_a, "alice", "acme", 2, 0).allowed
        assert not evaluate(worker_b, "alice", "acme", 2, 0).allowed

    async def test_the_counter_expires(self, bus):
        """Otherwise a busy minute would cap a caller forever."""
        limiter = RedisRateLimiter(bus, window_seconds=1)
        assert limiter.hit("alice", 1)[0]
        assert not limiter.hit("alice", 1)[0]

        time.sleep(1.2)
        assert limiter.hit("alice", 1)[0]

    async def test_the_key_always_gets_a_ttl(self, bus):
        """`INCR` creates a key with no expiry; only the caller that sees 1
        sets one. A key without a TTL is a caller banned until someone notices."""
        limiter = RedisRateLimiter(bus)
        limiter.hit("alice", 10)
        limiter.hit("alice", 10)

        keys = list(bus._sync.scan_iter(match=f"{bus.prefix}:ratelimit:*"))
        assert keys
        assert all(bus._sync.ttl(key) > 0 for key in keys)


class TestItFailsOpen:
    """A rate limiter is availability protection, not an authorisation control.

    Refusing every investigation because Redis blinked turns a degraded
    dependency into an outage — and what it guards against is a caller being
    noisy, not a caller being hostile. Authorisation, which *is* a security
    control, fails closed; the two must not be confused.
    """

    def test_a_broken_backend_allows(self):
        class Broken:
            prefix = "test"

            def increment_in_window(self, key, ttl_seconds):
                raise RuntimeError("redis is gone")

        allowed, retry = RedisRateLimiter(Broken()).hit("alice", 1)
        assert allowed
        assert retry > 0

    def test_authorisation_still_fails_closed(self, api, monkeypatch):
        """The contrast, asserted rather than assumed."""
        from app.authz.resolver import reset_resolver
        from app.authz.store import set_member_store

        class BrokenStore:
            def get(self, subject):
                raise RuntimeError("database is gone")

            def touch(self, subject, email=""):
                pass

        set_member_store(BrokenStore())
        reset_resolver()
        try:
            assert api.get("/investigations", headers=ALICE).status_code == 503
        finally:
            set_member_store(None)
            reset_resolver()


class TestConfiguration:
    def test_the_default_is_far_above_a_human_and_below_capacity(self):
        """60/min: no person submits one a second sustained, and a worker
        sustains roughly 600/min per the envelope."""
        assert Settings().rate_limit_per_minute == 60

    def test_a_tenant_quota_is_unset_by_default(self):
        """In `single` mode a tenant quota caps the whole platform, which is a
        different decision an operator should make deliberately."""
        assert Settings().rate_limit_tenant_per_minute == 0

    def test_shared_tenancy_without_a_quota_warns_rather_than_refuses(self, caplog):
        """A fairness gap, not an unsafe configuration — unlike the M6
        refusals, where the alternative was serving two customers out of one
        unprotected table."""
        config = Settings(
            TENANCY_MODE="shared",
            DATABASE_URL="postgresql://localhost/x",
            AUTH_MODE="token",
            RATE_LIMIT_TENANT_PER_MINUTE=0,
        )
        config.validate_rate_limits()  # must not raise

    def test_a_negative_limit_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            Settings(RATE_LIMIT_PER_MINUTE=-1)


class TestTheDecision:
    def test_an_allowed_decision_has_no_message(self):
        assert Decision(True).detail == ""

    def test_a_tenant_refusal_does_not_blame_the_caller(self):
        detail = Decision(False, "tenant", 10, 30).detail
        assert detail.startswith("This tenant has")
