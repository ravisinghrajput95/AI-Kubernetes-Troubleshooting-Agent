"""Opt-in access to a real Postgres and Redis for the integration tests.

Following the precedent set by `VITE_API_INTEGRATION` in the console: the
default suite is hermetic and needs no infrastructure, and the tests that prove
the distributed path works run only when someone asks for them.

    docker compose up -d postgres redis
    K8S_AGENT_INTEGRATION=1 python -m pytest tests/test_distributed_jobs.py

There is deliberately **no fake Postgres and no SQLite stand-in**. The store
depends on `jsonb`, `bigserial` and a conditional UPDATE for claiming; a
substitute would prove the tests pass rather than that the store works.
"""

import os
import uuid

import pytest

INTEGRATION_ENABLED = os.environ.get("K8S_AGENT_INTEGRATION") == "1"

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/k8sagent_test"
DEFAULT_REDIS_URL = "redis://localhost:6379/1"

SKIP_REASON = "Set K8S_AGENT_INTEGRATION=1 with Postgres and Redis running"

requires_backend = pytest.mark.skipif(not INTEGRATION_ENABLED, reason=SKIP_REASON)


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def redis_url() -> str:
    return os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL


class DistributedBackend:
    """A migrated database and an isolated Redis key space for one test."""

    def __init__(self, with_bus: bool = True) -> None:
        from app.persistence.postgres import Database

        self.database = Database(database_url(), min_size=1, max_size=4)
        self.database.migrate()
        self._truncate()

        # Agent enrolment is Postgres-only — single-use is a conditional UPDATE
        # and there is no latency layer in front of it — so those tests ask for
        # a backend without a bus rather than requiring a Redis they never use.
        self.bus = None
        if with_bus:
            from app.persistence.redis_bus import RedisBus

            # A unique prefix per test: two tests must not see each other's queue.
            self.bus = RedisBus(redis_url(), prefix=f"test-{uuid.uuid4().hex[:12]}")

    def store(self):
        from app.jobs.distributed import PostgresRedisJobStore

        return PostgresRedisJobStore(self.database, self.bus)

    def reports(self):
        from app.services.report_store import PostgresReportStore

        return PostgresReportStore(self.database)

    def unprivileged(self):
        """A second connection as a role that cannot bypass row-level security.

        The tenancy tests must not run as a superuser, because a superuser
        skips policies and would pass every isolation assertion while proving
        nothing. This creates the kind of role a real deployment uses and
        connects as it.
        """
        from app.persistence.postgres import Database

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'k8sagent_app') THEN
                        CREATE ROLE k8sagent_app LOGIN PASSWORD 'k8sagent_app';
                    END IF;
                END $$;
                """
            )
            cursor.execute("GRANT USAGE ON SCHEMA public TO k8sagent_app")
            cursor.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                "TO k8sagent_app"
            )
            cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO k8sagent_app")

        url = database_url()
        separator = "&" if "?" in url else "?"
        return Database(f"{url}{separator}user=k8sagent_app&password=k8sagent_app", max_size=4)

    def enrolment(self):
        from app.persistence.agent_identity import PostgresEnrolmentStore

        return PostgresEnrolmentStore(self.database)

    def members(self, database=None):
        from app.persistence.members import PostgresMemberStore

        return PostgresMemberStore(database or self.database)

    def drop_schema(self) -> None:
        """Return the database to empty, so a migration test starts from zero."""
        with self.database.cursor() as cursor:
            cursor.execute(
                "DROP TABLE IF EXISTS investigation_events, investigation_reports, "
                "investigations, agent_bootstrap_tokens, agent_certificates, "
                "tenant_members, schema_migrations CASCADE"
            )

    def _truncate(self) -> None:
        with self.database.cursor() as cursor:
            cursor.execute(
                "TRUNCATE investigations, investigation_events, investigation_reports, "
                "agent_bootstrap_tokens, agent_certificates, tenant_members "
                "RESTART IDENTITY CASCADE"
            )

    async def close(self) -> None:
        if self.bus is not None:
            await self.bus.close()
        self.database.close()
