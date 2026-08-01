"""Who a request belongs to, and how that is enforced.

ADR-006 asks for tenant isolation "enforced in the data layer, not in
handlers". That distinction is the whole milestone. Passing a `tenant_id`
argument into every store method is isolation by discipline: it works until
someone adds a query and forgets, and the failure mode is one customer reading
another's incidents. There is no test for the query nobody wrote yet.

So the tenant is not an argument. It is ambient — a context variable set once
when the caller is authenticated — and the *database* enforces it:

- `Database.cursor()` emits `SET LOCAL app.current_tenant` on every
  transaction, so the value is scoped to that transaction and cannot leak
  between pooled connections.
- Every tenanted table has `tenant_id` defaulting to that setting, so inserts
  are stamped without any caller mentioning it.
- Row-level security policies compare `tenant_id` to it, under FORCE, so the
  policy applies to the table owner too — which is what the application
  connects as.

The consequence worth stating: a `SELECT * FROM investigations` with no WHERE
clause returns only the caller's rows. Not "returns rows the handler then
filters". Returns only theirs.

Two modes, one decision at startup:

    TENANCY_MODE=single (default)  one implicit tenant, nothing to isolate
    TENANCY_MODE=shared            tenant per caller, RLS enforced, needs Postgres

`shared` without `DATABASE_URL` is refused rather than half-honoured. There is
no in-memory equivalent of row-level security, and a multi-tenant deployment
whose isolation is a Python `if` is worse than one that told you it could not.
"""

from app.tenancy.context import (
    DEFAULT_TENANT,
    SYSTEM_TENANT,
    current_tenant,
    is_system_scope,
    system_scope,
    tenant_scope,
)
from app.tenancy.models import Tenant, TenantError, valid_tenant_id

__all__ = [
    "DEFAULT_TENANT",
    "SYSTEM_TENANT",
    "Tenant",
    "TenantError",
    "current_tenant",
    "is_system_scope",
    "system_scope",
    "tenant_scope",
    "valid_tenant_id",
]
