"""What the collection cache may and may not do.

F18: every investigation re-read the whole cluster, so two investigations a
minute apart did identical work and `collect` is 65% of an investigation. The
saving is easy; the part worth testing is the promise that comes with it.

**A cache that lies is worse than no cache**, and in this product "lies" is
specific. Every conclusion cites an evidence id and every evidence record
carries a `collected_at`. A record dated *now* for a fact read forty seconds ago
is a false citation, not an untidy one. So the tests here are mostly not about
speed: they are about the cache being unable to answer a question it was not
asked, and being unable to hide the age of what it serves.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.evidence_redactor import EvidenceRedactor
from app.auth.models import Principal
from app.collectors.registry import CollectorRegistry
from app.collectors.scheduler import CollectionScheduler
from app.core.config import settings
from app.evidence.models import Evidence, EvidenceStatus, ResourceRef
from app.providers.base import ProviderResult, ReadVerb, ResourceRequest
from app.providers.cache import (
    CachingProvider,
    CollectionCache,
    cache_scope,
    fingerprint,
    freshness_window,
    get_collection_cache,
    reset_collection_cache,
    underlying,
    with_cache,
)
from app.tenancy.context import tenant_scope

PODS = ResourceRequest(verb=ReadVerb.GET, resource="pods", all_namespaces=True)
NODES = ResourceRequest(verb=ReadVerb.GET, resource="nodes")


class RecordingProvider:
    """A provider that counts what it was actually asked to read."""

    def __init__(self, cluster_id: str = "prod", payload=None, success: bool = True) -> None:
        self._cluster_id = cluster_id
        self._payload = payload if payload is not None else {"items": [{"name": "web-0"}]}
        self._success = success
        self.reads: list[str] = []
        self.executed_commands: list[str] = []
        self.truncations: list[dict] = []

    @property
    def cluster_id(self) -> str:
        return self._cluster_id

    async def fetch(self, request):
        return (await self.fetch_many([request]))[0]

    async def fetch_many(self, requests):
        results = []
        for request in requests:
            self.reads.append(request.describe())
            command = f"kubectl {request.describe()}"
            self.executed_commands.append(command)
            results.append(
                ProviderResult(
                    success=self._success,
                    data=self._payload if self._success else None,
                    error="" if self._success else "forbidden",
                    equivalent_command=command,
                )
            )
        return results


def build(provider=None, ttl: float = 60.0, max_bytes: int = 1 << 20, read: bool = True):
    inner = provider or RecordingProvider()
    return inner, CachingProvider(inner, CollectionCache(ttl, max_bytes), "scope", read=read)


class TestTheKeyCannotCollideAcrossReads:
    def test_the_fingerprint_covers_every_field_of_a_request(self):
        """Derived from the dataclass, so a new field is covered before anyone
        remembers to cover it. An enumerated fingerprint that forgot
        `namespace` would answer a `payments` investigation with `kube-system`
        pods and file them as its evidence."""
        from dataclasses import fields

        baseline = ResourceRequest(verb=ReadVerb.GET, resource="pods")
        for spec in fields(ResourceRequest):
            assert f"{spec.name}=" in fingerprint(baseline), spec.name

    @pytest.mark.parametrize(
        "changed",
        [
            ResourceRequest(verb=ReadVerb.DESCRIBE, resource="pods"),
            ResourceRequest(verb=ReadVerb.GET, resource="nodes"),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", name="web-0"),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", namespace="payments"),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", all_namespaces=True),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", label_selector="app=web"),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", field_selector="status=Bad"),
            ResourceRequest(verb=ReadVerb.GET, resource="pods", options={"previous": True}),
        ],
    )
    def test_any_difference_produces_a_different_key(self, changed):
        assert fingerprint(changed) != fingerprint(
            ResourceRequest(verb=ReadVerb.GET, resource="pods")
        )

    def test_option_order_is_not_a_difference(self):
        first = ResourceRequest(verb=ReadVerb.LOGS, options={"tail": 10, "previous": True})
        second = ResourceRequest(verb=ReadVerb.LOGS, options={"previous": True, "tail": 10})
        assert fingerprint(first) == fingerprint(second)

    def test_the_scope_separates_tenants_sharing_a_cluster_name(self):
        """M6 made `AgentRegistry` tenant-keyed precisely because two customers
        may both call a cluster `prod`. A cache keyed on the name alone would
        undo that in one dictionary."""
        provider = RecordingProvider(cluster_id="prod")
        with tenant_scope("acme"):
            acme = cache_scope(provider, None)
        with tenant_scope("globex"):
            globex = cache_scope(provider, None)
        assert acme != globex

    def test_the_scope_separates_callers_with_different_rbac(self, monkeypatch):
        """With impersonation on the cluster applies the *caller's* RBAC, so
        the same read has different correct answers for different callers —
        one of them a refusal. Sharing them is a privilege escalation with no
        log line."""
        monkeypatch.setattr(settings, "impersonate_users", True)
        provider = RecordingProvider()
        alice = cache_scope(provider, Principal(subject="alice@acme.com"))
        bob = cache_scope(provider, Principal(subject="bob@acme.com"))
        assert alice != bob

    def test_groups_are_part_of_the_identity(self, monkeypatch):
        """Kubernetes RBAC binds to groups as often as to users, and
        impersonation sends both."""
        monkeypatch.setattr(settings, "impersonate_users", True)
        provider = RecordingProvider()
        plain = cache_scope(provider, Principal(subject="alice@acme.com"))
        privileged = cache_scope(provider, Principal(subject="alice@acme.com", groups=("sre",)))
        assert plain != privileged

    def test_group_order_is_not_an_identity(self, monkeypatch):
        monkeypatch.setattr(settings, "impersonate_users", True)
        provider = RecordingProvider()
        assert cache_scope(
            provider, Principal(subject="a", groups=("sre", "oncall"))
        ) == cache_scope(provider, Principal(subject="a", groups=("oncall", "sre")))

    def test_two_providers_for_one_cluster_name_do_not_share(self):
        """An agent-reached `prod` and a kubeconfig context called `prod` are
        not guaranteed to be the same cluster — that is exactly what
        `select_provider` refuses over rather than guessing."""

        class Other(RecordingProvider):
            pass

        assert cache_scope(RecordingProvider(), None) != cache_scope(Other(), None)


class TestWhatIsAndIsNotReused:
    async def test_a_repeated_read_does_not_reach_the_cluster(self):
        inner, provider = build()
        await provider.fetch(PODS)
        await provider.fetch(PODS)
        assert len(inner.reads) == 1
        assert provider.hits == 1 and provider.misses == 1

    async def test_a_different_read_still_reaches_the_cluster(self):
        inner, provider = build()
        await provider.fetch(PODS)
        await provider.fetch(NODES)
        assert len(inner.reads) == 2

    async def test_a_failed_read_is_never_stored(self):
        """A cached `FORBIDDEN` goes on refusing after the RBAC that caused it
        is fixed, and `app/kubernetes/access.py` reads exactly those statuses
        to tell a locked door from a broken cluster. Measured against a real
        kind cluster: all 13 of a warm run's misses were failures."""
        inner, provider = build(RecordingProvider(success=False))
        await provider.fetch(PODS)
        await provider.fetch(PODS)
        assert len(inner.reads) == 2
        assert provider.hits == 0

    async def test_an_entry_expires(self):
        inner, provider = build(ttl=0.05)
        await provider.fetch(PODS)
        await asyncio.sleep(0.08)
        await provider.fetch(PODS)
        assert len(inner.reads) == 2

    async def test_a_partly_warm_wave_still_costs_one_round_trip(self):
        """The misses go to the provider together. Splitting them would trade a
        subprocess saving for a WAN one on the agent path."""
        calls = []
        inner = RecordingProvider()
        original = inner.fetch_many

        async def counted(requests):
            calls.append(len(requests))
            return await original(requests)

        inner.fetch_many = counted
        provider = CachingProvider(inner, CollectionCache(60.0, 1 << 20), "scope")
        await provider.fetch(PODS)
        calls.clear()
        await provider.fetch_many([PODS, NODES])
        assert calls == [1]

    async def test_a_cached_payload_cannot_be_corrupted_by_its_reader(self):
        """Handing the same dict to two investigations means one collector's
        mutation silently rewrites the other's evidence — and redaction runs
        above this layer, so the mutation would already be inside the store."""
        _, provider = build()
        first = await provider.fetch(PODS)
        first.data["items"].append({"name": "injected"})
        second = await provider.fetch(PODS)
        assert second.data == {"items": [{"name": "web-0"}]}

    def test_an_oversized_read_is_not_stored_at_all(self):
        cache = CollectionCache(60.0, max_bytes=10)
        cache.put("k", ProviderResult(success=True, data={"x": "y" * 100}))
        assert cache.get("k") is None

    def test_the_cache_evicts_rather_than_growing(self):
        """Peak heap for one investigation is ~5x its stored result, which is
        what `JOB_MAX_CONCURRENT` is sized against. An unbounded cache would
        move that number without anyone changing it."""
        payload = {"items": ["x" * 200]}
        size = len(json.dumps(payload))
        cache = CollectionCache(60.0, max_bytes=size * 3)
        for index in range(10):
            cache.put(f"key-{index}", ProviderResult(success=True, data=payload))
        assert cache.stats()["bytes"] <= size * 3
        assert cache.stats()["evictions"] > 0
        assert cache.get("key-0") is None
        assert cache.get("key-9") is not None


