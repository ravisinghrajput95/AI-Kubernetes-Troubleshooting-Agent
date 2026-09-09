"""What the collection cache is holding, which nothing observed.

`CollectionCache.stats()` has carried bytes, entries and evictions since F18
and no caller read them. So when a 60-minute soak reported both workers rising
monotonically at +2.5 and +0.6 MB/h, the one question that matters — is this
the cache filling toward its 64 MB bound, or a leak? — had no answer but
inference from resident memory, and `docs/PERFORMANCE_ENVELOPE.md` had to say
an hour cannot separate the two.

**An eviction count still at zero says the bound has never bound**, which is
what distinguishes them, and it is a fact the cache already knew.

Unlabelled, like every gauge in this registry: these are properties of the
worker, and `tests/test_metrics.py` already refuses any series carrying a
cluster, tenant or namespace.
"""

import pytest

from app.observability import metrics


@pytest.fixture(autouse=True)
def _reset():
    metrics._evictions_seen = 0
    yield
    metrics._evictions_seen = 0


def value(name: str) -> float:
    return metrics.REGISTRY.get_sample_value(name) or 0.0


class TestTheCacheReportsWhatItHolds:
    def test_bytes_and_entries_are_published(self):
        metrics.collection_cache_size({"bytes": 4096, "entries": 7, "evictions": 0})

        assert value("k8sagent_collection_cache_bytes") == 4096
        assert value("k8sagent_collection_cache_entries") == 7

    def test_a_cache_that_has_never_evicted_adds_nothing(self):
        """The load-bearing case. Zero is a fact, not an absence.

        A cache still filling and a cache at its bound are the two readings of
        a slow rise in resident memory, and this is what tells them apart — so
        the series has to exist and read zero rather than not exist at all,
        the same reason every label set here is seeded at import.
        """
        before = value("k8sagent_collection_cache_evictions_total")
        metrics.collection_cache_size({"bytes": 100, "entries": 1, "evictions": 0})

        assert value("k8sagent_collection_cache_evictions_total") == before
        assert (
            metrics.REGISTRY.get_sample_value("k8sagent_collection_cache_evictions_total")
            is not None
        ), "the series must exist even at zero, like every seeded label set"

    def test_evictions_accumulate_as_a_counter(self):
        """The cache counts totals; the metric is a counter, so it takes deltas."""
        before = value("k8sagent_collection_cache_evictions_total")
        metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 3})
        metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 10})

        assert value("k8sagent_collection_cache_evictions_total") == before + 10

    def test_a_repeated_sample_does_not_double_count(self):
        """Sampled once per wave, so the same total arrives many times."""
        before = value("k8sagent_collection_cache_evictions_total")
        for _ in range(5):
            metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 4})

        assert value("k8sagent_collection_cache_evictions_total") == before + 4

    def test_a_replaced_cache_does_not_rewind_the_counter(self):
        """`clear()` and a rebuilt singleton both restart the cache's own count.

        A counter that went backwards would be a broken series; the recorder
        re-bases instead, so the next real eviction still counts once.
        """
        metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 9})
        before = value("k8sagent_collection_cache_evictions_total")

        metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 0})
        assert value("k8sagent_collection_cache_evictions_total") == before

        metrics.collection_cache_size({"bytes": 1, "entries": 1, "evictions": 2})
        assert value("k8sagent_collection_cache_evictions_total") == before + 2

    @pytest.mark.parametrize(
        "stats",
        [
            {},
            {"bytes": None, "entries": None, "evictions": None},
            # The cases that make the `_safe` wrapper load-bearing rather than
            # decorative. `or 0` inside the recorder handles None on its own, so
            # a None-only test passes with the wrapper removed — the first
            # version of this file did exactly that and the mutation survived.
            # These reach the wrapper and nothing else.
            {"evictions": "many"},
            {"bytes": object(), "entries": 1, "evictions": 0},
            None,
        ],
    )
    def test_recording_never_raises(self, stats):
        """`_safe()` swallows: instrumentation must not fail a collection.

        This is the module's one rule — a metric that can raise turns an
        observability bug into an outage — and it is why the arithmetic lives
        inside the wrapper rather than beside it.
        """
        metrics.collection_cache_size(stats)


class TestItComesFromTheRealCache:
    async def test_a_wave_publishes_the_cache_it_actually_used(self):
        """Asserted through the provider, not by calling the recorder.

        A recorder that is correct and never called reads identically to a
        working one from inside the process — the mistake Loki's `X-Scope-OrgID`
        already shipped once.
        """
        from app.providers.cache import CachingProvider, CollectionCache

        class Recording:
            def __init__(self):
                self.calls = 0

            async def fetch_many(self, requests):
                self.calls += 1
                return [_ok() for _ in requests]

            async def fetch(self, request):  # pragma: no cover - unused here
                return _ok()

        cache = CollectionCache(ttl_seconds=60, max_bytes=64 * 1024 * 1024)
        provider = CachingProvider(Recording(), cache, scope="t")

        metrics.collection_cache_size({"bytes": 0, "entries": 0, "evictions": 0})
        await provider.fetch_many([_request()])

        assert value("k8sagent_collection_cache_entries") >= 1
        assert value("k8sagent_collection_cache_bytes") > 0


def _request():
    from app.providers.base import ReadVerb, ResourceRequest

    return ResourceRequest(verb=ReadVerb.GET, resource="pods", all_namespaces=True)


def _ok():
    from app.providers.base import ProviderResult

    return ProviderResult(success=True, data={"items": [{"a": "b" * 100}]}, text="")
