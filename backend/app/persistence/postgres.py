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
        """A cursor in its own transaction, committed on clean exit.

        Every transaction announces which tenant it is for. `SET LOCAL` scopes
        that to the transaction, so a pooled connection handed to the next
        request cannot carry the previous one's tenant — which a plain `SET`
        would, and which would be the quietest possible cross-tenant leak.

        This is the only place the tenant reaches the database, and store
        methods never mention it: the column default stamps inserts and the
        row-level security policy filters reads. That is what makes isolation a
        property of the schema rather than of everyone remembering.
        """
        from app.tenancy import current_tenant

        with self._pool.connection() as connection, connection.cursor() as cursor:
            # Parameterised rather than interpolated. `SET LOCAL` does not take
            # a bind parameter, so `set_config` is the correct spelling — and
            # it is also the one that cannot be escaped out of.
            cursor.execute("SELECT set_config('app.current_tenant', %s, true)", (current_tenant(),))
            yield cursor

    def assert_row_level_security_applies(self) -> None:
        """Refuse to run multi-tenant as a role that bypasses row-level security.

        This is the check that turns tenant isolation from a claim into a
        control, and it exists because the claim was false the first time it
        was tested. `ENABLE`/`FORCE ROW LEVEL SECURITY` were both set, the
        policies were correct, and every tenant could still read every row —
        because the application connected as `postgres`, and superusers (and
        anything with `BYPASSRLS`) skip policies entirely.

        A deployment in that state has no isolation and no symptom. So a
        multi-tenant startup asks the database what it is, and refuses rather
        than serving two customers out of one unprotected table.
        """
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, rolsuper OR rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            row = cursor.fetchone()

        if row is None:
            raise RuntimeError("Could not determine the database role; refusing to start.")

        role, bypasses = row
        if bypasses:
            raise RuntimeError(
                f"TENANCY_MODE=shared, but this deployment connects to Postgres as "
                f"{role!r}, which bypasses row-level security. Every tenant would "
                f"read every other tenant's rows and nothing would look wrong. "
                f"Connect as a role with neither SUPERUSER nor BYPASSRLS — the "
                f"migrations grant what the application needs."
            )

        logger.info("Tenant isolation is enforced; database role {role} obeys RLS", role=role)

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
