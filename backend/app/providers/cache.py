"""Serving a second investigation without re-reading the whole cluster.

Every investigation used to collect the whole cluster from scratch: ~20 reads,
each one a `kubectl` subprocess or a round trip to an agent that may be on
another continent. Two investigations of the same cluster a minute apart did
identical work, and `docs/PERFORMANCE_ENVELOPE.md` measured `collect` at 65% of
an investigation. This is that 65%.

**A cache that lies is worse than no cache**, and in a product whose premise is
that nothing is asserted without evidence, "lies" has a precise meaning: an
`Evidence` record whose `collected_at` says *now* when the fact left the
cluster forty seconds ago. Every conclusion cites an evidence id, so a
timestamp that drifts corrupts the citation spine rather than merely being
untidy. Five decisions follow from that, and each is pinned by a test:

- **It sits at the `ClusterProvider` seam**, wrapping whichever provider
  `select_provider` chose. Not in collectors: a collector that knew about
  caching could tell which provider it had, which is exactly the property
  removing `raw_executor()` was for. Not in the evidence store either — that
  would cache *conclusions*, and analysis must be reproducible from the facts.
- **The key carries the tenant and the impersonated identity**, not just the
  cluster name. `AgentRegistry` is keyed `(tenant, cluster)` because two
  customers may both call a cluster `prod`; a cache keyed on the name alone
  would undo M6 in one dictionary. The identity matters for the same reason
  impersonation exists: two callers with different Kubernetes RBAC get
  *different answers* to the same read, one of them a `FORBIDDEN`, and serving
  one user's rows to the other would be a privilege escalation with no log
  line.
- **Only successful reads are stored.** A cached `FORBIDDEN` would keep
  refusing after an operator fixed the RBAC, and a cached timeout would turn a
  one-second blip into a minute of blindness. Failures are also what
  `app/kubernetes/access.py` reasons over to tell a locked door from a broken
  cluster.
- **Evidence built from a cached read is stamped with the read's real age.**
  `FreshnessWindow` below carries it from the provider to the scheduler, which
  is the one place that already post-processes every evidence record.
- **A cached payload is re-parsed from text on every serve.** Handing the same
  dict to two investigations means one collector's mutation silently corrupts
  the other's evidence, and redaction runs *after* this layer. Re-parsing costs
  a `json.loads` — measurably less than a subprocess and a cluster round trip,
  which is the entire point — and buys certainty rather than a convention
  nobody can enforce.

**It applies to the agent path too, and identically.** Caching on the platform
side of a stream that exists to read a customer's live cluster deserves an
argument, and it is this: the freshness contract is a property of the evidence,
not of the transport. A cache that behaved differently per provider would make
an agent investigation and a kubeconfig investigation of the same cluster
non-comparable — the thing `tests/test_metrics_parity.py` exists to prevent —
and the agent path is where a round trip is most expensive, because it crosses
a WAN and the agent's client-go is rate limited at 50 QPS.

**In this process only, never Redis.** "Redis is the latency layer, Postgres is
the truth" means every message has a committed row behind it; a cached cluster
read has no row and never will, so putting it in Redis would be the Redis-only
fact that rule forbids. It would also put megabytes of a customer's unredacted
cluster interior in a shared store — redaction happens at the collection
boundary, *above* this — which `docs/DATA_PROTECTION.md` would then have to
answer for. In-process, bounded, and gone on restart is the honest scope.
"""

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.observability import metrics
from app.providers.base import ClusterProvider, ProviderResult, ResourceRequest
from app.tenancy.context import current_tenant

# Separators that cannot occur in a tenant id, a cluster name, a Kubernetes
# username or a group, so two different scopes cannot serialise to one key.
_SCOPE_SEP = "\x1e"
_IDENTITY_SEP = "\x1f"


