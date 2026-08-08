"""Granting and removing roles, and the two rules that make it safe.

Both rules live here rather than in the handler, because `rbacctl` performs the
same operations and must be held to the same invariant in one of the two cases
and deliberately exempt in the other:

- **You cannot grant a role you do not hold.** This is the whole reason `owner`
  exists as a role distinct from `admin`. Without it, an admin granting `admin`
  makes the two identical, there is no ceiling on escalation, and an admin can
  demote every other admin and take the tenant. Checked against the *actor's*
  effective role, so it applies to a group-derived grant exactly as it does to
  an assigned one.

- **A tenant's last owner cannot be demoted, removed or suspended.** Otherwise
  the ownership of a tenant is a state you can leave and cannot re-enter over
  HTTP, and recovery means shell access on the platform host.

`rbacctl` passes `actor=None`, which skips the first rule and keeps the second.
That asymmetry is the bootstrap: a brand-new tenant in `shared` mode has nobody
who can grant anything, and the escape is a CLI protected by shell access — the
same decision M4b made for token minting, for the same reason. It is
deliberately *not* "the first caller to authenticate owns the tenant", which in
a shared deployment hands a tenant to whoever walks through the door first.
"""

from app.authz.models import Membership, Role
from app.authz.store import MemberStore, get_member_store


class MemberError(Exception):
    """A membership change that must not be made."""

    def __init__(self, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _store(store: MemberStore | None) -> MemberStore:
    return store if store is not None else get_member_store()


def _refuse_escalation(actor_role: Role | None, target_role: Role, verb: str = "grant") -> None:
    """Refuse an action on a role the actor does not hold.

    The `verb` exists because this one guard serves three different actions and
    the refusal used to name only the first: an admin suspending an owner was
    told *"you cannot grant 'owner'"*, which makes a correct security decision
    read like a bug and sends the reader looking for a grant they never
    attempted. `_refuse_last_owner` below already took a verb for exactly this
    reason; this is the same fix applied one function up.

    The rule itself is unchanged, and so is the status code — only the sentence
    describing it.
    """
    if actor_role is None:
        raise MemberError("You hold no role in this tenant.", status_code=403)
    if target_role.rank > actor_role.rank:
        raise MemberError(
            f"You cannot {verb} '{target_role}' while holding '{actor_role}'. "
            f"A role may only be acted on by someone who holds it.",
            status_code=403,
        )


def _refuse_last_owner(subject: str, store: MemberStore, verb: str) -> None:
    """Guard the floor.

    Reads the existing binding first: this only bites when the target *is*
    currently an un-suspended owner, so demoting one of several owners, or
    anybody else, is unaffected.
    """
    existing = store.get(subject)
    if existing is None or existing.role is not Role.OWNER or existing.suspended:
        return
    if store.count_owners() <= 1:
        raise MemberError(
            f"{subject} is the last owner of this tenant and cannot be {verb}. "
            f"Grant another owner first."
        )


def assign_role(
    subject: str,
    role: Role,
    actor_role: Role | None = None,
    actor_subject: str = "",
    email: str = "",
    store: MemberStore | None = None,
    enforce_escalation: bool = True,
) -> Membership:
    """Grant `role` to `subject`, or re-grant a different one.

    Works whether or not `subject` has ever signed in. That is the deliberate
    answer to "invite or OIDC-only": pre-assignment is the entire useful content
    of an invite, without the email, the single-use token, the acceptance
    endpoint, or a second identity to reconcile against what the IdP actually
    asserts later. The platform cannot authenticate anyone the IdP does not
    know, so an invite could never grant access on its own.
    """
    subject = subject.strip()
    if not subject:
        raise MemberError("A member needs a subject.", status_code=422)

    store = _store(store)
    if enforce_escalation:
        _refuse_escalation(actor_role, role)

    if role is not Role.OWNER:
        _refuse_last_owner(subject, store, "demoted")

    return store.upsert(subject, role, email=email, granted_by=actor_subject)


def remove_role(
    subject: str,
    actor_role: Role | None = None,
    store: MemberStore | None = None,
    enforce_escalation: bool = True,
) -> bool:
    """Drop a binding entirely.

    Removing does not necessarily remove access: the caller may still hold a
    role through an IdP group, which is correct — this table does not own that
    grant. Suspension is the operation that denies regardless.
    """
    store = _store(store)
    existing = store.get(subject)

    if enforce_escalation and existing is not None and existing.role is not None:
        # Removing someone more powerful than you is escalation by subtraction.
        _refuse_escalation(actor_role, existing.role, verb="remove")

    _refuse_last_owner(subject, store, "removed")
    return store.remove(subject)


def set_suspended(
    subject: str,
    suspended: bool,
    actor_role: Role | None = None,
    store: MemberStore | None = None,
    enforce_escalation: bool = True,
) -> Membership:
    store = _store(store)
    existing = store.get(subject)
    if existing is None:
        raise MemberError(f"{subject} is not a member of this tenant.", status_code=404)

    if enforce_escalation and existing.role is not None:
        # Restoring is not suspending, and telling someone they "cannot suspend"
        # while they are lifting a suspension is the same defect one step over.
        _refuse_escalation(actor_role, existing.role, verb="suspend" if suspended else "restore")

    if suspended:
        _refuse_last_owner(subject, store, "suspended")

    updated = store.set_suspended(subject, suspended)
    if updated is None:  # pragma: no cover - the read above already proved it exists
        raise MemberError(f"{subject} is not a member of this tenant.", status_code=404)
    return updated
