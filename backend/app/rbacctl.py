"""Operator commands for tenant roles.

A CLI rather than an HTTP endpoint, for the same reason `agentctl` is one: this
is the bootstrap. A brand-new tenant in `TENANCY_MODE=shared` has nobody who can
grant anything, so *some* path has to exist that does not itself require a role
— and an unauthenticated role-granting endpoint is a far worse hole than the
problem it solves. Shell access on the platform host is the credential here.

It is deliberately not the other obvious answer, "the first caller to
authenticate into an empty tenant becomes its owner". In a shared deployment
that hands a tenant to whoever walks through the door first, and it is
unrecoverable if the wrong person does.

The escalation rule (*you cannot grant a role you do not hold*) is skipped here
and only here, because there is no actor to compare against. **The last-owner
rule is not skipped** — a tenant you can leave and cannot re-enter over HTTP is
the state this command exists to prevent, not to create.

    python -m app.rbacctl grant --subject alice@acme.com --role owner
    python -m app.rbacctl list
    python -m app.rbacctl suspend --subject bob@acme.com
    python -m app.rbacctl revoke --subject bob@acme.com

`--tenant` selects the tenant in a shared deployment; it defaults to the single
tenant, which is what a single-tenant install has.
"""

import argparse
import sys

from app.authz.models import AuthorizationModelError, Role
from app.authz.service import MemberError, assign_role, remove_role, set_suspended
from app.authz.store import get_member_store
from app.tenancy import DEFAULT_TENANT, tenant_scope


def _store():
    """The same store the API would use, chosen the same way.

    With `DATABASE_URL` set this must be the Postgres one, or the CLI would
    write a file nobody reads. `get_member_store()` falls back to the file
    store, so the database case is wired explicitly here.
    """
    from app.core.config import settings

    if settings.database_url:
        from app.persistence.members import PostgresMemberStore
        from app.persistence.postgres import Database

        database = Database(settings.database_url, min_size=1, max_size=2)
        database.migrate()
        return PostgresMemberStore(database), database

    return get_member_store(), None


def _run(arguments: argparse.Namespace, action) -> int:
    store, database = _store()
    try:
        with tenant_scope(arguments.tenant):
            return action(store)
    except (MemberError, AuthorizationModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


def grant(arguments: argparse.Namespace) -> int:
    def action(store):
        role = Role.parse(arguments.role)
        member = assign_role(
            arguments.subject,
            role,
            actor_subject="rbacctl",
            email=arguments.email,
            store=store,
            # No actor to compare against. This is the bootstrap, and the whole
            # point of it being a shell command rather than an endpoint.
            enforce_escalation=False,
        )
        print(f"{member.subject} is now {member.role} in tenant {arguments.tenant}")
        return 0

    return _run(arguments, action)


def revoke(arguments: argparse.Namespace) -> int:
    def action(store):
        removed = remove_role(arguments.subject, store=store, enforce_escalation=False)
        if not removed:
            print(f"{arguments.subject} had no binding in tenant {arguments.tenant}")
            return 1
        print(f"removed {arguments.subject} from tenant {arguments.tenant}")
        print(
            "note: this removes the assigned role only. If they hold one through "
            "an identity provider group, they keep it — use `suspend` to deny."
        )
        return 0

    return _run(arguments, action)


def suspend(arguments: argparse.Namespace) -> int:
    def action(store):
        member = set_suspended(
            arguments.subject, not arguments.restore, store=store, enforce_escalation=False
        )
        state = "restored" if arguments.restore else "suspended"
        print(f"{member.subject} is {state} in tenant {arguments.tenant}")
        return 0

    return _run(arguments, action)


def show(arguments: argparse.Namespace) -> int:
    def action(store):
        members = store.list()
        if not members:
            print(f"tenant {arguments.tenant} has no members yet")
            return 0

        print(f"{'SUBJECT':<40} {'ROLE':<10} {'STATE':<10} LAST SEEN")
        for member in members:
            role = str(member.role) if member.role else "-"
            state = "suspended" if member.suspended else "active"
            seen = (
                member.last_seen_at.strftime("%Y-%m-%d %H:%M") if member.last_seen_at else "never"
            )
            print(f"{member.subject:<40} {role:<10} {state:<10} {seen}")
        return 0

    return _run(arguments, action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.rbacctl",
        description="Manage tenant roles from the platform host.",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        help="Tenant to operate on (default: the single tenant).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    granting = commands.add_parser("grant", help="Grant a role, signed in or not.")
    granting.add_argument("--subject", required=True, help="The identity the IdP asserts.")
    granting.add_argument(
        "--role", required=True, choices=[str(role) for role in Role], help="Role to grant."
    )
    granting.add_argument("--email", default="", help="Optional, for display.")
    granting.set_defaults(handler=grant)

    revocation = commands.add_parser("revoke", help="Remove an assigned role.")
    revocation.add_argument("--subject", required=True)
    revocation.set_defaults(handler=revoke)

    suspension = commands.add_parser("suspend", help="Deny a member regardless of their groups.")
    suspension.add_argument("--subject", required=True)
    suspension.add_argument(
        "--restore", action="store_true", help="Lift a suspension instead of applying one."
    )
    suspension.set_defaults(handler=suspend)

    listing = commands.add_parser("list", help="Show this tenant's members.")
    listing.set_defaults(handler=show)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
