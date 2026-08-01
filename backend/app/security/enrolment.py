"""Bootstrap tokens and certificate revocation — the state identity needs.

Two facts have to outlive a request, and both are the kind that must not be
wrong:

- **A bootstrap token is single-use.** Enrolment is the one moment an
  unauthenticated peer can obtain a credential, so spending a token has to be
  atomic against every other attempt to spend it. Whoever's `UPDATE` matches
  gets the certificate; everyone else is refused.
- **A certificate can be revoked.** The transport's defining property is a
  stream that stays open for weeks, which makes revocation-at-reconnect
  meaningless. The gateway therefore has to be able to ask "is this serial
  still good?" cheaply and repeatedly.

The seam is the same one `ReportStore` already has, chosen by the same
configuration:

    both DATABASE_URL and REDIS_URL unset  → FileEnrolmentStore  (data/)
    both set                                → PostgresEnrolmentStore

Tokens are stored **hashed**. A leaked `data/` directory or a database dump
yields no enrollable credential, which is the whole reason to hash something
that is already short-lived and single-use.
"""

import hashlib
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from app.security.identity import require_cluster_id

# Recognisable at a glance in a log or a secret scanner, and distinct from the
# platform's own API tokens.
TOKEN_PREFIX = "k8sagt_"

# 32 bytes of entropy. The token is single-use and short-lived, so this is
# comfortably beyond what an online guessing attack could reach.
TOKEN_BYTES = 32

DEFAULT_TOKEN_TTL = timedelta(hours=1)


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The form the platform stores. Never reversible to the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CertificateRecord:
    """One certificate the platform issued, and whether it still counts."""

    serial: str
    cluster_id: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str = ""

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def describe(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "cluster_id": self.cluster_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked": self.revoked,
            "revoked_reason": self.revoked_reason,
        }


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """A bootstrap token, described without disclosing it."""

    token_hash: str
    cluster_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @property
    def spent(self) -> bool:
        return self.consumed_at is not None

    def describe(self) -> dict[str, Any]:
        return {
            # Enough to correlate a log line with a row, useless as a credential.
            "token_hash_prefix": self.token_hash[:12],
            "cluster_id": self.cluster_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "spent": self.spent,
            "expired": self.expires_at <= _now(),
        }


@runtime_checkable
class EnrolmentStore(Protocol):
    """Where single-use and revocation actually live."""

    def issue_token(self, cluster_id: str, ttl: timedelta = DEFAULT_TOKEN_TTL) -> str:
        """Create a token for `cluster_id` and return it. The only time it exists in the clear."""
        ...

    def spend_token(self, token: str) -> str | None:
        """Atomically consume `token`, returning the cluster it was bound to.

        `None` covers unknown, already-spent and expired without distinguishing
        them: the caller is unauthenticated, and which of the three it was is
        not information it has earned.
        """
        ...

    def record_certificate(
        self, serial: str, cluster_id: str, expires_at: datetime
    ) -> CertificateRecord: ...

    def revoke_certificate(self, serial: str, reason: str = "") -> bool:
        """True if a live certificate was revoked by this call."""
        ...

    def revoke_cluster(self, cluster_id: str, reason: str = "") -> int:
        """Revoke every unexpired certificate for a cluster. Returns the count."""
        ...

    def revoked_serials(self) -> set[str]:
        """Every serial the gateway must now refuse."""
        ...

    def certificates(self, cluster_id: str = "") -> list[CertificateRecord]: ...

    def tokens(self, cluster_id: str = "") -> list[TokenRecord]: ...


