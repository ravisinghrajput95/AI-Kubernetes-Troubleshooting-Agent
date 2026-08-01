"""Agent enrolment state in Postgres.

The distributed half of `EnrolmentStore`. Imported only when `DATABASE_URL` is
set, like everything else under `app/persistence/`.

The load-bearing line in this file is the conditional `UPDATE` in
`spend_token()`. Single-use is not enforced by reading the row and then writing
it — that is a race with a window wide enough to drive two registrations
through. It is enforced by making the database decide: `WHERE consumed_at IS
NULL` matches for exactly one caller, and `RETURNING` tells that caller it won.
The same shape claims a job in `PostgresRedisJobStore`, for the same reason.
"""

from datetime import UTC, datetime, timedelta

from app.persistence.postgres import Database
from app.security.enrolment import (
    DEFAULT_TOKEN_TTL,
    CertificateRecord,
    TokenRecord,
    hash_token,
    mint_token,
)
from app.security.identity import require_cluster_id


def _aware(moment: datetime | None) -> datetime | None:
    """Postgres hands back tz-aware values; be explicit rather than assume."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class PostgresEnrolmentStore:
    """Bootstrap tokens and certificate revocation, durable and shared."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- tokens ------------------------------------------------------------

    def issue_token(self, cluster_id: str, ttl: timedelta = DEFAULT_TOKEN_TTL) -> str:
        require_cluster_id(cluster_id)
        token = mint_token()
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_bootstrap_tokens (token_hash, cluster_id, expires_at)
                VALUES (%s, %s, now() + %s)
                """,
                (hash_token(token), cluster_id, ttl),
            )
        return token

    def spend_token(self, token: str) -> str | None:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_bootstrap_tokens
                   SET consumed_at = now()
                 WHERE token_hash = %s
                   AND consumed_at IS NULL
                   AND expires_at > now()
             RETURNING cluster_id
                """,
                (hash_token(token),),
            )
            row = cursor.fetchone()
        return str(row[0]) if row else None

    def tokens(self, cluster_id: str = "") -> list[TokenRecord]:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                SELECT token_hash, cluster_id, created_at, expires_at, consumed_at
                  FROM agent_bootstrap_tokens
                 WHERE (%s = '' OR cluster_id = %s)
              ORDER BY created_at DESC
                """,
                (cluster_id, cluster_id),
            )
            rows = cursor.fetchall()
        return [
            TokenRecord(
                token_hash=row[0],
                cluster_id=row[1],
                created_at=_aware(row[2]),  # type: ignore[arg-type]
                expires_at=_aware(row[3]),  # type: ignore[arg-type]
                consumed_at=_aware(row[4]),
            )
            for row in rows
        ]

    # --- certificates ------------------------------------------------------

    def record_certificate(
        self, serial: str, cluster_id: str, expires_at: datetime
    ) -> CertificateRecord:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_certificates (serial, cluster_id, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (serial) DO NOTHING
             RETURNING issued_at
                """,
                (serial, cluster_id, expires_at),
            )
            row = cursor.fetchone()
        issued_at = _aware(row[0]) if row else datetime.now(UTC)
        return CertificateRecord(
            serial=serial,
            cluster_id=cluster_id,
            issued_at=issued_at,  # type: ignore[arg-type]
            expires_at=expires_at,
        )

    def revoke_certificate(self, serial: str, reason: str = "") -> bool:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_certificates
                   SET revoked_at = now(), revoked_reason = %s
                 WHERE serial = %s
                   AND revoked_at IS NULL
             RETURNING serial
                """,
                (reason, serial),
            )
            return cursor.fetchone() is not None

    def revoke_cluster(self, cluster_id: str, reason: str = "") -> int:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_certificates
                   SET revoked_at = now(), revoked_reason = %s
                 WHERE cluster_id = %s
                   AND revoked_at IS NULL
                   AND expires_at > now()
                """,
                (reason, cluster_id),
            )
            return cursor.rowcount

    def revoked_serials(self) -> set[str]:
        with self._database.cursor() as cursor:
            # Expired certificates are dropped from the answer: TLS already
            # refuses them, and carrying them forever would grow the set the
            # gateway holds in memory without bound.
            cursor.execute(
                """
                SELECT serial
                  FROM agent_certificates
                 WHERE revoked_at IS NOT NULL
                   AND expires_at > now()
                """
            )
            return {row[0] for row in cursor.fetchall()}

    def certificates(self, cluster_id: str = "") -> list[CertificateRecord]:
        with self._database.cursor() as cursor:
            cursor.execute(
                """
                SELECT serial, cluster_id, issued_at, expires_at, revoked_at, revoked_reason
                  FROM agent_certificates
                 WHERE (%s = '' OR cluster_id = %s)
              ORDER BY issued_at DESC
                """,
                (cluster_id, cluster_id),
            )
            rows = cursor.fetchall()
        return [
            CertificateRecord(
                serial=row[0],
                cluster_id=row[1],
                issued_at=_aware(row[2]),  # type: ignore[arg-type]
                expires_at=_aware(row[3]),  # type: ignore[arg-type]
                revoked_at=_aware(row[4]),
                revoked_reason=row[5] or "",
            )
            for row in rows
        ]
