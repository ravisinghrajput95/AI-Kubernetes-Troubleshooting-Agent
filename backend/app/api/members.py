"""Who is in this tenant, and what they may do.

There is no invite flow here, and that is a decision rather than an omission.
An invite needs email delivery, single-use tokens with TTLs, an acceptance
endpoint and a second identity to reconcile against whatever the identity
provider eventually asserts — the same machinery class as agent enrolment, for
a problem the IdP already solves. And it could never grant *access*: the
platform cannot authenticate anyone the IdP does not know. All an invite could
do is pre-assign a role, which is exactly what `PUT /members/{subject}` does,
before or after that person has ever signed in.

Membership rows are also created on every authenticated request, so this lists
people who have actually appeared alongside people who have been granted
something. A row created that way carries no role at all — see `Membership`.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.audit.logger import get_audit_log
from app.auth.models import Principal
from app.authz.dependencies import grant_for, require_permission
from app.authz.models import AuthorizationModelError, Permission, Role
from app.authz.service import MemberError, assign_role, remove_role, set_suspended
from app.authz.store import get_member_store

router = APIRouter(tags=["members"], dependencies=[Depends(require_permission)])


class RoleAssignment(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    email: str = Field(default="", max_length=254)


def _parse(role: str) -> Role:
    try:
        return Role.parse(role)
    except AuthorizationModelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _handled(action):
    """Run a membership change, mapping its refusals onto status codes."""
    try:
        return action()
    except MemberError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/members")
def list_members(principal: Principal = Depends(require_permission)) -> dict[str, Any]:
    """Everyone this tenant has seen or granted a role to."""
    return {
        "items": [member.to_dict() for member in get_member_store().list()],
        "roles": [str(role) for role in Role],
    }


@router.put("/members/{subject}")
def assign_member_role(
    subject: str,
    assignment: RoleAssignment,
    principal: Principal = Depends(require_permission),
) -> dict[str, Any]:
    """Grant a role, whether or not this person has ever signed in."""
    role = _parse(assignment.role)
    grant = grant_for(principal)

    # The permission gate on this route is `member.manage`, which admins hold.
    # Granting `owner` needs its own, so that an admin cannot promote anyone
    # (including themselves) into the role that outranks them.
    if role is Role.OWNER and not grant.permits(Permission.MEMBER_MANAGE_OWNER):
        raise HTTPException(
            status_code=403,
            detail="Only an owner may grant the owner role.",
        )

    member = _handled(
        lambda: assign_role(
            subject,
            role,
            actor_role=grant.role,
            actor_subject=principal.subject,
            email=assignment.email,
        )
    )
    get_audit_log().record_action("members.assign", principal, target=subject, detail=str(role))
    return member.to_dict()


@router.delete("/members/{subject}")
def remove_member(
    subject: str,
    principal: Principal = Depends(require_permission),
) -> dict[str, Any]:
    """Drop a binding.

    Not necessarily a removal of access: the person may still hold a role
    through an IdP group, which this table does not own. Suspension is the
    operation that denies regardless.
    """
    grant = grant_for(principal)
    removed = _handled(lambda: remove_role(subject, actor_role=grant.role))
    if not removed:
        raise HTTPException(status_code=404, detail=f"{subject} is not a member of this tenant.")

    get_audit_log().record_action("members.remove", principal, target=subject)
    return {"subject": subject, "removed": True}


@router.post("/members/{subject}/suspend")
def suspend_member(
    subject: str,
    principal: Principal = Depends(require_permission),
) -> dict[str, Any]:
    """Deny this person immediately, whatever their groups say."""
    grant = grant_for(principal)
    member = _handled(lambda: set_suspended(subject, True, actor_role=grant.role))
    get_audit_log().record_action("members.suspend", principal, target=subject)
    return member.to_dict()


@router.delete("/members/{subject}/suspend")
def restore_member(
    subject: str,
    principal: Principal = Depends(require_permission),
) -> dict[str, Any]:
    grant = grant_for(principal)
    member = _handled(lambda: set_suspended(subject, False, actor_role=grant.role))
    get_audit_log().record_action("members.restore", principal, target=subject)
    return member.to_dict()
