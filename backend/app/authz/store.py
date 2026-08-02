"""Where role bindings live.

The same seam, chosen by the same configuration, as every other store here:

    DATABASE_URL unset -> FileMemberStore    (data/, JSON, atomic replace)
    DATABASE_URL set   -> PostgresMemberStore (migration 004, under RLS)

Deliberately **not** an in-memory store. Roles must survive a restart even in
the single-process deployment: an operator who sets `RBAC_DEFAULT_ROLE=viewer`
and assigns roles by hand would otherwise be locked out of their own platform by
a `docker restart`. `FileEnrolmentStore` is the precedent, and so is its honest
limitation — the file is safe for one process, which is the configuration
`validate_state_backend()` already gates.

Both implementations are held to `tests/test_member_store_contract.py`. The same
assertions against both is what has kept the job and enrolment stores from
diverging, and a role binding is not the place to start.

There is no tenant argument anywhere in this file. The file store is per
deployment and the Postgres one is under row-level security, so the tenant is
ambient in both — exactly as M6 left it.
"""

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from app.authz.models import Membership, Role
from app.tenancy import current_tenant


def _now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class MemberStore(Protocol):
    """Role bindings within one tenant."""

    def get(self, subject: str) -> Membership | None: ...

    def list(self) -> list[Membership]: ...

    def upsert(
        self,
        subject: str,
        role: Role,
        email: str = "",
        granted_by: str = "",
    ) -> Membership:
        """Create or re-grant a binding. Returns the stored row."""
        ...

    def remove(self, subject: str) -> bool:
        """Drop a binding. True when one existed."""
        ...

    def set_suspended(self, subject: str, suspended: bool) -> Membership | None: ...

    def touch(self, subject: str, email: str = "") -> None:
        """Record that this subject was seen, without changing their role.

        Called on every authenticated request, so it must be cheap and must
        never create authority — an unbound caller stays unbound and is
        recorded with whatever role the resolver would have given them anyway.
        """
        ...

    def count_owners(self) -> int:
        """How many un-suspended owners this tenant has.

        The last-owner check reads this. A tenant with zero owners cannot grant
        one back over HTTP, which is what `rbacctl` is for.
        """
        ...


class FileMemberStore:
    """The single-process default: one JSON file, keyed by tenant.

    Keyed by tenant even though this store serves the single-tenant deployment,
    because `TENANCY_MODE=shared` is refused without Postgres but `tenant_scope`
    is not — a test, a CLI invocation or a future caller can still set one, and
    silently merging two tenants' role bindings into one dictionary is the kind
    of bug that only shows up as a cross-tenant admin.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"tenants": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Refusing rather than starting empty. An unreadable member file
            # read as "no bindings" would hand every caller the default role,
            # which in single-tenant mode is `admin`.
            raise RuntimeError(
                f"The member store at {self._path} is unreadable ({exc}). Refusing "
                f"to continue, because an empty role table would grant the default "
                f"role to everyone."
            ) from exc
        data.setdefault("tenants", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _members(self, data: dict[str, Any]) -> dict[str, Any]:
        return data["tenants"].setdefault(current_tenant(), {})

    @staticmethod
    def _hydrate(subject: str, row: dict[str, Any]) -> Membership:
        return Membership(
            subject=subject,
            # Absent means seen but never granted, which is not the same as
            # `viewer` and must not be stored as it.
            role=Role.parse(row["role"]) if row.get("role") else None,
            email=row.get("email", ""),
            suspended=bool(row.get("suspended", False)),
            granted_by=row.get("granted_by", ""),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_seen_at=(
                datetime.fromisoformat(row["last_seen_at"]) if row.get("last_seen_at") else None
            ),
        )

    def get(self, subject: str) -> Membership | None:
        with self._lock:
            row = self._members(self._read()).get(subject)
        return self._hydrate(subject, row) if row else None

    def list(self) -> list[Membership]:
        with self._lock:
            members = self._members(self._read())
        return sorted(
            (self._hydrate(subject, row) for subject, row in members.items()),
            key=lambda member: (-(member.role.rank if member.role else -1), member.subject),
        )

    def upsert(self, subject: str, role: Role, email: str = "", granted_by: str = "") -> Membership:
        now = _now()
        with self._lock:
            data = self._read()
            members = self._members(data)
            row = members.get(subject, {"created_at": now.isoformat(), "suspended": False})
            row["role"] = str(role)
            row["updated_at"] = now.isoformat()
            row["granted_by"] = granted_by
            if email:
                row["email"] = email
            members[subject] = row
            self._write(data)
            return self._hydrate(subject, row)

    def remove(self, subject: str) -> bool:
        with self._lock:
            data = self._read()
            members = self._members(data)
            if subject not in members:
                return False
            del members[subject]
            self._write(data)
            return True

    def set_suspended(self, subject: str, suspended: bool) -> Membership | None:
        now = _now()
        with self._lock:
            data = self._read()
            members = self._members(data)
            row = members.get(subject)
            if row is None:
                return None
            row["suspended"] = suspended
            row["updated_at"] = now.isoformat()
            self._write(data)
            return self._hydrate(subject, row)

    def touch(self, subject: str, email: str = "") -> None:
        now = _now()
        with self._lock:
            data = self._read()
            members = self._members(data)
            row = members.get(subject)
            if row is None:
                # Seen, not granted. The row records that this person exists so
                # an admin can find them in `GET /members`, and carries no role
                # at all — see `Membership`.
                row = {
                    "role": None,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "granted_by": "",
                    "suspended": False,
                }
                members[subject] = row
            if email and row.get("email") != email:
                row["email"] = email
            row["last_seen_at"] = now.isoformat()
            self._write(data)

    def count_owners(self) -> int:
        return sum(
            1 for member in self.list() if member.role is Role.OWNER and not member.suspended
        )


_store: MemberStore | None = None


def set_member_store(store: MemberStore | None) -> None:
    global _store
    _store = store


def get_member_store() -> MemberStore:
    """The process's member store, built on first use if startup did not.

    Falls back to the file store rather than raising, so `rbacctl` reaches the
    same rows the API will — the same reason `get_enrolment_store()` does.
    """
    global _store
    if _store is None:
        from app.core.config import settings

        _store = FileMemberStore(Path(settings.rbac_store_dir) / "members.json")
        logger.debug("Role bindings are file-backed at {path}", path=settings.rbac_store_dir)
    return _store
