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

    def __init__(self) -> None:
        from app.persistence.postgres import Database
        from app.persistence.redis_bus import RedisBus

        self.database = Database(database_url(), min_size=1, max_size=4)
        self.database.migrate()
        self._truncate()
        # A unique prefix per test: two tests must not see each other's queue.
        self.bus = RedisBus(redis_url(), prefix=f"test-{uuid.uuid4().hex[:12]}")

    def store(self):
        from app.jobs.distributed import PostgresRedisJobStore

        return PostgresRedisJobStore(self.database, self.bus)

    def reports(self):
        from app.services.report_store import PostgresReportStore

        return PostgresReportStore(self.database)

    def drop_schema(self) -> None:
        """Return the database to empty, so a migration test starts from zero."""
        with self.database.cursor() as cursor:
            cursor.execute(
                "DROP TABLE IF EXISTS investigation_events, investigation_reports, "
                "investigations, schema_migrations CASCADE"
            )

    def _truncate(self) -> None:
        with self.database.cursor() as cursor:
            cursor.execute(
                "TRUNCATE investigations, investigation_events, investigation_reports "
                "RESTART IDENTITY CASCADE"
            )

    async def close(self) -> None:
        await self.bus.close()
        self.database.close()
