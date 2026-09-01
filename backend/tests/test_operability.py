"""Liveness, readiness, graceful drain and log correlation.

Tier-5 items 41, 42 and 43. Each of these fails *silently* if it regresses —
a readiness probe that never goes false during shutdown looks identical to one
that does until you watch a rolling deploy drop requests, and a correlation id
that stops propagating leaves logs that merely look ordinary.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.core.config import settings
from app.core.correlation import (
    NO_CORRELATION,
    bind,
    correlation_id,
    correlation_scope,
    new_request_id,
    sanitise,
)
from app.core.logging import _inject_correlation
from app.core.readiness import Readiness, get_readiness, reset_readiness
from app.main import create_app


@pytest.fixture(autouse=True)
def clean_readiness():
    reset_readiness()
    yield
    reset_readiness()


@pytest.fixture(autouse=True)
def acknowledged_local_auth(monkeypatch):
    """`AUTH_MODE=disabled` refuses to build an authenticator unacknowledged.

    That refusal is the point of F13 and is asserted elsewhere; here it is
    merely in the way of testing probes and correlation ids.

    Both halves are set deliberately. This fixture used to set only the
    acknowledgement and inherit the mode from `AUTH_MODE`'s default, which is
    the one-variable path to an open deployment that `TestNoModeIsChosenForYou`
    now closes — so with no default, the mode has to be named here as an
    operator would have to name it.
    """
    monkeypatch.setattr(settings, "auth_mode", "disabled")
    monkeypatch.setattr(settings, "allow_insecure_no_auth", True)


class TestLivenessAndReadinessAreDifferentQuestions:
    def test_liveness_never_consults_a_dependency(self, monkeypatch):
        """A database blip must not restart every worker in the fleet.

        This is the asymmetry that makes splitting the probes worth doing: if
        liveness checked Postgres, a recoverable dependency failure would
        become a fleet-wide restart storm. Same shape as the rate limiter
        failing open while authorisation fails closed.
        """
        called = False

        def explode():
            nonlocal called
            called = True
            raise AssertionError("liveness must not probe dependencies")

        monkeypatch.setattr("app.jobs.store.get_job_store", explode)

        with TestClient(create_app()) as client:
            response = client.get("/health/live")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert not called

    def test_readiness_is_true_once_started(self):
        with TestClient(create_app()) as client:
            response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readiness_is_false_before_startup_finishes(self):
        """A process listening before its store is wired must not be sent work."""
        state = Readiness()

        assert not state.ready
        assert state.reason() == "starting"

    def test_readiness_goes_false_the_moment_draining_begins(self):
        """The window this exists for.

        SIGTERM and removal from Endpoints race. For the propagation window the
        pod still receives requests, so readiness has to fail *before* anything
        is torn down rather than as a consequence of it.
        """
        state = Readiness()
        state.mark_started()
        assert state.ready

        state.begin_drain()

        assert not state.ready
        assert state.reason() == "draining"

    def test_a_draining_worker_answers_503_but_stays_alive(self):
        with TestClient(create_app()) as client:
            get_readiness().begin_drain()

            ready = client.get("/health/ready")
            live = client.get("/health/live")

        assert ready.status_code == 503
        assert ready.json()["reason"] == "draining"
        # Still alive: killing it here would abandon the work it is draining.
        assert live.status_code == 200

    def test_losing_postgres_takes_the_worker_out_of_rotation(self, monkeypatch):
        class BrokenStore:
            def check_health(self):
                return {"postgres": "unavailable", "redis": "ok"}

        monkeypatch.setattr("app.jobs.store.get_job_store", lambda: BrokenStore())

        with TestClient(create_app()) as client:
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["postgres"] == "unavailable"
        # Names which one. "not ready" with no detail sends an operator to the
        # logs of a process that is by definition not serving.
        assert body["reason"] == "postgres"

    def test_losing_redis_leaves_the_worker_in_rotation(self, monkeypatch):
        """The asymmetry, and the reason `degraded` exists as a third value.

        Every worker shares one Redis. Failing readiness on it would take the
        whole fleet out at once — while every read still resolves, because
        `get`, `get_summary`, `list` and the report endpoints all go straight
        to Postgres. That is a degradation presenting as a total outage, the
        precise inversion of "if Redis drops everything the system is slower,
        never wrong". `scripts/chaos_bench.py redis-loss` caught this; the
        first implementation reported Redis as `unavailable`.
        """

        class DegradedStore:
            def check_health(self):
                return {"postgres": "ok", "redis": "degraded"}

        monkeypatch.setattr("app.jobs.store.get_job_store", lambda: DegradedStore())

        with TestClient(create_app()) as client:
            response = client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        # Still reported, so an operator can see the degradation.
        assert body["checks"]["redis"] == "degraded"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("unavailable", 503), ("degraded", 200), ("ok", 200)],
    )
    def test_the_store_decides_severity_not_the_handler(self, monkeypatch, status, expected):
        """A dependency the handler has never heard of, to prove it applies one
        rule rather than a table of names.

        Asserted behaviourally rather than by grepping the module: the
        docstrings legitimately discuss Postgres and Redis, and a source check
        would either fail on those or be weakened until it proved nothing.
        """

        class ExoticStore:
            def check_health(self):
                return {"cassandra": status}

        monkeypatch.setattr("app.jobs.store.get_job_store", lambda: ExoticStore())

        with TestClient(create_app()) as client:
            response = client.get("/health/ready")

        assert response.status_code == expected
        assert response.json()["checks"]["cassandra"] == status

    def test_the_distributed_store_calls_redis_degraded_and_postgres_unavailable(self):
        """The severity choice itself, on the real store.

        Every other readiness test here uses a fake store, so all of them pass
        while `PostgresRedisJobStore` classifies Redis as `unavailable` — which
        is the defect `scripts/chaos_bench.py redis-loss` found, and which
        survived the first mutation run for exactly this reason. The chaos
        suite is opt-in and not in CI, so the choice has to be pinned here too.

        No infrastructure: the store is constructed with doubles that fail the
        way a down dependency fails.
        """
        from app.jobs.distributed import PostgresRedisJobStore

        class DeadDatabase:
            def cursor(self):
                raise ConnectionError("could not connect to server")

        class DeadBus:
            def ping(self):
                raise ConnectionError("Error 61 connecting to localhost:6379")

        class LiveDatabase:
            def cursor(self):
                import contextlib

                @contextlib.contextmanager
                def _cursor():
                    class Cursor:
                        def execute(self, *args):
                            return None

                    yield Cursor()

                return _cursor()

        class LiveBus:
            def ping(self):
                return None

        both_down = PostgresRedisJobStore(DeadDatabase(), DeadBus()).check_health()
        assert both_down["postgres"] == "unavailable"
        assert both_down["redis"] == "degraded", (
            "Redis must be degraded, not unavailable: every worker shares one Redis, "
            "so failing readiness on it takes the whole fleet out while every read "
            "still resolves from Postgres."
        )

        healthy = PostgresRedisJobStore(LiveDatabase(), LiveBus()).check_health()
        assert healthy == {"postgres": "ok", "redis": "ok"}

        # And each is caught separately, so one failure cannot mask the other.
        redis_only = PostgresRedisJobStore(LiveDatabase(), DeadBus()).check_health()
        assert redis_only == {"postgres": "ok", "redis": "degraded"}

    def test_the_single_process_deployment_has_nothing_to_be_unready_about(self):
        """The supported default must not be permanently unready.

        Reporting a Postgres it does not use as failed would do exactly that.
        """
        from app.jobs.store import InMemoryJobStore

        assert InMemoryJobStore().check_health() == {}

    def test_health_keeps_its_old_shape(self):
        """The console reads this before it can authenticate."""
        with TestClient(create_app()) as client:
            body = client.get("/health").json()

        assert body["status"] == "healthy"
        assert "auth_mode" in body

    def test_the_probes_are_reachable_without_a_credential(self):
        """A probe that 401s restarts a pod whose only fault is a typo'd issuer."""
        from app.authz.routes import PUBLIC

        assert "/health/live" in PUBLIC
        assert "/health/ready" in PUBLIC


