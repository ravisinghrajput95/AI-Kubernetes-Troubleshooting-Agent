"""Role bindings in Postgres.

The distributed half of `MemberStore`, imported only when `DATABASE_URL` is set
like everything else under `app/persistence/`.

There is no tenant in any query in this file, and that is the point. Migration
004 defaults `tenant_id` to `current_setting('app.current_tenant')` and filters
reads with a row-level security policy, so `SELECT ... FROM tenant_members` with
no WHERE clause returns one tenant's members — the same property M6 established
for investigations, applied to the table that decides who may do things.
"""

from datetime import UTC, datetime

from app.authz.models import Membership, Role
from app.persistence.postgres import Database

_COLUMNS = "subject, role, email, suspended, granted_by, created_at, updated_at, last_seen_at"


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _hydrate(row: tuple) -> Membership:
    subject, role, email, suspended, granted_by, created_at, updated_at, last_seen_at = row
    return Membership(
        subject=subject,
        # NULL is "seen, never granted", not `viewer`.
        role=Role.parse(role) if role else None,
        email=email or "",
        suspended=bool(suspended),
        granted_by=granted_by or "",
        created_at=_aware(created_at),
        updated_at=_aware(updated_at),
        last_seen_at=_aware(last_seen_at),
    )


class PostgresMemberStore:
    """Durable, shared, and isolated by the same policy as everything else."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, subject: str) -> Membership | None:
        with self._database.cursor() as cursor:
            cursor.execute(f"SELECT {_COLUMNS} FROM tenant_members WHERE subject = %s", (subject,))
            row = cursor.fetchone()
        return _hydrate(row) if row else None

    def list(self) -> list[Membership]:
        with self._database.cursor() as cursor:
            # No WHERE clause on purpose. The policy is the filter.
            cursor.execute(
                f"SELECT {_COLUMNS} FROM tenant_members "
                "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
                "WHEN 'operator' THEN 2 WHEN 'viewer' THEN 3 ELSE 4 END, subject"
            )
            return [_hydrate(row) for row in cursor.fetchall()]

    def upsert(self, subject: str, role: Role, email: str = "", granted_by: str = "") -> Membership:
        with self._database.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO tenant_members (subject, role, email, granted_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, subject) DO UPDATE
                   SET role = EXCLUDED.role,
                       granted_by = EXCLUDED.granted_by,
                       -- An empty email in a grant must not erase one learned
                       -- at login; `rbacctl` grants by subject and has none.
                       email = COALESCE(NULLIF(EXCLUDED.email, ''), tenant_members.email),
                       updated_at = now()
                RETURNING {_COLUMNS}
                """,
                (subject, str(role), email, granted_by),
            )
            return _hydrate(cursor.fetchone())

    def remove(self, subject: str) -> bool:
        with self._database.cursor() as cursor:
            cursor.execute("DELETE FROM tenant_members WHERE subject = %s", (subject,))
            return cursor.rowcount > 0

    def set_suspended(self, subject: str, suspended: bool) -> Membership | None:
        with self._database.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE tenant_members
                   SET suspended = %s, updated_at = now()
                 WHERE subject = %s
                RETURNING {_COLUMNS}
                """,
                (suspended, subject),
            )
            row = cursor.fetchone()
        return _hydrate(row) if row else None

    def touch(self, subject: str, email: str = "") -> None:
        """Record the sighting. Never writes a role — see `Membership`."""
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tenant_members (subject, email, last_seen_at)
                VALUES (%s, %s, now())
                ON CONFLICT (tenant_id, subject) DO UPDATE
                   SET last_seen_at = now(),
                       email = COALESCE(NULLIF(EXCLUDED.email, ''), tenant_members.email)
                """,
                (subject, email),
            )

    def count_owners(self) -> int:
        with self._database.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM tenant_members WHERE role = 'owner' AND NOT suspended"
            )
            return int(cursor.fetchone()[0])
