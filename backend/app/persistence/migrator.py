"""Forward-only schema migrations, applied under an advisory lock.

Why not Alembic: there is no ORM here. Alembic's value is autogenerating
revisions by diffing declarative models, and without SQLAlchemy we would carry
the dependency and get none of that. The schema is small and hand-written, and
a change should read as SQL in review — the same argument that keeps the
generated protobuf bindings committed rather than built.

What this deliberately does not have: autogenerate, and downgrades. Forward-only
is the right production discipline anyway. A bad migration is corrected by the
next migration, not by a rollback that has to guess what the data meant.

Revisit if SQLAlchemy ever arrives for some other reason; at that point Alembic
starts paying for itself.
"""

from pathlib import Path

from loguru import logger

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Arbitrary but fixed: every replica must pick the same lock. Booting ten
# workers at once must not race ten copies of the same CREATE TABLE.
ADVISORY_LOCK_KEY = 0x4B38_4147  # "K8AG"


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str]]:
    """`(version, sql)` pairs in lexical order, which is application order."""
    return [
        (path.stem, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.sql"))
    ]


def migrate(connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration this database has not seen. Returns what ran.

    Each migration commits with its own version row, so an interrupted run
    resumes rather than reapplying work that already succeeded.
    """
    applied: list[str] = []

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            connection.commit()

            cursor.execute("SELECT version FROM schema_migrations")
            known = {row[0] for row in cursor.fetchall()}

            for version, sql in discover_migrations(directory):
                if version in known:
                    continue
                logger.info("Applying migration {version}", version=version)
                cursor.execute(sql)
                cursor.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                connection.commit()
                applied.append(version)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            connection.commit()

    return applied
