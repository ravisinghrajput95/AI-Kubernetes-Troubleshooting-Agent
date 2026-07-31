"""Postgres connection pooling.

`psycopg` is imported here and nowhere else, and this module is only imported
when `DATABASE_URL` is set. The default single-process deployment never touches
the driver.
"""

from contextlib import contextmanager

from loguru import logger

from app.persistence.migrator import migrate


class Database:
    """A pooled Postgres connection, sized for one API worker."""

    def __init__(self, url: str, min_size: int = 1, max_size: int = 10) -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            # Fail a request rather than hanging on it when the pool is starved.
            timeout=10.0,
        )
        self._pool.open(wait=True, timeout=30.0)
        logger.info("Postgres pool ready ({min}-{max} connections)", min=min_size, max=max_size)

    @contextmanager
    def connection(self):
        with self._pool.connection() as connection:
            yield connection

    @contextmanager
    def cursor(self):
        """A cursor in its own transaction, committed on clean exit."""
        with self._pool.connection() as connection, connection.cursor() as cursor:
            yield cursor

    def migrate(self) -> list[str]:
        with self._pool.connection() as connection:
            # Migrations manage their own transactions.
            connection.autocommit = False
            applied = migrate(connection)
        if applied:
            logger.info("Applied {count} migration(s): {names}", count=len(applied), names=applied)
        return applied

    def close(self) -> None:
        self._pool.close()