@dataclass
class FreshnessWindow:
    """How old the oldest fact a single collector saw actually is.

    **Mutable, and shared by reference on purpose.** `asyncio` copies the
    context when it creates a task, so a `ContextVar` holding a *string* that a
    collector's own reads rebind would land in a copy and never be seen by the
    scheduler that opened the window — `LocalKubectlProvider.fetch_many` gathers
    its reads into child tasks, so that is the normal path, not an edge case.
    Mutating one shared holder crosses the boundary. Same family as
    `correlation_scope()` installing a holder rather than a string, and as
    `require_principal` having to stay `async`.

    `oldest` is the *worst* case across the reads one collector performed. A
    collector that mixed three cached reads with one live one is stamped with
    the cached age: understating freshness is safe, overstating it is the defect
    this whole module is written around.
    """

    oldest: datetime | None = None
    hits: int = 0
    misses: int = 0

    def record_cached(self, fetched_at: datetime) -> None:
        self.hits += 1
        if self.oldest is None or fetched_at < self.oldest:
            self.oldest = fetched_at

    def record_live(self) -> None:
        self.misses += 1


_window: ContextVar[FreshnessWindow | None] = ContextVar("collection_freshness", default=None)


@contextmanager
def freshness_window() -> Iterator[FreshnessWindow]:
    """Observe how old the evidence collected inside this block really is."""
    window = FreshnessWindow()
    token = _window.set(window)
    try:
        yield window
    finally:
        _window.reset(token)


def fingerprint(request: ResourceRequest) -> str:
    """A key for one read, derived from *every* field of the request.

    Derived rather than enumerated, deliberately. An enumerated fingerprint
    that forgot a field would collide two different reads and serve one's
    answer for the other — a `-n kube-system` pod list returned for a
    `-n payments` investigation, filed as evidence and cited. Reading the
    dataclass means a field added later is covered before anyone remembers to
    cover it; `tests/test_collection_cache.py` asserts the coverage anyway, so
    a future non-dataclass request shape cannot pass silently.
    """
    parts: list[str] = []
    for spec in fields(request):
        value = getattr(request, spec.name)
        if isinstance(value, dict):
            value = sorted((str(key), str(item)) for key, item in value.items())
        parts.append(f"{spec.name}={value!r}")
    return "|".join(parts)


