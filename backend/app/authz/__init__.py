"""Who, inside a tenant, may do what.

M6 made a tenant a data boundary. This makes it an organisation: users, roles,
and a permission check that a new endpoint cannot be added without.

The three pieces, and the property each one carries:

- `models.py` — four roles and a closed permission set. `owner` exists for one
  invariant: you cannot grant a role you do not hold.
- `routes.py` — the route → permission table. **A route with no entry is
  denied**, which is what makes a forgotten endpoint fail closed instead of
  open.
- `dependencies.py` — one router-level dependency that reads that table. There
  are no per-route permission checks to forget, because there are none.

Authorisation deliberately has **no `system_scope()` equivalent**. The tenancy
escape exists because the queue consumer must read a row before it can know the
tenant that row names; authorisation has no such ordering problem, because the
decision is made at the HTTP boundary and background work carries the principal
it was submitted with. The only hole is `AUTH_MODE=disabled` resolving to
`owner`, which lives in one function in `resolver.py` and is pinned by a test.
"""

from app.authz.dependencies import grant_for, has_permission, require_permission
from app.authz.models import (
    DENIED,
    ROLE_PERMISSIONS,
    SUSPENDED,
    AuthorizationModelError,
    Grant,
    Membership,
    Permission,
    Role,
    highest,
)
from app.authz.resolver import (
    RoleResolver,
    default_role,
    get_resolver,
    parse_role_mappings,
    reset_resolver,
    reset_sightings,
    role_from_groups,
)
from app.authz.routes import AUTHENTICATED, PUBLIC, ROUTE_PERMISSIONS, required_permission
from app.authz.store import (
    FileMemberStore,
    MemberStore,
    get_member_store,
    set_member_store,
)

__all__ = [
    "AUTHENTICATED",
    "DENIED",
    "PUBLIC",
    "ROLE_PERMISSIONS",
    "ROUTE_PERMISSIONS",
    "SUSPENDED",
    "AuthorizationModelError",
    "FileMemberStore",
    "Grant",
    "MemberStore",
    "Membership",
    "Permission",
    "Role",
    "RoleResolver",
    "default_role",
    "get_member_store",
    "get_resolver",
    "grant_for",
    "has_permission",
    "highest",
    "parse_role_mappings",
    "require_permission",
    "required_permission",
    "reset_resolver",
    "reset_sightings",
    "role_from_groups",
    "set_member_store",
]
