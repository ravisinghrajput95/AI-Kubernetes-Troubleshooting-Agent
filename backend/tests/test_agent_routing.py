"""M8a: sending an investigation to the worker that can actually collect it.

A gRPC stream belongs to whichever worker holds the socket, so a cluster
reachable only through an agent has exactly one worker that can investigate it.
Nothing expressed that before this milestone. The job went onto the shared
queue, any worker claimed it, and a worker without the stream fell back to
`LocalKubectlProvider` — so on three replicas roughly two thirds of
agent-cluster investigations were answered by the platform's own kubeconfig
instead of by the cluster.

That is not a slow answer, it is a **wrong** one. `LocalKubectlProvider`
resolves the cluster's *name* against whatever contexts the platform holds and
has no notion of a tenant, while M6 made `AgentRegistry` tenant-keyed precisely
so two customers could both call a cluster `prod`. Evidence filed under a
cluster it was not read from is the outcome the evidence spine exists to
prevent.

Two mechanisms, tested here:

- **routing** — the submit path asks presence who holds the stream and queues
  the work on that worker's queue;
- **refusal** — if selection still lands somewhere that cannot reach the agent,
  it refuses and names the worker rather than reading a same-named local
  context.

Routing is a hint and refusal is the guarantee. That split is deliberate: a
hint that is occasionally wrong costs a retry, whereas a guarantee that is
occasionally wrong costs a misdiagnosis.
"""

from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.gateway.presence import PRESENCE_TTL_SECONDS, AgentPresence
from app.jobs.consumer import UNCLAIMED_GRACE_SECONDS
from app.jobs.runner import agent_affinity
from app.models.investigation import InvestigationRequest
from app.providers.base import ClusterUnreachable
from app.providers.local_kubectl import LocalKubectlProvider
from app.services.investigation_service import select_provider
from app.tenancy import tenant_scope
from tests.test_agent_presence import FakeBus, FakeSession


class RegisteredSession(FakeSession):
    """A `FakeSession` the real `AgentRegistry` will accept.

    Uses the genuine registry rather than a stub, because the property under
    test is "affinity asks the local registry first" — a stubbed registry would
    pass whether or not it was consulted the way `select_provider` consults it.
    """

    @property
    def key(self) -> tuple[str, str]:
        return (self.tenant, self.cluster_id)


@pytest.fixture
def gateway(monkeypatch):
    """A deployment with the agent gateway switched on."""
    monkeypatch.setattr(settings, "agent_gateway_port", 9443)
    return settings


@pytest.fixture
def registry(monkeypatch):
    """A real, empty agent registry installed as the process-wide one."""
    from app.gateway import session as session_module

    registry = session_module.AgentRegistry()
    monkeypatch.setattr(session_module, "_registry", registry)
    return registry


@pytest.fixture
def presence(monkeypatch):
    """Install a presence index and hand back a factory for other workers."""
    from app.gateway import presence as presence_module

    bus = FakeBus()

    def install(worker_id: str) -> AgentPresence:
        index = AgentPresence(bus, worker_id)
        monkeypatch.setattr(presence_module, "_presence", index)
        return index

    install.bus = bus  # type: ignore[attr-defined]
    return install


# --- routing ----------------------------------------------------------------