class TestTheAgeIsNeverHidden:
    """The load-bearing property. Everything else here is an optimisation."""

    async def test_a_collector_reading_from_cache_reports_the_read_s_age(self):
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        provider = CachingProvider(inner, cache, "scope")
        await provider.fetch(PODS)

        with freshness_window() as window:
            await provider.fetch(PODS)
        assert window.oldest is not None
        assert window.hits == 1 and window.misses == 0

    async def test_a_live_read_does_not_backdate_anything(self):
        _, provider = build()
        with freshness_window() as window:
            await provider.fetch(PODS)
        assert window.oldest is None
        assert window.misses == 1

    async def test_the_window_survives_the_task_boundary(self):
        """`asyncio` copies the context when it creates a task, and
        `LocalKubectlProvider.fetch_many` gathers its reads into child tasks. A
        `ContextVar` holding a string would land in a copy and the scheduler
        would see nothing — the same defect shape as `require_principal` having
        to stay `async`, and as the correlation id holder."""
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        provider = CachingProvider(inner, cache, "scope")
        await provider.fetch(PODS)

        with freshness_window() as window:
            await asyncio.gather(*(provider.fetch(PODS) for _ in range(3)))
        assert window.hits == 3
        assert window.oldest is not None

    def test_the_scheduler_dates_evidence_by_the_read_not_the_run(self):
        """Asserted on the record a citation would resolve to, because that is
        the value a report shows six weeks later."""
        scheduler = CollectionScheduler(CollectorRegistry(), EvidenceRedactor())
        read_at = datetime.now(UTC) - timedelta(seconds=45)
        evidence = Evidence.create(
            kind="k8s.pods",
            status=EvidenceStatus.OK,
            target=ResourceRef.cluster("prod"),
            data={"items": []},
        )
        window = type(
            "W", (), {"oldest": read_at, "hits": 1, "misses": 0}
        )()  # a window that saw one 45s-old read
        stamped = scheduler._sanitize(evidence, 5, window)
        assert stamped.collected_at == read_at
        assert stamped.to_index_entry()["collected_at"] == read_at.isoformat()

    def test_a_collector_that_mixed_cached_and_live_reads_understates_freshness(self):
        """Understating is safe; overstating is the defect this exists to
        prevent. A record that says 45 seconds when one of its four reads was
        45 seconds old is honest about the weakest fact it rests on."""
        scheduler = CollectionScheduler(CollectorRegistry(), EvidenceRedactor())
        old = datetime.now(UTC) - timedelta(seconds=45)
        window = type("W", (), {"oldest": old})()
        evidence = Evidence.create(
            kind="k8s.pods", status=EvidenceStatus.OK, target=ResourceRef.cluster("prod")
        )
        assert scheduler._sanitize(evidence, 5, window).collected_at == old

    def test_a_window_is_never_used_to_move_a_record_forward(self):
        scheduler = CollectionScheduler(CollectorRegistry(), EvidenceRedactor())
        future = datetime.now(UTC) + timedelta(seconds=45)
        window = type("W", (), {"oldest": future})()
        evidence = Evidence.create(
            kind="k8s.pods", status=EvidenceStatus.OK, target=ResourceRef.cluster("prod")
        )
        assert scheduler._sanitize(evidence, 5, window).collected_at < future

    def test_redaction_still_happens_to_a_backdated_record(self):
        scheduler = CollectionScheduler(CollectorRegistry(), EvidenceRedactor())
        old = datetime.now(UTC) - timedelta(seconds=45)
        window = type("W", (), {"oldest": old})()
        evidence = Evidence.create(
            kind="k8s.pods",
            status=EvidenceStatus.OK,
            target=ResourceRef.cluster("prod"),
            data={"password": "hunter2"},
        )
        stamped = scheduler._sanitize(evidence, 5, window)
        assert stamped.data == {"password": "[REDACTED]"}
        assert stamped.collected_at == old


