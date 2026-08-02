"""The one place a permission is checked.

Applied as a router-level dependency, exactly like `require_principal` and for
exactly the same reason: a check that has to be remembered per route will
eventually be forgotten. The difference is that permissions vary per route,
which is why this resolves the matched route against `ROUTE_PERMISSIONS`
instead of taking a permission as an argument.

Two properties are load-bearing:

- **A route missing from the table is denied**, not allowed. Adding an endpoint
  without deciding what it requires fails closed.
- **Permission is checked before ownership.** A viewer who guesses an id gets
  403 and learns nothing about whether the id exists; the ownership checks
  inside the handlers keep answering 404, which is what stops *those* from
  confirming an id. Reversing the order would turn every 404 into a disclosure
  for callers who cannot read investigations at all.

403 rather than 404 for a permission denial, deliberately. The existing
404-on-denial is about **ownership**, where 403 would confirm an id exists. A
permission denial discloses only the caller's own role, which `/me` already
tells them — and a 404 on `POST /agents/enrolment` would be a lie about the
shape of the API rather than a disclosure control.
"""

from fastapi import Depends, HTTPException, Request, status
from loguru import logger

from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.authz.models import Grant, Permission
from app.authz.resolver import get_resolver
from app.authz.routes import AUTHENTICATED, is_costed, required_permission
from app.observability import metrics

_DENIAL_HINT = {
    Permission.CLUSTER_ENROL: "Enrolling a cluster requires the admin role.",
    Permission.CLUSTER_REVOKE: "Revoking an agent certificate requires the admin role.",
    Permission.INVESTIGATION_RUN: "Running an investigation requires the operator role.",
    Permission.MEMBER_MANAGE: "Managing members requires the admin role.",
    Permission.MEMBER_MANAGE_OWNER: "Only an owner may grant or remove the owner role.",
}


def grant_for(principal: Principal) -> Grant:
    """The caller's effective role. Cheap enough to call per request."""
    return get_resolver().resolve(principal)


def require_permission(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Principal:
    """Authorise the caller for the route they matched.

    Returns the principal so a handler can depend on this instead of
    `require_principal` where it wants both in one place.
    """
    route = request.scope.get("route")
    path = getattr(route, "path_format", "") or request.url.path
    needed = required_permission(request.method, path)

    if needed is None:
        # Not in the table. Closed, and loud: this is a programming error that
        # a test should already have caught, so it should be findable in a log
        # rather than merely returning 403.
        logger.error(
            "{method} {path} has no entry in ROUTE_PERMISSIONS and was denied. "
            "Add one to app/authz/routes.py.",
            method=request.method,
            path=path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint has no authorisation policy and is therefore refused.",
        )

    try:
        grant = grant_for(principal)
    except Exception as exc:
        # The membership store could not be read. Denying is the only safe
        # answer: `RBAC_DEFAULT_ROLE=admin` and "the database is down" must not
        # compose into "everybody is an admin".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorisation could not be determined; refusing the request.",
        ) from exc

    request.state.grant = grant
    get_resolver().note_seen(principal)

    if needed is AUTHENTICATED:
        return principal

    if not grant.permits(needed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_denial(grant, needed))

    _enforce_rate_limit(needed, principal)
    return principal


def _enforce_rate_limit(needed: Permission, principal: Principal) -> None:
    """Cap the operations that cost a cluster and a model call.

    Keyed off the permission the route already declares rather than off a list
    of paths, so a new endpoint that runs an investigation is limited by virtue
    of needing `investigation.run` — there is no second table to keep in step.

    **After the permission check, deliberately.** A caller who may not run
    investigations at all should be told that, not handed a 429 that implies
    they would be allowed if only they waited.
    """
    if not is_costed(needed):
        return

    from app.core.config import settings
    from app.ratelimit import evaluate, get_rate_limiter

    decision = evaluate(
        get_rate_limiter(),
        subject=principal.subject,
        tenant=principal.tenant,
        subject_limit=settings.rate_limit_per_minute,
        tenant_limit=settings.rate_limit_tenant_per_minute,
    )
    if decision.allowed:
        return

    metrics.rate_limited(decision.scope)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=decision.detail,
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def _denial(grant: Grant, needed: Permission) -> str:
    """Say what was needed and what was held.

    A denial that does not name the missing permission turns every role
    misconfiguration into a support ticket, and the caller already knows their
    own role from `/me` — there is nothing here to withhold.
    """
    if grant.source == "suspended":
        return "This account is suspended in this tenant."
    held = f"the {grant.role} role" if grant.role else "no role in this tenant"
    hint = _DENIAL_HINT.get(needed, "")
    return f"{hint} Requires '{needed}'; you hold {held}.".strip()


def has_permission(principal: Principal, permission: Permission) -> bool:
    """For handlers that widen behaviour rather than refuse it.

    `investigation.read_all` is the case: an admin listing investigations sees
    the tenant's, everyone else sees their own. That is a different shape from
    a gate and belongs in the handler, not here.
    """
    try:
        return grant_for(principal).permits(permission)
    except Exception:  # pragma: no cover - the gate above already denied
        return False