class TestWorkRoutesToTheWorkerHoldingTheStream:
    def test_an_agent_cluster_is_routed_to_its_holder(self, gateway, presence):
        """The whole point: worker-b holds the stream, so worker-b gets the job."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == "worker-b"

    def test_the_worker_holding_the_stream_pins_the_job_to_itself(
        self, gateway, presence, registry
    ):
        """The case that was inverted, and the one a two-replica fleet hits half the time.

        `holder()` returns nothing when the presence record names *this*
        worker — right for `select_provider`, which only consults it after the
        local registry has said no, so a self-record there is stale. Affinity
        had no such check, so a submission landing on the worker that actually
        holds the stream fell through to the **shared** queue and could be
        claimed by anyone.

        Found in-cluster, not here: on two replicas one investigation in three
        reached the agent and the rest were refused naming the worker that had
        just accepted the submission.
        """
        index = presence("worker-a")
        index.announce(FakeSession("prod-eu"))
        registry.register(RegisteredSession("prod-eu"))

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == "worker-a"

    def test_a_cluster_with_no_agent_goes_to_the_shared_queue(self, gateway, presence):
        """A kubeconfig cluster can be collected anywhere, so it should be."""
        presence("worker-a")

        assert agent_affinity(InvestigationRequest(context="laptop")) == ""

    def test_a_lapsed_record_routes_nowhere(self, gateway, presence):
        """A worker that stopped heartbeating must not keep attracting work."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")
        presence.bus.expire(f"{presence.bus.prefix}:agents:default:prod-eu")

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == ""

    def test_an_unnamed_cluster_goes_to_the_shared_queue(self, gateway, presence):
        """No context means the local kubeconfig's current one; no agent serves that."""
        presence("worker-a")

        assert agent_affinity(None) == ""
        assert agent_affinity(InvestigationRequest()) == ""

    def test_another_tenants_agent_does_not_attract_this_tenants_work(self, gateway, presence):
        """Two customers may both call a cluster `prod`. M6 keyed the registry
        by tenant for exactly that reason; routing must not undo it."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod", tenant="globex"))
        presence("worker-a")

        with tenant_scope("acme"):
            assert agent_affinity(InvestigationRequest(context="prod")) == ""
        with tenant_scope("globex"):
            assert agent_affinity(InvestigationRequest(context="prod")) == "worker-b"

    def test_a_worker_without_a_gateway_still_routes(self, monkeypatch, presence):
        """F21. This test used to assert the opposite, and the docstring said
        why: "a kubeconfig-only deployment must not pay for a feature it cannot
        use". That reasoning is right and the guard was in the wrong place.

        A worker with no gateway holds no streams — but it can still be *told*
        that another worker holds one, and queue the work there. Presence is
        JSON in Redis and needs no grpc. The thing a kubeconfig-only deployment
        must not pay for is the presence index itself, and not having one is
        already the guard: see the single-process test below."""
        monkeypatch.setattr(settings, "agent_gateway_port", 0)
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == "worker-b"

    def test_a_single_process_deployment_routes_nowhere(self, gateway, monkeypatch):
        """There is one worker, so affinity is already satisfied and naming it
        would only create a queue nobody else reads."""
        from app.gateway import presence as presence_module

        monkeypatch.setattr(presence_module, "_presence", None)

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == ""


class TestTheQueueIsPartitioned:
    def test_a_worker_queue_is_distinct_from_the_shared_one(self):
        from app.persistence.redis_bus import RedisBus

        bus = RedisBus.__new__(RedisBus)
        bus._prefix = "k8sagent"

        assert bus.worker_queue_key("w1") != bus.queue_key
        assert bus.worker_queue_key("w1") != bus.worker_queue_key("w2")

    def test_a_consumer_reads_its_own_queue_before_the_shared_one(self):
        """`BLPOP` returns from the first non-empty key, so the order *is* the
        priority. Reversing it would let a busy shared queue starve the work
        only this worker can do."""
        from app.persistence.redis_bus import RedisBus

        bus = RedisBus.__new__(RedisBus)
        bus._prefix = "k8sagent"

        captured: list[list[str]] = []

        class Async:
            async def blpop(self, keys, timeout):
                captured.append(list(keys))
                return None

        bus._async = Async()

        import asyncio

        asyncio.run(bus.dequeue(worker_id="w1"))
        assert captured == [[bus.worker_queue_key("w1"), bus.queue_key]]


class TestAffinityDecays:
    def test_presence_lapses_before_an_unclaimed_job_is_re_offered(self):
        """The ordering that makes routing recovery terminate.

        A job routed to a worker that then dies waits on that worker's queue
        until the reaper re-offers it after `UNCLAIMED_GRACE_SECONDS`. If
        presence outlived the grace period, the re-offer would route it
        straight back to the same dead worker — forever. Because the record
        lapses first, the re-offer reaches the shared queue and whoever picks
        it up either holds the stream or refuses honestly.

        Mutation: raise `PRESENCE_TTL_SECONDS` above the grace period.
        """
        assert PRESENCE_TTL_SECONDS < UNCLAIMED_GRACE_SECONDS, (
            "A dead worker would keep attracting the jobs it can never run."
        )

    def test_a_re_offer_goes_to_the_shared_queue_never_a_worker_queue(self):
        """Otherwise a dead worker's queue is where investigations go to die."""
        import inspect

        from app.jobs.distributed import PostgresRedisJobStore

        source = inspect.getsource(PostgresRedisJobStore.requeue_unclaimed)
        assert "self._bus.enqueue(job_id)" in source, (
            "requeue_unclaimed must re-offer without an affinity; passing one "
            "would strand jobs on the queue of the worker that just died."
        )


