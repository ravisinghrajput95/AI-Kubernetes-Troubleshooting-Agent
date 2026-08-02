"""How much work one caller, or one customer, may ask for.

`PRODUCTION_READINESS.md` lists "no rate limiting" as an open P1, and
`docs/PERFORMANCE_ENVELOPE.md` gave it a number: a worker sustains ~10
investigations/s, and nothing stopped one caller consuming all of it. §10 lists
"per-tenant quotas" among the mitigations for *evidence volume overwhelms the
platform*; budgets at source were built, this is the other half.

**What is limited is deliberately narrow.** An investigation is the platform's
only outbound action: it reads a customer's production cluster under the
caller's impersonated identity and spends a model call. Reads of what has
already been collected are cheap and already owner-scoped. So the limit applies
to exactly the operations that cost a cluster and a budget — which, since M6.5,
is precisely the set requiring `investigation.run`.

**A per-worker limit is not a limit.** With three replicas and a counter in
process memory, the effective limit is three times what an operator configured,
and it changes when they scale — a quota that moves when you add capacity is
not a quota. So the counter follows the same seam as every other piece of state
here:

    no REDIS_URL  -> InMemoryRateLimiter, where one process *is* the fleet
    REDIS_URL     -> RedisRateLimiter, shared across every worker

**Fixed window, and the limitation is stated rather than hidden.** `INCR` plus
`EXPIRE` is one round trip and cannot be got subtly wrong; the cost is that a
caller can spend a full window's budget at the end of one window and again at
the start of the next, so the true short-term ceiling is 2x the configured rate.
At the defaults here that is far below what the platform can serve, and a
sliding window would trade a real increase in complexity for protection against
a burst the envelope says is harmless. If the limits are ever tightened toward
actual capacity, this is the assumption to revisit first.
"""

import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loguru import logger

WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a caller may proceed, and what to tell them if not."""

    allowed: bool
    # Which bucket refused. A closed set — `subject` or `tenant` — because it
    # becomes a metric label and a caller-visible string.
    scope: str = ""
    limit: int = 0
    retry_after_seconds: int = 0

    @property
    def detail(self) -> str:
        if self.allowed:
            return ""
        who = "You have" if self.scope == "subject" else "This tenant has"
        return (
            f"{who} reached the limit of {self.limit} investigations per minute. "
            f"An investigation reads a production cluster and spends a model call, "
            f"so the rate is capped. Retry in {self.retry_after_seconds}s."
        )


@runtime_checkable
class RateLimiter(Protocol):
    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        """Count one use of `key`. Returns (allowed, seconds until the window rolls)."""
        ...


class InMemoryRateLimiter:
    """The single-process default, where one process is the whole fleet.

    Not a stand-in for the Redis one: with no `REDIS_URL` there is exactly one
    worker, so a process-local counter *is* the shared counter and the limit it
    enforces is the real one.
    """

    def __init__(self, window_seconds: int = WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._counts: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        window = int(now // self._window)
        remaining = int(self._window - (now % self._window)) or 1

        with self._lock:
            # Old windows are dropped rather than swept: the key space is
            # bounded by callers-per-window, and a sweep would be a second
            # thing to get right for no benefit.
            if len(self._counts) > 10_000:
                self._counts = {
                    entry: count for entry, count in self._counts.items() if entry[1] >= window
                }
            used = self._counts.get((key, window), 0) + 1
            self._counts[(key, window)] = used

        return used <= limit, remaining


class RedisRateLimiter:
    """One counter for the whole fleet.

    `INCR` returns the post-increment value, so the first caller into a window
    sees exactly 1 and is the one that sets the expiry. No read-then-write, and
    therefore no race that lets two workers both believe they are first.
    """

    def __init__(self, bus, window_seconds: int = WINDOW_SECONDS) -> None:
        self._bus = bus
        self._window = window_seconds

    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        window = int(now // self._window)
        remaining = int(self._window - (now % self._window)) or 1

        try:
            used = self._bus.increment_in_window(
                f"{self._bus.prefix}:ratelimit:{key}:{window}", self._window
            )
        except Exception as exc:
            # Fail **open**, loudly. A rate limiter is availability protection,
            # not an authorisation control: refusing every investigation because
            # Redis blinked would turn a degraded dependency into an outage, and
            # the thing it protects against is a caller being noisy rather than
            # a caller being hostile. Authorisation, which *is* a security
            # control, fails closed — see `app/authz`.
            logger.warning("Rate limit could not be read, allowing: {error}", error=exc)
            return True, remaining

        return used <= limit, remaining


def evaluate(
    limiter: RateLimiter,
    subject: str,
    tenant: str,
    subject_limit: int,
    tenant_limit: int,
) -> Decision:
    """Check both buckets. A limit of 0 means that bucket is not enforced.

    Subject first, so a single runaway caller is told it is their own rate that
    stopped them rather than their colleagues'.
    """
    if subject_limit > 0 and subject:
        allowed, retry = limiter.hit(f"subject:{tenant}:{subject}", subject_limit)
        if not allowed:
            return Decision(False, "subject", subject_limit, retry)

    if tenant_limit > 0:
        allowed, retry = limiter.hit(f"tenant:{tenant}", tenant_limit)
        if not allowed:
            return Decision(False, "tenant", tenant_limit, retry)

    return Decision(True)


_limiter: RateLimiter | None = None


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = InMemoryRateLimiter()
    return _limiter
