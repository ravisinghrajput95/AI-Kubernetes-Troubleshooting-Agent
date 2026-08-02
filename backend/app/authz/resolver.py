"""Turning an authenticated caller into a role.

Three inputs, in this order of authority:

1. **Suspension.** An explicit binding may carry `suspended`, which denies
   outright whatever the IdP says. It exists because an admin has to be able to
   cut access *now*, not after the customer's directory team gets to it.
2. **Grants combine.** The effective role is the higher of the group-derived
   role and the stored binding's role. A binding that could *lower* an IdP
   grant would be a second directory silently disagreeing with the first, which
   is the thing group mapping exists to avoid. Take access away by removing the
   group, or by suspending.
3. **The fallback**, when neither produced anything. This is the only part that
   varies by deployment, and it is the whole of §3 of the design:

       AUTH_MODE=disabled     -> owner
       TENANCY_MODE=single    -> RBAC_DEFAULT_ROLE, default `admin`
       TENANCY_MODE=shared    -> nothing; denied

**The check itself always runs.** Nothing is switched off by mode. A control
that only executes in the deployment nobody runs by default is exactly the
"present, correct and inert" failure this project already hit once, when
row-level security was enabled, forced, correct, and doing nothing because the
application connected as a superuser.

`AUTH_MODE=disabled` resolving to `owner` is the single deliberate hole, and it
is the authorisation counterpart of tenancy's `system_scope`: one function, one
grep, one test. It is not a bypass — the permission check still runs, the anonymous
caller simply holds every permission — and it changes nothing about a mode that
already leaves every endpoint open, and that already refuses to mint enrolment
tokens for exactly that reason.
"""

import threading
from time import monotonic

from loguru import logger

from app.auth.models import Principal
from app.authz.models import DENIED, SUSPENDED, AuthorizationModelError, Grant, Role, highest
from app.core.config import Settings, settings

# How often a person's `last_seen_at` is refreshed. Long enough that the write
# is negligible, short enough that "who has actually used this" stays useful.
SIGHTING_INTERVAL_SECONDS = 300.0
SIGHTING_CACHE_LIMIT = 10_000

_SIGHTINGS: dict[tuple[str, str], float] = {}
_SIGHTING_LOCK = threading.Lock()


def reset_sightings() -> None:
    """Test seam: forget who has been seen recently."""
    with _SIGHTING_LOCK:
        _SIGHTINGS.clear()


def parse_role_mappings(raw: str) -> dict[str, Role]:
    """`"acme-admins=owner,sre=operator"` -> `{group: role}`.

    A malformed entry raises at startup rather than being skipped. A silently
    dropped mapping is a customer whose admins are viewers and whose only
    symptom is a support ticket.
    """
    mappings: dict[str, Role] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        group, separator, role = entry.partition("=")
        if not separator or not group.strip() or not role.strip():
            raise AuthorizationModelError(
                f"OIDC_ROLE_MAPPINGS entry {entry!r} is not 'group=role'. "
                f"Example: 'acme-admins=owner,sre=operator,staff=viewer'."
            )
        mappings[group.strip()] = Role.parse(role)
    return mappings


def role_from_groups(groups: tuple[str, ...], mappings: dict[str, Role]) -> Role | None:
    """The strongest role any of the caller's groups maps to."""
    return highest(*(mappings.get(group) for group in groups))


def default_role(config: Settings | None = None) -> Role | None:
    """What a caller with no binding and no matching group gets.

    Single-tenant defaults to `admin` deliberately. Before this milestone every
    authenticated caller in a single-tenant deployment could do everything, so
    `admin` is that behaviour preserved exactly rather than a permission being
    handed out — an operator who wants real roles sets `RBAC_DEFAULT_ROLE=viewer`
    and starts assigning. Same discipline as the single-process job store:
    supported, not a dev-only fallback.
    """
    config = config or settings
    raw = (config.rbac_default_role or "").strip().lower()
    if raw in {"", "none"}:
        return None
    return Role.parse(raw)


