"""One id that follows a request through submit, collect, analyse and report.

The question this exists to answer is "what happened to investigation X", asked
of a log aggregator holding four workers' output interleaved. Before this, the
answer was to grep for the id and hope the interesting lines happened to
mention it — and the interesting lines are exactly the ones that do not, because
a collector logging a kubectl timeout knows nothing about the job it serves.

**The correlation id is the investigation id wherever one exists.** That is
deliberate and it is what makes the id span workers: the job id is the
investigation id already, so a job submitted on worker A and claimed by worker
B carries the same id on both without anything being passed. Requests that are
not investigations get a generated `req-…` so every line has an id and the
format never shifts.

`ContextVar` for the same reason `app/tenancy` uses one: asyncio copies the
context at task creation, so a background investigation started from a request
keeps that request's id after the request has returned. A module global would
be shared by every concurrent request on the worker and would interleave two
investigations' ids into each other's logs.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass
class _Holder:
    """A mutable box, so an id chosen deeper in the stack is visible higher up.

    The `ContextVar` cannot hold the string directly. Starlette's HTTP
    middleware runs the downstream application in a *child task*, which gets a
    copy of the context — so `bind()` called in a route handler would set the
    id in the copy and the middleware, reading back to fill in the response
    header, would still see the id the request arrived with.

    The copy duplicates the *reference* to this object, not the object, so
    mutating `value` is visible in both. That is exactly the propagation
    wanted at the submit boundary and nowhere else: `correlation_scope()`
    installs a **new** holder, so a background investigation rebinding its own
    id cannot reach back and rename the request that started it.

    Same family of problem as `require_principal` having to stay `async` —
    FastAPI's context copying is load-bearing in both, and in both the failure
    is silent.
    """

    value: str


# Not "unknown" or "-": a line with no correlation id is almost always
# process-level (startup, shutdown, a reaper tick), and saying so reads better
# in an aggregator than a placeholder that looks like a lost id.
NO_CORRELATION = "system"

_current: ContextVar[_Holder | None] = ContextVar("correlation_holder", default=None)

# Long enough not to collide across a fleet's worth of concurrent requests,
# short enough to stay readable in a log line.
_REQUEST_ID_LENGTH = 12

# A header value is attacker-supplied and lands in every log line this request
# writes, so it is bounded and sanitised rather than trusted. An unbounded value
# would let a caller pad the aggregator; a newline would let them forge a line.
MAX_INBOUND_LENGTH = 64
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")


def correlation_id() -> str:
    """The id this code is running under."""
    holder = _current.get()
    return holder.value if holder is not None else NO_CORRELATION


def new_request_id() -> str:
    return f"req-{uuid4().hex[:_REQUEST_ID_LENGTH]}"


def sanitise(value: str | None) -> str:
    """Make an inbound header safe to put in a log line, or reject it.

    Returns "" when there is nothing usable, so the caller generates one
    instead. Silently truncating to something valid would let two callers using
    long ids collide on the same prefix.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_INBOUND_LENGTH:
        return ""
    if not all(character in _SAFE for character in candidate):
        return ""
    return candidate


@contextmanager
def correlation_scope(value: str) -> Iterator[str]:
    """Run a block under one correlation id, isolated from its caller.

    Installs a **new** holder, so a `bind()` inside the block cannot rename the
    id the caller is using. Used at the HTTP boundary, and again by the runner
    when it executes a job — the second is what carries the id onto a worker
    that never saw the request.
    """
    token = _current.set(_Holder(value or NO_CORRELATION))
    try:
        yield value
    finally:
        _current.reset(token)


def bind(value: str) -> None:
    """Adopt an id for the current scope, including for whoever opened it.

    For the submit path: the investigation id does not exist until the job has
    been created, and from that moment the request, the response header and the
    background task should all use it rather than the `req-…` the request
    arrived with.

    Mutates the holder rather than setting the `ContextVar`, which is what lets
    the change reach the HTTP middleware across Starlette's child task. Must
    still run before `asyncio.create_task` for the *task* to see it — a task
    started earlier captured the holder at its previous value only if a new
    scope was opened in between, which `correlation_scope` is careful to do.
    """
    holder = _current.get()
    if holder is None:
        _current.set(_Holder(value or NO_CORRELATION))
        return
    holder.value = value or NO_CORRELATION