# --- refusal ----------------------------------------------------------------


class TestSelectionRefusesRatherThanReadingTheWrongCluster:
    def test_an_agent_on_another_worker_refuses(self, gateway, registry, presence):
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        with pytest.raises(ClusterUnreachable, match="worker-b"):
            select_provider("prod-eu", None)

    def test_the_refusal_names_the_cluster_and_the_worker(self, gateway, registry, presence):
        """An operator has to be able to act on it, and 'check your kubeconfig'
        is precisely the wrong instruction here."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        with pytest.raises(ClusterUnreachable) as raised:
            select_provider("prod-eu", None)

        message = str(raised.value)
        assert "prod-eu" in message
        assert "worker-b" in message
        assert "kubeconfig" in message

    def test_no_agent_anywhere_still_falls_back_to_the_kubeconfig(
        self, gateway, registry, presence
    ):
        """The getting-started path, and a genuinely kubeconfig-only cluster.
        The refusal must be about a *known* agent elsewhere, not about the
        absence of one."""
        presence("worker-a")

        assert isinstance(select_provider("laptop", None), LocalKubectlProvider)

    def test_an_agent_attached_here_is_used(self, gateway, registry, presence):
        from app.gateway import session as session_module

        session = FakeSession("prod-eu")
        session.cancel_all = lambda reason: None  # registry may evict an older one
        session.key = ("default", "prod-eu")
        session_module.get_agent_registry().register(session)
        presence("worker-a")

        provider = select_provider("prod-eu", None)
        assert type(provider).__name__ == "RemoteAgentProvider"

    def test_a_single_process_deployment_never_refuses(self, gateway, registry, monkeypatch):
        """With one process the local registry *is* the fleet; there is no
        other worker to be wrong about, and refusing would break the
        kubeconfig-plus-gateway deployment."""
        from app.gateway import presence as presence_module

        monkeypatch.setattr(presence_module, "_presence", None)

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_an_unreadable_presence_index_does_not_refuse(self, gateway, registry, monkeypatch):
        """A Redis hiccup must not become an outage. We cannot prove another
        worker holds the agent, so the pre-M8a answer stands and
        `cluster_access` still reports which route was taken."""
        from app.gateway import presence as presence_module

        class Broken:
            def holder(self, tenant, cluster_id):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(presence_module, "_presence", Broken())

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_another_tenants_agent_does_not_cause_a_refusal(self, gateway, registry, presence):
        """`globex` having an agent called `prod` says nothing about `acme`'s
        `prod`, which may legitimately be a kubeconfig cluster."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod", tenant="globex"))
        presence("worker-a")

        with tenant_scope("acme"):
            assert isinstance(select_provider("prod", None), LocalKubectlProvider)


class TestTheRefusalReachesTheCaller:
    """A failure an operator can act on has to survive the layers above it.

    The generic detail tells them to check kubeconfig, cluster access and
    kubectl permissions — all irrelevant when the cluster is reachable and
    simply attached to another worker.
    """

    async def test_a_job_records_the_specific_reason(self, monkeypatch):
        from app.jobs.runner import InvestigationJobRunner
        from app.jobs.store import InMemoryJobStore
        from app.services.investigation_runner import FAILURE_DETAIL

        async def refuse(*args, **kwargs):
            raise ClusterUnreachable("The agent for cluster 'prod-eu' is on worker-b.")

        monkeypatch.setattr("app.jobs.runner.run_investigation", refuse)

        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)
        job = runner.submit(InvestigationRequest(context="prod-eu"))

        import asyncio

        await asyncio.sleep(0.05)

        finished = store.get(job.id)
        assert finished.error != FAILURE_DETAIL
        assert "worker-b" in finished.error


# --- across real workers ----------------------------------------------------
#
# Everything above uses a fake bus, which proves the decision. These prove the
# *mechanism*: two consumers, one Redis, and a job that has to reach exactly one
# of them. A single-process test cannot have this property at all.