def cache_scope(provider: ClusterProvider, principal: Principal | None) -> str:
    """Everything that can change the answer to the same read.

    The tenant, because two customers may both call a cluster `prod`. The
    provider class, because a cluster reached through its agent and the same
    name resolved against the platform's kubeconfig are not guaranteed to be
    the same cluster — that is precisely what `select_provider` refuses over.
    The impersonated identity, because with impersonation on the cluster
    applies the *caller's* RBAC, so the same read genuinely has different
    correct answers for different callers.

    With impersonation off every read runs as the service account, so one
    shared identity is not a leak — it is the truth about what was read.
    """
    identity = "service-account"
    if settings.impersonate_users and principal is not None and not principal.anonymous:
        identity = principal.subject + _IDENTITY_SEP + ",".join(sorted(principal.groups))
    return _SCOPE_SEP.join(
        (current_tenant(), type(provider).__name__, provider.cluster_id, identity)
    )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored read. Only ever built from a successful `ProviderResult`."""

    payload: str | None
    text: str
    equivalent_command: str
    truncated: bool
    total_items: int
    truncation: dict[str, Any] | None
    fetched_at: datetime
    stored_at: float
    size: int

    def to_result(self) -> ProviderResult:
        return ProviderResult(
            success=True,
            data=json.loads(self.payload) if self.payload is not None else None,
            text=self.text,
            equivalent_command=self.equivalent_command,
            truncated=self.truncated,
            total_items=self.total_items,
        )


class CollectionCache:
    """Process-local, TTL-bounded, byte-bounded store of cluster reads.

    Bounded by bytes rather than entries because entries are not comparable: a
    node list is kilobytes and a 2,000-pod list with logs is megabytes. The
    measured peak heap of one investigation is ~5x its stored result, so an
    unbounded cache would silently change how many investigations fit in a
    worker — the number `JOB_MAX_CONCURRENT` is sized against.
    """

    def __init__(self, ttl_seconds: float, max_bytes: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._bytes = 0
        # Reads happen on the event loop, but `to_thread` and the queue
        # consumer make "only the loop touches this" a claim rather than a
        # guarantee. An uncontended lock costs nothing and removes the class.
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0 and self.max_bytes > 0

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if time.monotonic() - entry.stored_at > self.ttl_seconds:
                self._drop(key)
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry

    def put(
        self,
        key: str,
        result: ProviderResult,
        truncation: dict[str, Any] | None = None,
    ) -> None:
        """Store one **successful** read.

        A failure is never stored. A cached `FORBIDDEN` would go on refusing
        after the RBAC that caused it was fixed, and `app/kubernetes/access.py`
        reads exactly those statuses to tell a locked door from a broken
        cluster — caching one would make the diagnosis persist past its cause.
        """
        if not self.enabled or not result.success:
            return

        try:
            payload = None if result.data is None else json.dumps(result.data)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            logger.debug("Read not cached, payload is not JSON: {error}", error=exc)
            return

        entry = CacheEntry(
            payload=payload,
            text=result.text,
            equivalent_command=result.equivalent_command,
            truncated=result.truncated,
            total_items=result.total_items,
            truncation=dict(truncation) if truncation else None,
            fetched_at=datetime.now(UTC),
            stored_at=time.monotonic(),
            size=len(payload or "") + len(result.text),
        )
        if entry.size > self.max_bytes:
            return

        with self._lock:
            self._drop(key)
            self._entries[key] = entry
            self._bytes += entry.size
            while self._bytes > self.max_bytes and self._entries:
                oldest, _ = next(iter(self._entries.items()))
                if oldest == key:
                    break
                self._drop(oldest)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "ttl_seconds": self.ttl_seconds,
                "max_bytes": self.max_bytes,
            }

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size


_cache: CollectionCache | None = None


def get_collection_cache() -> CollectionCache:
    """The process's one cache. Built on first use, from settings."""
    global _cache
    if _cache is None:
        _cache = CollectionCache(
            ttl_seconds=settings.collection_cache_ttl_seconds,
            max_bytes=settings.collection_cache_max_bytes,
        )
    return _cache


def reset_collection_cache() -> None:
    """Drop the process cache. For tests and for a settings change in one."""
    global _cache
    _cache = None