class TestNothingElseAboutTheInvestigationChanges:
    async def test_a_reused_read_is_still_in_the_reproduction_trail(self):
        """A warm investigation whose command list shrank would look like one
        that examined less of the cluster."""
        _, provider = build()
        await provider.fetch(PODS)
        await provider.fetch(NODES)
        cold = list(provider.executed_commands)

        _, warm = build()
        warm._cache = provider._cache
        await warm.fetch(PODS)
        await warm.fetch(NODES)
        assert sorted(warm.executed_commands) == sorted(cold)

    async def test_a_truncation_survives_the_cache(self):
        """Losing it would make a warm investigation silently claim it saw a
        whole cluster it only saw the first 2,000 objects of."""

        class Truncating(RecordingProvider):
            async def fetch_many(self, requests):
                results = []
                for request in requests:
                    command = f"kubectl {request.describe()}"
                    self.truncations.append(
                        {"command": command, "returned": 40000, "retained": 2000}
                    )
                    results.append(
                        ProviderResult(
                            success=True,
                            data={"items": []},
                            equivalent_command=command,
                            truncated=True,
                            total_items=40000,
                        )
                    )
                return results

        inner = Truncating()
        cache = CollectionCache(60.0, 1 << 20)
        cold = CachingProvider(inner, cache, "scope")
        await cold.fetch(PODS)

        warm = CachingProvider(RecordingProvider(), cache, "scope")
        result = await warm.fetch(PODS)
        assert result.truncated and result.total_items == 40000
        assert warm.truncations == [
            {"command": "kubectl get pods all", "returned": 40000, "retained": 2000}
        ]

    def test_cluster_access_still_names_the_transport(self):
        """`cluster_access_total` exists to make the M8a regression visible —
        an agent fleet quietly answered from kubeconfigs. Reading
        `type(self.provider)` through a wrapper would report every
        investigation as `kubeconfig` and hide it again."""

        class RemoteAgentProvider(RecordingProvider):
            pass

        remote = RemoteAgentProvider()
        wrapped = CachingProvider(remote, CollectionCache(60.0, 1 << 20), "scope")
        assert type(underlying(wrapped)).__name__ == "RemoteAgentProvider"
        assert type(underlying(remote)).__name__ == "RemoteAgentProvider"