@pytest.fixture
async def fleet():
    from tests.distributed_backend import INTEGRATION_ENABLED, SKIP_REASON, DistributedBackend

    if not INTEGRATION_ENABLED:
        pytest.skip(SKIP_REASON)

    resource = DistributedBackend()
    try:
        yield resource
    finally:
        await resource.close()


class TestRoutingAcrossRealWorkers:
    async def test_a_routed_job_reaches_only_the_holding_worker(self, fleet):
        """The exit criterion, at its smallest: worker-b holds the stream, so
        worker-a must not be offered the job at all."""
        store = fleet.store()
        store.enqueue("job-for-b", "worker-b")

        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-a") is None
        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-b") == "job-for-b"

    async def test_an_unrouted_job_is_offered_to_anyone(self, fleet):
        store = fleet.store()
        store.enqueue("job-for-anyone")

        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-a") == "job-for-anyone"

    async def test_a_worker_prefers_its_own_queue(self, fleet):
        """Affinity work is the work only this worker can do; shared work is
        work anyone can. Draining the shared queue first would let a busy fleet
        starve the agent clusters."""
        store = fleet.store()
        store.enqueue("shared-1")
        store.enqueue("mine-1", "worker-a")

        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-a") == "mine-1"
        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-a") == "shared-1"

    async def test_a_dead_workers_queue_is_drained_by_the_re_offer(self, fleet):
        """Routing must not be able to strand work permanently.

        worker-b is handed a job and then never runs again. The reaper re-offers
        it to the *shared* queue, where a live worker picks it up — and by then
        worker-b's presence has lapsed, so nothing routes it back.
        """
        store = fleet.store()
        job = store.create({"context": "prod-eu"})
        store.enqueue(job.id, "worker-b")

        assert fleet.bus.queue_depth("worker-b") == 1
        assert fleet.bus.queue_depth() == 0

        # `older_than_seconds=0` is the passage of the grace period.
        assert store.requeue_unclaimed(0) == [job.id]

        assert fleet.bus.queue_depth() == 1, "the re-offer must reach the shared queue"
        assert await fleet.bus.dequeue(timeout=1, worker_id="worker-a") == job.id

    async def test_a_routed_job_is_still_claimed_exactly_once(self, fleet):
        """Affinity is a hint; the conditional UPDATE is the guarantee.

        If routing were load-bearing, a mis-route would be a double run. It is
        not: both workers may see the id, and only one claim matches a row.
        """
        worker_a, worker_b = fleet.store(), fleet.store()
        job = worker_a.create({"context": "prod-eu"})

        claimed = [
            worker_a.claim(job.id, "worker-a", 30),
            worker_b.claim(job.id, "worker-b", 30),
        ]
        assert [bool(one) for one in claimed].count(True) == 1


