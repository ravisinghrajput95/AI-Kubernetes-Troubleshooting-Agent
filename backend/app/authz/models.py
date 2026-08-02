"""Roles, permissions, and the membership that binds a person to one.

M6 made a tenant a data boundary and stopped there: inside one, every caller who
could authenticate could start investigations, mint cluster enrolment tokens and
revoke certificates. `Principal` already carried `groups`, and they were used
only for Kubernetes impersonation — never for platform authorisation.

Four roles, and each boundary is a capability rather than a tier label:

- `viewer`   — may read what they own, and see the fleet.
- `operator` — **may cause reads against a customer cluster** and spend model
               budget. That is the platform's only outbound action, so "may
               look at what the team found" and "may go poke prod" are
               deliberately different jobs.
- `admin`    — **may change the fleet** (enrol a cluster, revoke an agent) and
               **may change who can do what**.
- `owner`    — **may grant `owner`**, and is the floor: a tenant's last owner
               cannot be demoted or removed.

`owner` exists for one invariant: *you cannot grant a role you do not hold*.
Without it, `admin` granting `admin` makes the two roles identical, there is no
ceiling on escalation, and an admin could demote every other admin and take the
tenant. With it the ceiling is a single rule, checked in one place.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Permission(StrEnum):
    """What a caller may do. A closed set, deliberately.

    Named after the operation rather than the endpoint, so that moving a route
    or adding a second one for the same capability does not invent a permission.
    """

    # Investigations
    INVESTIGATION_READ = "investigation.read"
    # Every investigation in the tenant, not only your own. New power that
    # nobody has today — ownership already isolates per user — so it is a
    # separate permission rather than something `admin` acquires invisibly.
    INVESTIGATION_READ_ALL = "investigation.read_all"
    INVESTIGATION_RUN = "investigation.run"

    # Fleet
    CLUSTER_READ = "cluster.read"
    CLUSTER_ENROL = "cluster.enrol"
    CLUSTER_REVOKE = "cluster.revoke"

    # People
    MEMBER_READ = "member.read"
    MEMBER_MANAGE = "member.manage"
    MEMBER_MANAGE_OWNER = "member.manage_owner"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        """Where this role sits in the ordering. Only meaningful for comparison."""
        return _RANKS[self]

    def permits(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self]

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self]

    @classmethod
    def parse(cls, value: str) -> "Role":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise AuthorizationModelError(
                f"{value!r} is not a role. Use one of: {', '.join(role.value for role in cls)}."
            ) from exc


class AuthorizationModelError(Exception):
    """A role or permission that cannot be used."""


_RANKS: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 1,
    Role.ADMIN: 2,
    Role.OWNER: 3,
}

_VIEWER = frozenset({Permission.INVESTIGATION_READ, Permission.CLUSTER_READ})

_OPERATOR = _VIEWER | {Permission.INVESTIGATION_RUN}

_ADMIN = _OPERATOR | {
    Permission.CLUSTER_ENROL,
    Permission.CLUSTER_REVOKE,
    Permission.MEMBER_READ,
    Permission.MEMBER_MANAGE,
}

# `investigation.read_all` is owner-only, and deliberately **not** an admin
# permission, because of how it composes with the deployment default.
#
# `RBAC_DEFAULT_ROLE=admin` is what keeps existing single-tenant installs
# working unchanged (see `config.validate_authz`), which means every unbound
# caller is an admin. Put tenant-wide report reading in `admin` and upgrading
# to this milestone silently removes the per-user report isolation those
# deployments already had — a confidentiality regression introduced by a
# milestone about tightening authorisation. Reading other people's incident
# reports is *data* access; enrolling clusters and managing members is
# *control* access, and separating them is the least-privilege reading anyway.
#
# A customer who wants incident review that does not depend on whoever ran the
# investigation grants an owner, deliberately.
_OWNER = _ADMIN | {Permission.MEMBER_MANAGE_OWNER, Permission.INVESTIGATION_READ_ALL}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}


def highest(*roles: "Role | None") -> "Role | None":
    """The strongest of several grants, or `None` when there are none.

    Grants combine rather than override. A stored binding that could *lower* an
    IdP-granted role would be a second directory silently disagreeing with the
    first, which is the thing group mapping exists to avoid. Taking access away
    is `suspended`, which is not a role at all.
    """
    present = [role for role in roles if role is not None]
    return max(present, key=lambda role: role.rank) if present else None


@dataclass(frozen=True, slots=True)
class Membership:
    """One person's standing in one tenant.

    **`role` is `None` for a row that only records having seen someone.** Every
    authenticated request upserts a row so an admin can find real people in
    `GET /members` rather than only the ones already granted something — and if
    that row carried a role, it would carry authority. Written as `viewer` it
    would *demote* a caller whose role comes from the single-tenant default on
    their very next request; written as the resolved role it would freeze a
    group grant into storage and outlive the group. A row that says only "this
    person exists" is the one form that cannot do either.

    `role` is therefore exactly what an operator explicitly granted, and it is
    not necessarily the caller's effective role — group mappings grant too, and
    the resolver takes the higher of the two. `suspended` is the one thing that
    overrides everything, because an admin has to be able to cut access now
    rather than after an IdP round trip.
    """

    subject: str
    role: Role | None = None
    email: str = ""
    suspended: bool = False
    granted_by: str = ""
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    last_seen_at: datetime | None = None

    @property
    def assigned(self) -> bool:
        """Whether anyone actually granted this person a role."""
        return self.role is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "role": str(self.role) if self.role else "",
            "assigned": self.assigned,
            "email": self.email,
            "suspended": self.suspended,
            "granted_by": self.granted_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


@dataclass(frozen=True, slots=True)
class Grant:
    """The outcome of resolving a caller's authority, and where it came from.

    `source` is carried so `/me` can tell a denied user *why* — "no role" and
    "suspended" need different answers from the person reading the screen, and
    a support conversation that cannot distinguish them is a bad one.
    """

    role: Role | None
    source: str

    @property
    def permissions(self) -> frozenset[Permission]:
        return self.role.permissions if self.role is not None else frozenset()

    def permits(self, permission: Permission) -> bool:
        return self.role is not None and self.role.permits(permission)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": str(self.role) if self.role else "",
            "role_source": self.source,
            "permissions": sorted(str(permission) for permission in self.permissions),
        }


DENIED = Grant(role=None, source="none")
SUSPENDED = Grant(role=None, source="suspended")
