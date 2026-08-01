"""The ambient tenant, and the one narrow way to step outside it.

A `ContextVar` rather than an argument, because the argument is the thing that
gets forgotten. Set once when the caller is authenticated; read by
`Database.cursor()` and by nothing else that matters.

`ContextVar` is the right primitive here specifically because asyncio tasks
inherit a *copy* of the context at creation: a background investigation started
from a request keeps that request's tenant, and cannot be mutated by the
request that started it finishing. A module global would be shared by every
concurrent request on the worker, which for this value is a cross-tenant leak
with extra steps.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.tenancy.models import require_tenant_id

# The tenant everything belongs to when tenancy is not in use. A single-tenant
# deployment has exactly this one, and rows written before M6 read as it.
DEFAULT_TENANT = "default"

# Not a tenant. A marker meaning "this code path legitimately spans tenants",
# which the row-level security policies recognise and which only
# `system_scope()` can set. Deliberately not a valid tenant id — the shape rule
# rejects `*`, so no caller can ever be authenticated into it.
SYSTEM_TENANT = "*"

_current: ContextVar[str] = ContextVar("current_tenant", default=DEFAULT_TENANT)


def current_tenant() -> str:
    """The tenant this code is running on behalf of."""
    return _current.get()


def is_system_scope() -> bool:
    return _current.get() == SYSTEM_TENANT


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """Run a block on behalf of one tenant.

    Validated on entry: an unchecked value here would be interpolated into a
    `SET LOCAL`, and a tenant id is exactly the kind of value that arrives from
    a token claim.
    """
    token = _current.set(require_tenant_id(tenant_id))
    try:
        yield tenant_id
    finally:
        _current.reset(token)


@contextmanager
def system_scope() -> Iterator[str]:
    """Run a block that legitimately spans every tenant.

    There are exactly two of these, and both are infrastructure rather than
    features: the queue consumer claiming whatever work is next, and the reaper
    failing jobs whose worker died. Neither can know a tenant before it has
    read the row that names one.

    It is a deliberate hole in the isolation, which is why it is one function
    with a name that shows up in a grep, rather than an optional argument on
    every store method. `tests/test_tenancy.py` asserts who uses it.
    """
    token = _current.set(SYSTEM_TENANT)
    try:
        yield SYSTEM_TENANT
    finally:
        _current.reset(token)