class CachingProvider(ClusterProvider):
    """A `ClusterProvider` that answers a repeated read from memory.

    Transparent by construction: it implements the same protocol, and a
    collector cannot tell it apart from the provider it wraps. The one thing it
    is *not* transparent about is time, and that is the point — see
    `FreshnessWindow`.
    """

    def __init__(
        self,
        inner: ClusterProvider,
        cache: CollectionCache,
        scope: str,
        read: bool = True,
    ) -> None:
        self.inner = inner
        self._cache = cache
        self._scope = scope
        self.read = read
        self.hits = 0
        self.misses = 0
        self.oldest_served: datetime | None = None
        self._cached_commands: list[str] = []
        self._cached_truncations: list[dict[str, Any]] = []

    @property
    def cluster_id(self) -> str:
        return self.inner.cluster_id

    @property
    def executed_commands(self) -> list[str]:
        """Every read this investigation rests on, cached ones included.

        A warm investigation whose command list had shrunk would look like one
        that examined less of the cluster. The commands are reproduction
        instructions — "here is how to see this yourself" — and a read served
        from cache did happen, just earlier. Cached reads are listed first
        because they are the older ones.
        """
        return [*self._cached_commands, *self.inner.executed_commands]

    @property
    def truncations(self) -> list[dict[str, Any]]:
        """Where the cluster was larger than the read that produced this entry.

        Carried through the cache rather than recomputed. Losing it would make
        a warm investigation silently claim it saw a whole cluster it only saw
        the first 2,000 objects of — a partial view presented as a complete
        one, which is the failure `collection_limits` exists to prevent.
        """
        return [*self._cached_truncations, *self.inner.truncations]

    async def fetch(self, request: ResourceRequest) -> ProviderResult:
        results = await self.fetch_many([request])
        return results[0]

    async def fetch_many(self, requests: Sequence[ResourceRequest]) -> Sequence[ProviderResult]:
        """Serve what is cached, ask the provider for the rest in one call.

        The misses go to `inner.fetch_many` together rather than one at a time,
        so a partially-warm wave still costs the remote agent a single round
        trip. Splitting them would trade a subprocess saving for a WAN one.
        """
        window = _window.get()
        results: list[ProviderResult | None] = [None] * len(requests)
        misses: list[int] = []

        for position, request in enumerate(requests):
            entry = self._cache.get(self._key(request)) if self.read else None
            if entry is None:
                misses.append(position)
                continue
            results[position] = entry.to_result()
            self._record_hit(entry, window)

        if misses:
            fetched = await self.inner.fetch_many([requests[position] for position in misses])
            for position, result in zip(misses, fetched, strict=True):
                results[position] = result
                self.misses += 1
                metrics.collection_cache("miss")
                if window is not None:
                    window.record_live()
                self._cache.put(self._key(requests[position]), result, self._truncation_for(result))

        if any(result is None for result in results):  # pragma: no cover - defensive
            raise RuntimeError("A provider answered fewer reads than it was asked for.")
        return [result for result in results if result is not None]

    def _record_hit(self, entry: CacheEntry, window: FreshnessWindow | None) -> None:
        self.hits += 1
        metrics.collection_cache("hit")
        if window is not None:
            window.record_cached(entry.fetched_at)
        if self.oldest_served is None or entry.fetched_at < self.oldest_served:
            self.oldest_served = entry.fetched_at
        if entry.equivalent_command:
            self._cached_commands.append(entry.equivalent_command)
        if entry.truncation:
            self._cached_truncations.append(entry.truncation)

    def _truncation_for(self, result: ProviderResult) -> dict[str, Any] | None:
        if not result.truncated:
            return None
        return next(
            (
                record
                for record in self.inner.truncations
                if record.get("command") == result.equivalent_command
            ),
            None,
        )

    def _key(self, request: ResourceRequest) -> str:
        return f"{self._scope}{_SCOPE_SEP}{fingerprint(request)}"

    def report(self) -> dict[str, Any]:
        """What this investigation took from the cache, for its payload.

        `enabled` means "reuse was in effect *for this investigation*", not
        "the deployment has a cache" — a `refresh=true` run reports `false`,
        which is what a reader of one investigation wants to know.
        """
        age = None
        if self.oldest_served is not None:
            age = round((datetime.now(UTC) - self.oldest_served).total_seconds(), 1)
        return {
            "enabled": self.read,
            "ttl_seconds": self._cache.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "oldest_evidence_seconds": age,
        }


def underlying(provider: ClusterProvider) -> ClusterProvider:
    """The provider that actually reaches the cluster.

    `cluster_access` reports `agent` or `kubeconfig` by asking what the
    provider *is*, and a wrapper is neither. Without this an agent fleet would
    report every investigation as `kubeconfig` — the exact shape of the M8a
    regression `cluster_access_total` was added to make visible, reintroduced by
    the thing meant to speed it up.
    """
    return provider.inner if isinstance(provider, CachingProvider) else provider


def with_cache(
    provider: ClusterProvider,
    principal: Principal | None = None,
    refresh: bool = False,
) -> ClusterProvider:
    """Wrap a provider so repeated reads are served from memory.

    Returns the provider unchanged when caching is off or when the caller asked
    for fresh data, so a deployment with `COLLECTION_CACHE_TTL_SECONDS=0`
    behaves exactly as it did before this existed.

    **`refresh` bypasses reading, and still writes.** An investigation that
    insists on fresh data gets it; refusing to store what it read as well would
    mean an alert storm left the cache permanently cold for the operator who
    goes to look immediately afterwards — the worst possible time to be slow.
    """
    cache = get_collection_cache()
    if not cache.enabled:
        return provider
    return CachingProvider(provider, cache, cache_scope(provider, principal), read=not refresh)
