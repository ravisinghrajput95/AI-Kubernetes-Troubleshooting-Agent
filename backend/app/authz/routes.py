"""What each endpoint requires. The table *is* the enforcement point.

`require_principal` can be a router-level dependency because every route needs
the same thing. Permissions differ per route, so the naive translation is
`Depends(require(Permission.X))` on each handler — which is forgettable, and
forgettable is precisely the property this milestone is trying not to have.

So there is exactly one dependency, applied once per router, and it looks the
matched route up here. The load-bearing part is what happens on a miss:

    **a route with no entry in this table is denied.**

Not allowed. A new endpoint added without a thought about authorisation fails
closed at runtime *and* fails `tests/test_authz.py`, which derives the route
list from the OpenAPI schema — the same derivation `test_auth.py` uses, and for
the same reason: the hand-maintained list it replaced had already drifted by
four endpoints.

Leaving a route open therefore requires naming it `PUBLIC` or `AUTHENTICATED`
below, where the decision is visible in review.
"""

from app.authz.models import Permission

# Reachable with no credential at all. `/health` is hit by container probes
# before one exists; the rest are FastAPI's own and carry no data.
PUBLIC: frozenset[str] = frozenset(
    {
        "/health",
        # Kubelet probes carry no credential and never will. They are also the
        # one caller that must still get an answer when authentication itself
        # is misconfigured — a probe that 401s restarts a pod whose only fault
        # is a typo'd issuer, turning a fixable configuration error into a
        # crashloop across the fleet.
        "/health/live",
        "/health/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
        "/metrics",
        # Signed, and authorised as the source's configured identity
        # rather than by a role. See app/api/events.py.
        "/events/{source_name}",
    }
)

# Authenticated, but requiring no permission. There is one, and it has to
# exist: a caller with no role must still be able to discover that they have no
# role, or the deployment has a locked-out user with no way to see why.
AUTHENTICATED = "authenticated"

# (METHOD, path template) -> permission, or AUTHENTICATED.
#
# The path is FastAPI's `path_format`, i.e. `/investigations/{investigation_id}`
# rather than a concrete id.
ROUTE_PERMISSIONS: dict[tuple[str, str], Permission | str] = {
    # --- session ---------------------------------------------------------
    ("GET", "/me"): AUTHENTICATED,
    # --- MCP -------------------------------------------------------------
    # The only other `AUTHENTICATED` entry, and it means something different:
    # not "nothing to check" but "checked deeper". One endpoint serves many
    # capabilities, so the permission belongs to the tool rather than the
    # route — `app/mcp/tools.py` carries the table and `tests/test_mcp.py`
    # asserts every tool has an entry.
    ("POST", "/mcp"): AUTHENTICATED,
    # --- fleet -----------------------------------------------------------
    # Cluster names and contexts are fleet topology, so reading them is a
    # permission rather than a given.
    ("GET", "/clusters"): Permission.CLUSTER_READ,
    ("GET", "/agents"): Permission.CLUSTER_READ,
    # The CA bundle is a public certificate — agents must trust it, and
    # `agentctl ca` prints it. Read, not enrol.
    ("GET", "/agents/ca"): Permission.CLUSTER_READ,
    # Mints a credential that enrols a cluster into the fleet.
    ("POST", "/agents/enrolment"): Permission.CLUSTER_ENROL,
    # --- investigations --------------------------------------------------
    # Running one reads a production cluster under the caller's impersonated
    # identity and spends an LLM call. It is the platform's only outbound
    # action, which is what makes it the viewer/operator boundary.
    ("POST", "/investigate"): Permission.INVESTIGATION_RUN,
    ("POST", "/investigations"): Permission.INVESTIGATION_RUN,
    ("POST", "/investigations/{investigation_id}/cancel"): Permission.INVESTIGATION_RUN,
    # Re-renders from stored JSON without touching a cluster, but it writes to
    # the report store.
    ("POST", "/investigations/{investigation_id}/regenerate"): Permission.INVESTIGATION_RUN,
    ("GET", "/investigations"): Permission.INVESTIGATION_READ,
    ("GET", "/investigation-jobs"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/status"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/events"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/report"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/pdf"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/json"): Permission.INVESTIGATION_READ,
    ("GET", "/investigations/{investigation_id}/markdown"): Permission.INVESTIGATION_READ,
    # --- people ----------------------------------------------------------
    ("GET", "/members"): Permission.MEMBER_READ,
    ("PUT", "/members/{subject}"): Permission.MEMBER_MANAGE,
    ("DELETE", "/members/{subject}"): Permission.MEMBER_MANAGE,
    ("POST", "/members/{subject}/suspend"): Permission.MEMBER_MANAGE,
    ("DELETE", "/members/{subject}/suspend"): Permission.MEMBER_MANAGE,
}


# Permissions whose routes cost a customer's cluster and a model call.
#
# Rate limiting keys off this rather than off a second list of paths, because a
# second list is a second thing to forget — the same reasoning that put the
# permission check in one router-level dependency. An endpoint that runs an
# investigation already has to declare `investigation.run` to work at all, and
# declaring it is what makes it rate limited.
COSTED_PERMISSIONS: frozenset[Permission] = frozenset({Permission.INVESTIGATION_RUN})


def is_costed(permission: Permission | str | None) -> bool:
    return permission in COSTED_PERMISSIONS


def required_permission(method: str, path: str) -> Permission | str | None:
    """What this route needs, or `None` when it is not in the table.

    `None` means denied. It is spelled as a distinct return rather than folded
    into a default so the caller cannot mistake "no entry" for "no permission
    required" — those are opposite answers and they must not share a value.
    """
    return ROUTE_PERMISSIONS.get((method.upper(), path))