class TestAStaleRecordNamingThisWorker:
    """The presence index can name *us*, and it is never a destination.

    `holder()` is consulted only after the local registry has already said no,
    so a record still claiming this worker means the agent disconnected here
    and the record has not yet lapsed. Treating it as a holder would route work
    to a queue this worker is already draining, and would make the refusal read
    "the agent is attached to worker-a, not this one" — where worker-a is this
    one. Found by reasoning through a two-worker run, not by a test.
    """

    def test_a_record_naming_this_worker_is_not_a_routing_target(self, gateway, presence):
        index = presence("worker-a")
        index.announce(FakeSession("prod-eu"))  # announced by, and naming, worker-a

        assert index.holder("default", "prod-eu") is None
        assert agent_affinity(InvestigationRequest(context="prod-eu")) == ""

    def test_selection_falls_back_rather_than_refusing_against_itself(
        self, gateway, registry, presence
    ):
        index = presence("worker-a")
        index.announce(FakeSession("prod-eu"))
        # The registry is empty: the agent disconnected from this worker.

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_another_workers_record_is_still_a_destination(self, gateway, presence):
        """The fix must not disable routing altogether."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == "worker-b"


class TestWorkerConcurrencyIsConfigurable:
    """The platform's real concurrency ceiling, and why it moved.

    `max_concurrent` was a constructor default of 4 that `state.py` never
    passed, so every deployment ran four investigations per worker with no way
    to change it. Reaching the roadmap's 5,000 concurrent would have needed
    1,250 workers — and not because of memory: peak heap is about 5x the stored
    result, so 13.4 MB at the `MAX_LIST_ITEMS` ceiling, or roughly 76
    investigations per GB (`scripts/payload_bench.py --memory`).

    The default stays 4. Memory is not the only cost — collection and analysis
    occupy worker threads and anyio's pool defaults to 40 — so raising it is an
    operator's decision against their own cluster sizes, not one to inherit.
    """

    def _consumer(self, **kwargs):
        from app.jobs.consumer import JobConsumer

        return JobConsumer(store=None, runner=None, bus=None, worker_id="w", **kwargs)

    def test_the_default_is_unchanged(self, monkeypatch):
        """An existing deployment must not silently start running more work."""
        assert settings.job_max_concurrent == 4
        assert self._consumer()._max_concurrent == 4

    def test_configuration_raises_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(settings, "job_max_concurrent", 64)
        assert self._consumer()._max_concurrent == 64

    def test_an_explicit_argument_still_wins(self, monkeypatch):
        """Tests construct consumers directly and must stay able to pin it."""
        monkeypatch.setattr(settings, "job_max_concurrent", 64)
        assert self._consumer(max_concurrent=1)._max_concurrent == 1

    def test_the_setting_is_reachable_from_the_environment(self):
        """Catches: adding the field without its validation alias, which reads
        as configurable and is not."""
        from app.core.config import Settings

        assert Settings(JOB_MAX_CONCURRENT=32).job_max_concurrent == 32

    def test_zero_concurrency_is_refused(self):
        """A worker that runs nothing looks healthy and drains no queue."""
        import pydantic

        from app.core.config import Settings

        with pytest.raises(pydantic.ValidationError):
            Settings(JOB_MAX_CONCURRENT=0)


class TestARevokedAgentIsNotQuietlyReplacedByTheKubeconfig:
    """Revoking says "this agent must not serve". Reading a local context that
    merely shares the cluster's name is the opposite of that.

    **Revoked and disconnected are different, and only the first refuses.** An
    agent that dropped a moment ago still holds a valid certificate and will
    reconnect; refusing there would turn every flap into an outage, which is
    exactly why presence is TTL-based. The distinction is the whole feature —
    a version that refused for both would pass the first test here and fail the
    second.

    Found by verifying revocation against a live deployment: the investigation
    after a revoke came back `provider=kubeconfig`, and failed only because that
    cluster had no same-named local context. One that did would have read the
    wrong cluster and filed it as evidence for the right one.
    """

    def _store(self, monkeypatch, records):
        from app.services import investigation_service

        class Store:
            def certificates(self, cluster_id=""):
                return [r for r in records if r.cluster_id == cluster_id]

        monkeypatch.setattr(
            "app.security.enrolment.get_enrolment_store", lambda: Store(), raising=False
        )
        return investigation_service

    def _cert(self, cluster, revoked=False, expired=False):
        from datetime import timedelta

        from app.security.enrolment import CertificateRecord

        now = datetime.now(UTC)
        return CertificateRecord(
            serial=f"{cluster}-{revoked}-{expired}",
            cluster_id=cluster,
            issued_at=now - timedelta(days=1),
            expires_at=now - timedelta(hours=1) if expired else now + timedelta(days=30),
            revoked_at=now if revoked else None,
        )

    def test_a_revoked_cluster_refuses_instead_of_falling_back(
        self, gateway, registry, presence, monkeypatch
    ):
        self._store(monkeypatch, [self._cert("prod-eu", revoked=True)])
        presence("worker-a")

        with pytest.raises(ClusterUnreachable, match="revoked"):
            select_provider("prod-eu", None)

    def test_a_merely_disconnected_agent_still_falls_back(
        self, gateway, registry, presence, monkeypatch
    ):
        """The availability half. This is the common case — an agent restarting,
        a node draining — and it must not become a refusal."""
        self._store(monkeypatch, [self._cert("prod-eu", revoked=False)])
        presence("worker-a")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_a_replacement_agent_lifts_the_refusal(self, gateway, registry, presence, monkeypatch):
        """Re-enrolling is the documented remedy, so it has to work without an
        operator also having to un-revoke anything."""
        self._store(
            monkeypatch,
            [self._cert("prod-eu", revoked=True), self._cert("prod-eu", revoked=False)],
        )
        presence("worker-a")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_an_expired_replacement_does_not_lift_it(
        self, gateway, registry, presence, monkeypatch
    ):
        """Expired is not valid. A stale record must not read as a live agent."""
        self._store(
            monkeypatch,
            [
                self._cert("prod-eu", revoked=True),
                self._cert("prod-eu", revoked=False, expired=True),
            ],
        )
        presence("worker-a")

        with pytest.raises(ClusterUnreachable, match="revoked"):
            select_provider("prod-eu", None)

    def test_an_expired_certificate_that_was_never_revoked_still_falls_back(
        self, gateway, registry, presence, monkeypatch
    ):
        """Lapsed is not revoked, and only revoking asked us to stop.

        This case is the difference between the two conditions in
        `_agent_was_revoked`, and nothing else here distinguishes them: without
        it, replacing the "was anything revoked?" test with "are there any
        records?" passes the whole class while quietly refusing every cluster
        whose certificate simply ran out. Added because that mutation survived.
        """
        self._store(monkeypatch, [self._cert("prod-eu", revoked=False, expired=True)])
        presence("worker-a")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_a_cluster_that_never_enrolled_is_untouched(
        self, gateway, registry, presence, monkeypatch
    ):
        """The getting-started path: no agent was ever issued for this name."""
        self._store(monkeypatch, [])
        presence("worker-a")

        assert isinstance(select_provider("laptop", None), LocalKubectlProvider)

    def test_an_unreadable_enrolment_store_refuses(self, gateway, registry, presence, monkeypatch):
        """Fails closed, like M8a's own refusal and unlike the rate limiter.
        The alternative is answering from the wrong cluster, and the only
        investigations affected are ones that already have no agent."""

        class Broken:
            def certificates(self, cluster_id=""):
                raise RuntimeError("enrolment store unavailable")

        monkeypatch.setattr(
            "app.security.enrolment.get_enrolment_store", lambda: Broken(), raising=False
        )
        presence("worker-a")

        with pytest.raises(ClusterUnreachable):
            select_provider("prod-eu", None)


class TestTheGuaranteeDoesNotDependOnThisWorkerRunningAGateway:
    """F21: M8a's routing and its refusal were both inert on a worker whose own
    `AGENT_GATEWAY_PORT` was unset.

    The presence index and the enrolment store were installed inside the
    gateway branch of `app/state.py`, so on such a worker `get_agent_presence()`
    was `None` and the enrolment store fell back to an empty local file.
    `agent_affinity` returned the shared queue and `select_provider` went
    straight to `LocalKubectlProvider` — reading a local context that merely
    shares the cluster's *name*, with no tenant, which is the exact
    cross-tenant answer M8a's refusal exists to prevent.

    The shipped topology cannot reach it: one Deployment, one config, N
    replicas. A fleet part-way through enabling `AGENT_GATEWAY_PORT` can, and
    there the guarantee was absent with no symptom at all. Found by a soak that
    gave the first worker a gateway and the second none: a third of
    investigations came back `provider=kubeconfig` with an agent attached and
    no refusal logged anywhere.

    Every test here runs with the gateway *off*, which is the point.
    """

    @pytest.fixture(autouse=True)
    def no_gateway(self, monkeypatch):
        monkeypatch.setattr(settings, "agent_gateway_port", 0)

    def test_it_refuses_rather_than_reading_a_same_named_local_context(self, presence):
        """The one that matters. Before F21 this returned a
        `LocalKubectlProvider` for tenant A's `prod` pointed at whatever
        context the platform's own kubeconfig calls `prod`."""
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        with pytest.raises(ClusterUnreachable) as refusal:
            select_provider("prod-eu", None)
        assert "worker-b" in str(refusal.value)

    def test_it_routes_to_the_holder(self, presence):
        AgentPresence(presence.bus, "worker-b").announce(FakeSession("prod-eu"))
        presence("worker-a")

        assert agent_affinity(InvestigationRequest(context="prod-eu")) == "worker-b"

    def test_a_cluster_with_no_agent_anywhere_still_uses_the_kubeconfig(self, presence):
        """The other half. Refusing everything would be a different outage, and
        a fleet with no agents at all must be unaffected by F21."""
        presence("worker-a")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_the_single_process_default_is_untouched(self, monkeypatch):
        """No gateway *and* no shared state is `uvicorn app.main:app` against a
        kubeconfig — the getting-started path. There is no fleet to consult and
        nothing may refuse.

        """
        from app.gateway import presence as presence_module

        monkeypatch.setattr(presence_module, "_presence", None)
        monkeypatch.setattr(settings, "database_url", "")
        monkeypatch.setattr(settings, "redis_url", "")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)
        assert agent_affinity(InvestigationRequest(context="prod-eu")) == ""

    def test_an_unreadable_enrolment_store_does_not_refuse_the_default_path(self, monkeypatch):
        """The guard that keeps F21's fix from becoming a different outage.

        `_agent_was_revoked` **refuses when it cannot read the store** — right
        when a revoked agent is possible, because answering from a same-named
        local context is worse. F21 moved it out from behind the gateway flag,
        which put it on the single-process path, where `get_enrolment_store()`
        lazily builds a file store under `AGENT_IDENTITY_DIR`. An unreadable or
        corrupt file there would then refuse *every* investigation on the
        getting-started path — a deployment where no agent can ever have
        attached, because there is no gateway for one to attach to.

        Written after the first version of this test passed with the guard
        removed: a store that merely has no records returns an empty list, so
        it proved nothing. Only a store that *raises* can tell the two apart."""
        from app.gateway import presence as presence_module
        from app.security import enrolment as enrolment_module

        class Unreadable:
            def certificates(self, cluster: str):
                raise OSError("enrolment.json is not readable")

        monkeypatch.setattr(presence_module, "_presence", None)
        monkeypatch.setattr(enrolment_module, "_store", Unreadable())
        monkeypatch.setattr(settings, "database_url", "")
        monkeypatch.setattr(settings, "redis_url", "")

        assert isinstance(select_provider("prod-eu", None), LocalKubectlProvider)

    def test_the_same_unreadable_store_does_refuse_where_an_agent_could_exist(
        self, monkeypatch, presence
    ):
        """The control, without which the test above is satisfied by a
        revocation check that never refuses at all."""
        from app.security import enrolment as enrolment_module

        class Unreadable:
            def certificates(self, cluster: str):
                raise OSError("enrolment.json is not readable")

        presence("worker-a")
        monkeypatch.setattr(enrolment_module, "_store", Unreadable())
        monkeypatch.setattr(settings, "database_url", "postgres://stub")
        monkeypatch.setattr(settings, "redis_url", "redis://stub")

        with pytest.raises(ClusterUnreachable):
            select_provider("prod-eu", None)

    def test_a_revoked_agent_is_refused_here_too(self, monkeypatch, presence):
        """Revocation is the second thing that was inert, for the same reason:
        the enrolment store was the gateway's as well. A revoked agent leaves
        no presence record, so without this the fallback reads a local context
        — the opposite of what revoking asked for."""
        from datetime import timedelta

        presence("worker-a")
        monkeypatch.setattr(settings, "database_url", "postgres://stub")
        monkeypatch.setattr(settings, "redis_url", "redis://stub")

        class Record:
            def __init__(self, revoked: bool) -> None:
                self.revoked = revoked
                self.expires_at = datetime.now(UTC) + timedelta(hours=1)

        class Store:
            def certificates(self, cluster: str):
                return [Record(revoked=True)]

        from app.security import enrolment as enrolment_module

        monkeypatch.setattr(enrolment_module, "_store", Store())

        with pytest.raises(ClusterUnreachable) as refusal:
            select_provider("prod-eu", None)
        assert "revoked" in str(refusal.value)


class TestTheFleetIndexIsInstalledWithTheStateBackend:
    """The structural half of F21, asserted on the wiring rather than on
    behaviour — because behaviour cannot see it.

    Every test above monkeypatches a presence index into place, so they pass
    whether or not startup would ever install one. That is precisely the shape
    of the original defect: the tests proved the routing logic and the wiring
    was wrong somewhere else entirely.
    """

    def test_build_state_installs_presence_and_the_enrolment_store(self):
        import inspect

        from app import state as state_module

        source = inspect.getsource(state_module.install_fleet_index)
        assert "set_agent_presence" in source
        assert "set_enrolment_store" in source
        # And `build_state` must call it, not just define it.
        assert "install_fleet_index(" in inspect.getsource(state_module.build_state)

    def test_the_gateway_no_longer_installs_them(self):
        """If it does, the two can disagree about which store is in effect, and
        the branch that shadowed them is back."""
        import inspect

        from app import state as state_module

        source = inspect.getsource(state_module.start_agent_gateway)
        assert "set_agent_presence" not in source
        assert "set_enrolment_store" not in source