class TestRefusingToReuse:
    async def test_refresh_reads_the_cluster_again(self):
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        await CachingProvider(inner, cache, "scope").fetch(PODS)
        await CachingProvider(inner, cache, "scope", read=False).fetch(PODS)
        assert len(inner.reads) == 2

    async def test_refresh_still_leaves_what_it_read_behind(self):
        """An alert storm must not leave the cache permanently cold for the
        operator who opens the console straight afterwards."""
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        await CachingProvider(inner, cache, "scope", read=False).fetch(PODS)
        await CachingProvider(inner, cache, "scope").fetch(PODS)
        assert len(inner.reads) == 1

    def test_a_zero_ttl_is_the_old_code_path_not_a_disabled_new_one(self, monkeypatch):
        monkeypatch.setattr(settings, "collection_cache_ttl_seconds", 0)
        reset_collection_cache()
        provider = RecordingProvider()
        assert with_cache(provider) is provider

    def test_the_default_deployment_caches(self, monkeypatch):
        reset_collection_cache()
        assert get_collection_cache().enabled
        assert isinstance(with_cache(RecordingProvider()), CachingProvider)


class TestTheInvestigationSaysWhatItReused:
    async def test_the_payload_carries_the_age_of_the_oldest_reused_fact(self):
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        await CachingProvider(inner, cache, "scope").fetch(PODS)
        warm = CachingProvider(inner, cache, "scope")
        await warm.fetch(PODS)

        report = warm.report()
        assert report["enabled"] is True
        assert report["hits"] == 1 and report["misses"] == 0
        assert report["oldest_evidence_seconds"] is not None

    async def test_an_investigation_reports_its_reuse(self, monkeypatch):
        """Through the real service, so the key is the one a real request
        builds rather than one this test invented."""
        from app.providers.local_kubectl import LocalKubectlProvider
        from app.services.investigation_service import InvestigationService
        from tests.test_investigation_service import FakeKubectl

        reset_collection_cache()
        monkeypatch.setattr(settings, "collection_cache_ttl_seconds", 60.0)

        def run(**kwargs):
            service = InvestigationService(context="cache-cluster", **kwargs)
            service.provider = with_cache(
                LocalKubectlProvider(context="cache-cluster", executor=FakeKubectl()),
                refresh=kwargs.get("refresh", False),
            )
            return service

        cold = await run().run()
        assert cold["collection_cache"]["hits"] == 0
        assert cold["collection_cache"]["misses"] > 0

        warm = await run().run()
        assert warm["collection_cache"]["hits"] > 0
        assert warm["pods"] == cold["pods"]

        forced = await run(refresh=True).run()
        assert forced["collection_cache"]["hits"] == 0
        assert forced["collection_cache"]["enabled"] is False


class TestTheContextIsScoped:
    async def test_a_scope_does_not_leak_out_of_its_block(self):
        cache = CollectionCache(60.0, 1 << 20)
        inner = RecordingProvider()
        provider = CachingProvider(inner, cache, "scope")
        with freshness_window():
            await provider.fetch(PODS)
        with freshness_window() as second:
            pass
        assert second.hits == 0 and second.misses == 0

    async def test_no_window_is_not_an_error(self):
        """Nothing outside the scheduler opens one, and a provider used
        directly must still work."""
        _, provider = build()
        assert (await provider.fetch(PODS)).success