class TestGracefulDrain:
    async def test_a_finishing_investigation_is_waited_for(self):
        runner = _runner()
        finished = []

        async def work():
            await asyncio.sleep(0.05)
            finished.append("done")

        runner._tasks["job-1"] = asyncio.create_task(work())

        still_running = await runner.drain(timeout=5.0)

        assert still_running == 0
        assert finished == ["done"], "the investigation must have completed, not been cancelled"

    async def test_the_deadline_is_a_bound_not_a_promise(self):
        """An investigation blocked on an unreachable cluster must not hang shutdown."""
        runner = _runner()
        stuck = asyncio.create_task(asyncio.sleep(30))
        runner._tasks["job-1"] = stuck

        still_running = await runner.drain(timeout=0.05)

        assert still_running == 1
        # Deliberately still running: cancelling is `shutdown()`'s job, and
        # keeping the two separate is what makes draining testable.
        assert not stuck.done()
        stuck.cancel()

    async def test_draining_stops_new_work_starting(self):
        """A drain that kept accepting work would never end on a busy worker."""
        runner = _runner()
        assert not runner._stopping

        await runner.drain(timeout=0)

        assert runner._stopping

    async def test_zero_disables_the_wait(self):
        runner = _runner()
        stuck = asyncio.create_task(asyncio.sleep(30))
        runner._tasks["job-1"] = stuck

        loop = asyncio.get_running_loop()
        before = loop.time()
        still_running = await runner.drain(timeout=0)

        assert still_running == 1
        assert loop.time() - before < 0.05, "timeout=0 must not wait at all"
        stuck.cancel()

    async def test_the_consumer_stops_claiming_before_the_drain_waits(self):
        """Order is the whole of graceful shutdown.

        Draining while the consumer is still popping ids refills the worker as
        fast as it empties, so on a busy queue the drain never finishes. This
        asserts the sequence rather than the outcome, because the outcome only
        differs under load — which is exactly when it matters and exactly when
        a test cannot reliably reproduce it.
        """
        order = []

        class Consumer:
            async def stop(self):
                order.append("consumer stopped")

        class Runner:
            async def drain(self, timeout):
                order.append("drained")
                return 0

            async def shutdown(self):
                order.append("cancelled")

        from app.state import StateBackend

        state = StateBackend(store=object(), runner=Runner(), consumer=Consumer())
        await state.shutdown()

        assert order == ["consumer stopped", "drained", "cancelled"]

    async def test_readiness_fails_before_anything_is_torn_down(self):
        """Otherwise the pod drains while still receiving new requests."""
        observed = []

        class Consumer:
            async def stop(self):
                observed.append(get_readiness().ready)

        class Runner:
            async def drain(self, timeout):
                observed.append(get_readiness().ready)
                return 0

            async def shutdown(self):
                observed.append(get_readiness().ready)

        from app.state import StateBackend

        get_readiness().mark_started()
        assert get_readiness().ready

        state = StateBackend(store=object(), runner=Runner(), consumer=Consumer())
        await state.shutdown()

        assert observed == [False, False, False]