class FileEnrolmentStore:
    """The single-process default: one JSON file beside the reports.

    The precedent is `FilesystemReportStore`, and so is the honest limitation.
    Single-use survives a restart, because this is a file rather than memory,
    and the read-modify-write is serialised in-process and replaced atomically
    so a concurrent reader never sees a torn file. What it does **not** survive
    is two server processes writing at once — and it does not have to, because
    that configuration is exactly what `validate_state_backend()` already
    refuses without Postgres. The CLI is the only other writer, and it appends.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    # --- storage -----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"tokens": {}, "certificates": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Refusing is the only safe answer: an unreadable enrolment file
            # means single-use cannot be guaranteed, and continuing would
            # silently accept a replayed token.
            raise RuntimeError(
                f"The agent enrolment store at {self._path} is unreadable ({exc}). "
                f"Refusing to continue, because single-use tokens cannot be "
                f"guaranteed without it."
            ) from exc
        data.setdefault("tokens", {})
        data.setdefault("certificates", {})
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

    # --- tokens ------------------------------------------------------------

    def issue_token(self, cluster_id: str, ttl: timedelta = DEFAULT_TOKEN_TTL) -> str:
        require_cluster_id(cluster_id)
        token = mint_token()
        now = _now()
        with self._lock:
            data = self._read()
            data["tokens"][hash_token(token)] = {
                "cluster_id": cluster_id,
                "created_at": now.isoformat(),
                "expires_at": (now + ttl).isoformat(),
                "consumed_at": None,
            }
            self._write(data)
        return token

    def spend_token(self, token: str) -> str | None:
        digest = hash_token(token)
        with self._lock:
            data = self._read()
            row = data["tokens"].get(digest)
            if row is None or row.get("consumed_at") is not None:
                return None
            if datetime.fromisoformat(row["expires_at"]) <= _now():
                return None
            row["consumed_at"] = _now().isoformat()
            self._write(data)
            return str(row["cluster_id"])

    def tokens(self, cluster_id: str = "") -> list[TokenRecord]:
        with self._lock:
            data = self._read()
        records = [
            TokenRecord(
                token_hash=digest,
                cluster_id=row["cluster_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                consumed_at=(
                    datetime.fromisoformat(row["consumed_at"]) if row.get("consumed_at") else None
                ),
            )
            for digest, row in data["tokens"].items()
            if not cluster_id or row["cluster_id"] == cluster_id
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    # --- certificates ------------------------------------------------------

    def record_certificate(
        self, serial: str, cluster_id: str, expires_at: datetime
    ) -> CertificateRecord:
        now = _now()
        with self._lock:
            data = self._read()
            data["certificates"][serial] = {
                "cluster_id": cluster_id,
                "issued_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "revoked_at": None,
                "revoked_reason": "",
            }
            self._write(data)
        return CertificateRecord(
            serial=serial, cluster_id=cluster_id, issued_at=now, expires_at=expires_at
        )

    def revoke_certificate(self, serial: str, reason: str = "") -> bool:
        with self._lock:
            data = self._read()
            row = data["certificates"].get(serial)
            if row is None or row.get("revoked_at") is not None:
                return False
            row["revoked_at"] = _now().isoformat()
            row["revoked_reason"] = reason
            self._write(data)
            return True

    def revoke_cluster(self, cluster_id: str, reason: str = "") -> int:
        now = _now()
        with self._lock:
            data = self._read()
            revoked = 0
            for row in data["certificates"].values():
                if row["cluster_id"] != cluster_id or row.get("revoked_at") is not None:
                    continue
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    continue
                row["revoked_at"] = now.isoformat()
                row["revoked_reason"] = reason
                revoked += 1
            if revoked:
                self._write(data)
            return revoked

    def revoked_serials(self) -> set[str]:
        with self._lock:
            data = self._read()
        return {
            serial
            for serial, row in data["certificates"].items()
            if row.get("revoked_at") is not None
        }

    def certificates(self, cluster_id: str = "") -> list[CertificateRecord]:
        with self._lock:
            data = self._read()
        records = [
            CertificateRecord(
                serial=serial,
                cluster_id=row["cluster_id"],
                issued_at=datetime.fromisoformat(row["issued_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                revoked_at=(
                    datetime.fromisoformat(row["revoked_at"]) if row.get("revoked_at") else None
                ),
                revoked_reason=row.get("revoked_reason", ""),
            )
            for serial, row in data["certificates"].items()
            if not cluster_id or row["cluster_id"] == cluster_id
        ]
        return sorted(records, key=lambda record: record.issued_at, reverse=True)


_store: EnrolmentStore | None = None


def set_enrolment_store(store: EnrolmentStore | None) -> None:
    global _store
    _store = store


def get_enrolment_store() -> EnrolmentStore:
    """The process's enrolment store, built on first use if startup did not.

    Falls back to the file store rather than raising: `agentctl` runs outside
    the application and must reach the same rows the gateway will.
    """
    global _store
    if _store is None:
        from app.core.config import settings

        _store = FileEnrolmentStore(Path(settings.agent_identity_dir) / "enrolment.json")
        logger.debug(
            "Agent enrolment state is file-backed at {path}", path=settings.agent_identity_dir
        )
    return _store
