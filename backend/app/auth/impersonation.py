"""Whose identity a cluster read runs as — decided once, for both providers.

F13's guarantee is that the platform cannot see more than the calling user can,
and it is delivered by Kubernetes impersonation: the API server applies the
*caller's* RBAC rather than the service account's. Two code paths deliver it —
`kubectl --as` locally, `Impersonate-User` headers through an agent — and until
this existed they made the decision separately and did not agree.

The local path declined to impersonate when the setting was off or the caller
was anonymous. The agent path sent `principal.subject` unconditionally, so an
unauthenticated deployment asked the cluster to read as a user literally named
`anonymous`. That was inert only because the agent discarded the field; the
moment it started honouring it, every read on such a deployment would have been
refused by a cluster that has no such user.

So the decision lives here and both callers ask. `tests/test_auth.py` asserts
they agree, which is the only thing that keeps two implementations of one policy
from drifting apart again — the same reason `Settings.validate_auth()` calls the
very builder the request dependency uses.
"""

from app.auth.models import Principal
from app.core.config import settings


def identity_for(principal: Principal | None) -> tuple[str, tuple[str, ...]] | None:
    """The user and groups to read as, or `None` to read as the platform.

    `None` is not a fallback that lost information — it is the deliberate
    answer in three cases, and each one is a deployment saying it has no caller
    identity to apply:

    - impersonation turned off (`IMPERSONATE_USERS=false`);
    - no principal at all, which is background work with no request behind it;
    - an anonymous principal, which is what `AUTH_MODE=disabled` produces.

    A cluster agent configured with `--impersonate` **refuses** a read that
    names nobody rather than falling back to its own broad-read ServiceAccount.
    That is deliberate and it is the same refusal `EVENT_SOURCES` makes by
    requiring a subject: automation must not be the one door with no user
    behind it. The consequence worth knowing is concrete — an impersonating
    agent and `AUTH_MODE=disabled` are not a working combination, and the agent
    says so on every read rather than quietly reading as itself.
    """
    if not settings.impersonate_users or principal is None:
        return None
    if principal.anonymous or not principal.subject:
        return None
    return principal.subject, tuple(principal.groups)