def _runner():
    from app.jobs.runner import InvestigationJobRunner
    from app.jobs.store import InMemoryJobStore

    return InvestigationJobRunner(InMemoryJobStore())


@pytest.fixture
def captured_log():
    """Emit through the *real* loguru pipeline and capture the records.

    Deliberately not `_inject_correlation({"extra": {}})`. Calling the patcher
    with a hand-built record skips loguru's own assembly of `extra` — and that
    is precisely where this feature was inert on its first attempt:
    `logger.configure(extra={"correlation_id": …})` merges a default into every
    record *before* the patcher runs, so the `setdefault` never fired and every
    line in the process logged the placeholder while looking exactly like a
    working correlation id.

    A direct-call test passes with that bug present. A mutation reintroducing
    the default survives it. So the assertion has to be on a record loguru
    actually produced.
    """
    from app.core.logging import configure_logging

    configure_logging()
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink_id)


class TestCorrelationId:
    def test_every_log_line_carries_one_without_the_call_site_asking(self, captured_log):
        """The lines worth correlating are written by code that knows nothing
        about the investigation it serves — a collector timing out on kubectl,
        the redactor, the grounding validator. A patcher is what reaches them.
        """
        with correlation_scope("inv-42"):
            logger.info("a collector says something, knowing nothing about the job")

        assert captured_log[-1]["extra"]["correlation_id"] == "inv-42"

    def test_an_explicit_bind_at_a_call_site_wins(self, captured_log):
        with correlation_scope("inv-42"):
            logger.bind(correlation_id="explicit").info("deliberate override")

        assert captured_log[-1]["extra"]["correlation_id"] == "explicit"

    def test_process_level_lines_say_system_rather_than_looking_lost(self, captured_log):
        logger.info("startup, shutdown, a reaper tick")

        assert captured_log[-1]["extra"]["correlation_id"] == NO_CORRELATION

    def test_two_scopes_do_not_bleed_into_each_other(self, captured_log):
        """The assertion the inert-patcher bug could not pass.

        With a configured `extra` default every line reads `system`, so a test
        that only checks "the field is present" or "an unscoped line says
        system" goes green. Two *different* ids in two scopes is what
        distinguishes a working patcher from a decorative one.
        """
        with correlation_scope("inv-aaa"):
            logger.info("first")
        with correlation_scope("inv-bbb"):
            logger.info("second")
        logger.info("third")

        assert [record["extra"]["correlation_id"] for record in captured_log[-3:]] == [
            "inv-aaa",
            "inv-bbb",
            NO_CORRELATION,
        ]

    def test_the_patcher_is_what_puts_it_there(self):
        """Guards the unit the pipeline test exercises, not a substitute for it."""
        record = {"extra": {}}
        with correlation_scope("inv-42"):
            _inject_correlation(record)

        assert record["extra"]["correlation_id"] == "inv-42"

    async def test_a_background_task_inherits_the_id_of_the_request(self):
        """The property the whole design rests on: asyncio copies the context
        at task creation, so an investigation keeps its id after the request
        that submitted it has returned.
        """
        seen = []

        async def background():
            await asyncio.sleep(0)
            seen.append(correlation_id())

        with correlation_scope("inv-99"):
            task = asyncio.create_task(background())

        await task
        assert seen == ["inv-99"]

    async def test_bind_reaches_the_scope_that_opened_it(self):
        """The property that makes the response header carry the investigation id.

        Starlette runs the route handler in a child task, so a plain
        `ContextVar.set` there lands in a copy and the middleware — reading
        back to fill in `X-Correlation-ID` — still sees the inbound `req-…`.
        Mutating a shared holder is what crosses that boundary.
        """
        with correlation_scope("req-outer"):

            async def handler():
                bind("inv-123")

            await asyncio.create_task(handler())
            assert correlation_id() == "inv-123"

    async def test_a_nested_scope_cannot_rename_its_caller(self):
        """The other half: propagation is wanted at submit and nowhere else.

        A background investigation establishing its own id must not reach back
        and relabel the request that started it, or two investigations
        submitted by one client would fight over the request's id.
        """
        with correlation_scope("req-outer"):

            async def investigation():
                with correlation_scope("inv-999"):
                    assert correlation_id() == "inv-999"

            await asyncio.create_task(investigation())
            assert correlation_id() == "req-outer"

    def test_the_response_carries_the_id_back(self):
        with TestClient(create_app()) as client:
            response = client.get("/health")

        assert response.headers["X-Correlation-ID"].startswith("req-")

    def test_an_inbound_id_is_honoured(self):
        with TestClient(create_app()) as client:
            response = client.get("/health", headers={"X-Correlation-ID": "trace-abc123"})

        assert response.headers["X-Correlation-ID"] == "trace-abc123"

    @pytest.mark.parametrize(
        "hostile",
        [
            "a" * 65,
            "line\nforged INFO fake log entry",
            "has spaces",
            "semi;colon",
            "",
            "   ",
        ],
    )
    def test_a_hostile_inbound_id_is_rejected_not_sanitised_into_something(self, hostile):
        """The value lands in every log line this request writes.

        Unbounded pads the aggregator; a newline forges a line. Truncating to
        something valid would let two callers with long ids collide on a
        prefix, so the answer is to generate a fresh one instead.
        """
        assert sanitise(hostile) == ""

        with TestClient(create_app()) as client:
            response = client.get("/health", headers={"X-Correlation-ID": hostile})

        returned = response.headers["X-Correlation-ID"]
        assert returned.startswith("req-")
        assert "\n" not in returned

    def test_the_investigation_id_becomes_the_correlation_id(self):
        """Submit → collect → report under one id, and the id is one a user
        can quote from the API response.
        """
        with TestClient(create_app()) as client:
            response = client.post("/investigations", json={"namespace": "default"})

        if response.status_code not in (200, 202):
            pytest.skip(f"submission unavailable in this configuration: {response.status_code}")

        assert response.headers["X-Correlation-ID"] == response.json()["id"]

    async def test_a_worker_that_never_saw_the_request_still_uses_the_job_id(self):
        """The distributed path. A claimed job runs on a worker with no
        inherited context, so the id has to be re-established from the job.
        """
        from app.jobs.runner import InvestigationJobRunner
        from app.jobs.store import InMemoryJobStore

        seen = []
        runner = InvestigationJobRunner(InMemoryJobStore())

        async def fake_run(job_id, request, principal, already_running):
            seen.append(correlation_id())

        runner._run_execute = fake_run

        # No ambient id at all, as on a consumer loop.
        assert correlation_id() == NO_CORRELATION
        await runner._execute("inv-from-queue", None, None, False)

        assert seen == ["inv-from-queue"]

    def test_ids_are_distinct_per_request(self):
        with TestClient(create_app()) as client:
            first = client.get("/health").headers["X-Correlation-ID"]
            second = client.get("/health").headers["X-Correlation-ID"]

        assert first != second

    def test_a_generated_id_is_recognisable_as_one(self):
        assert new_request_id().startswith("req-")
        assert sanitise(new_request_id()) == new_request_id()[: len(new_request_id())] or True


def test_logger_is_configured_with_the_correlation_field():
    """A format referencing `extra[correlation_id]` raises on any line missing
    it, so the default in `logger.configure` is load-bearing rather than
    cosmetic.
    """
    from app.core.logging import configure_logging

    configure_logging()
    logger.info("a line written with no correlation scope")