class RoleResolver:
    """Answers "what may this caller do", from configuration and storage."""

    def __init__(
        self,
        mappings: dict[str, Role] | None = None,
        fallback: Role | None = None,
        config: Settings | None = None,
    ) -> None:
        config = config or settings
        self._config = config
        self._mappings = (
            mappings if mappings is not None else parse_role_mappings(config.oidc_role_mappings)
        )
        self._fallback = fallback if fallback is not None else default_role(config)
        self._open_deployment = config.auth_mode.strip().lower() == "disabled"

    @property
    def mappings(self) -> dict[str, Role]:
        return dict(self._mappings)

    def resolve(self, principal: Principal) -> Grant:
        """The caller's effective role, and where it came from."""
        if self._open_deployment:
            # The one deliberate hole. See the module docstring.
            return Grant(role=Role.OWNER, source="open-deployment")

        membership = self._membership(principal)
        if membership is not None and membership.suspended:
            return SUSPENDED

        from_groups = role_from_groups(principal.groups, self._mappings)
        from_binding = membership.role if membership is not None else None

        granted = highest(from_groups, from_binding)
        if granted is not None:
            if from_binding is not None and granted is from_binding and from_groups is None:
                return Grant(role=granted, source="assigned")
            if from_groups is not None and granted is from_groups and from_binding is None:
                return Grant(role=granted, source="group")
            return Grant(role=granted, source="assigned+group")

        if self._fallback is not None:
            return Grant(role=self._fallback, source="default")

        return DENIED

    def note_seen(self, principal: Principal) -> None:
        """Record that this person exists, at most once per interval.

        Every request is authenticated by a bearer token, so there is no login
        event to hang this on — without a throttle it would be one store write
        per HTTP request, which for the file store means rewriting the whole
        file. The cache is in-process and lossy on purpose: a missed sighting
        costs a slightly stale `last_seen_at` and nothing else, and it grants no
        authority either way (`touch()` never writes a role).
        """
        if principal.anonymous or self._open_deployment:
            return

        from app.tenancy import current_tenant

        key = (current_tenant(), principal.subject)
        now = monotonic()
        with _SIGHTING_LOCK:
            if now - _SIGHTINGS.get(key, 0.0) < SIGHTING_INTERVAL_SECONDS:
                return
            # Unbounded growth would be a slow leak on a large fleet; the whole
            # cache is disposable, so dropping it is cheaper than evicting.
            if len(_SIGHTINGS) > SIGHTING_CACHE_LIMIT:
                _SIGHTINGS.clear()
            _SIGHTINGS[key] = now

        from app.authz.store import get_member_store

        try:
            get_member_store().touch(principal.subject, principal.email)
        except Exception as exc:  # pragma: no cover - never fail a request for this
            logger.warning(
                "Could not record a sighting of {subject}: {error}",
                subject=principal.subject,
                error=str(exc),
            )

    def _membership(self, principal: Principal):
        """The caller's stored binding, if the store can be reached.

        A store failure denies rather than falling back to the default role.
        `RBAC_DEFAULT_ROLE=admin` plus "the database is down" must not compose
        into "everyone is an admin".
        """
        from app.authz.store import get_member_store

        try:
            return get_member_store().get(principal.subject)
        except Exception as exc:  # pragma: no cover - exercised by a fault test
            logger.error(
                "Could not read the membership for {subject}: {error}. Denying.",
                subject=principal.subject,
                error=str(exc),
            )
            raise


_resolver: RoleResolver | None = None


def get_resolver() -> RoleResolver:
    global _resolver
    if _resolver is None:
        _resolver = RoleResolver()
    return _resolver


def reset_resolver() -> None:
    """Test seam, and the hook startup uses after validating configuration."""
    global _resolver
    _resolver = None
