"""Choosing where state lives, once, at startup.

Two deployments, one decision point:

- **single process** (no `DATABASE_URL`/`REDIS_URL`) — jobs in memory, reports
  on local disk. Needs no infrastructure, which is what keeps
  `uvicorn app.main:app --reload` the getting-started path.
- **multi worker** (both set) — jobs in Postgres, messages in Redis, reports as
  blobs. Jobs survive a restart and any worker can serve any investigation.

Nothing above this module knows which one it got.
"""

import asyncio
import os
import socket
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings
from app.jobs.runner import InvestigationJobRunner, set_job_runner
from app.jobs.store import InMemoryJobStore, set_job_store
from app.services.report_store import set_report_store


async def _retention_sweep() -> None:
    """Prune expired reports, now and then periodically.

    Runs in-process rather than as a cron job because the single-process
    deployment has nowhere else to put it, and the distributed one would
    otherwise need an operator to remember. Every worker sweeps; the work is
    idempotent, so overlap costs a little I/O and never correctness.
    """
    from app.services.report_store import get_report_store

    interval = max(0.5, settings.report_retention_sweep_hours) * 3600
    while True:
        try:
            removed = await asyncio.to_thread(
                get_report_store().prune, settings.report_retention_days
            )
            if removed:
                logger.info("Retention sweep removed {count} report artefact(s)", count=removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a sweep must not kill startup
            logger.opt(exception=exc).warning("Retention sweep failed")
        await asyncio.sleep(interval)


def start_retention(state: "StateBackend") -> None:
    if settings.report_retention_days <= 0:
        logger.info("Report retention is disabled; reports are kept indefinitely.")
        return
    state.retention = asyncio.create_task(_retention_sweep())
    logger.info(
        "Reports are kept for {days} days; sweeping every {hours}h",
        days=settings.report_retention_days,
        hours=settings.report_retention_sweep_hours,
    )


@dataclass
class StateBackend:
    """Everything startup created, so shutdown can take it down again."""

    store: Any
    runner: InvestigationJobRunner
    consumer: Any = None
    database: Any = None
    bus: Any = None
    gateway: Any = None
    retention: Any = None

    async def shutdown(self) -> None:
        """Tear down in dependency order, and un-install what startup installed.

        In-flight investigations stop first: they use the store, and closing a
        connection pool underneath a running job turns a clean shutdown into a
        burst of errors.

        Clearing the process-wide singletons last matters because startup set
        them. A closed pool that is still reachable through a module global is
        a trap for anything that outlives the application — the next `get_*`
        call would hand out a store whose connections are gone.
        """
        await self.runner.shutdown()
        if self.retention is not None:
            self.retention.cancel()
            self.retention = None
        if self.gateway is not None:
            await self.gateway.stop()
        if self.consumer is not None:
            await self.consumer.stop()
        if self.bus is not None:
            await self.bus.close()
        if self.database is not None:
            self.database.close()

        set_job_runner(None)
        set_job_store(None)
        set_report_store(None)

        if self.gateway is not None:
            from app.security.enrolment import set_enrolment_store

            set_enrolment_store(None)


def worker_identity() -> str:
    return settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"


async def start_agent_gateway(state: "StateBackend | None" = None):
    """The gRPC endpoint cluster agents dial into, when one is configured.

    Off by default and imported lazily: a deployment reading a local kubeconfig
    needs no agent, and should not load grpc or the certificate machinery to
    find that out.
    """
    if not settings.agent_gateway_enabled:
        return None

    settings.validate_agent_gateway()

    # Agent enrolment state follows the same decision the job store made: with
    # Postgres it is a shared table whose conditional UPDATE is what makes a
    # bootstrap token single-use across workers; without it, a file beside the
    # reports. Installed here rather than in `build_state` so a deployment with
    # no agents never imports any of it.
    if state is not None and settings.distributed_state and state.database is not None:
        from app.persistence.agent_identity import PostgresEnrolmentStore
        from app.security.enrolment import set_enrolment_store

        set_enrolment_store(PostgresEnrolmentStore(state.database))
        logger.info("Agent enrolment state is in Postgres")
    else:
        logger.info(
            "Agent enrolment state is a file under {path}. Single-use survives a "
            "restart; it is not safe for two server processes, which is the "
            "configuration DATABASE_URL/REDIS_URL already gate.",
            path=settings.agent_identity_dir,
        )

    from app.gateway.server import AgentGateway

    gateway = AgentGateway(settings.agent_gateway_port)
    await gateway.start()
    return gateway


def build_state() -> StateBackend:
    settings.validate_state_backend()

    if not settings.distributed_state:
        store = InMemoryJobStore()
        runner = InvestigationJobRunner(store)
        set_job_store(store)
        set_job_runner(runner)
        logger.info(
            "State is in-process: jobs do not survive a restart and a second "
            "worker will not see them. Set DATABASE_URL and REDIS_URL for a "
            "multi-worker deployment."
        )
        return StateBackend(store=store, runner=runner)

    # Imported here, not at module scope: the single-process deployment must
    # not need the Postgres or Redis drivers to be installed or importable.
    from app.jobs.consumer import JobConsumer
    from app.jobs.distributed import PostgresRedisJobStore
    from app.persistence.postgres import Database
    from app.persistence.redis_bus import RedisBus
    from app.services.report_store import PostgresReportStore

    worker = worker_identity()
    database = Database(settings.database_url)
    database.migrate()
    bus = RedisBus(settings.redis_url, prefix=settings.redis_key_prefix)

    store = PostgresRedisJobStore(database, bus)
    runner = InvestigationJobRunner(store)
    consumer = JobConsumer(store, runner, bus, worker_id=worker)
    consumer.start()

    set_job_store(store)
    set_job_runner(runner)
    set_report_store(PostgresReportStore(database))

    logger.info("State is distributed; this worker is {worker}", worker=worker)
    return StateBackend(
        store=store,
        runner=runner,
        consumer=consumer,
        database=database,
        bus=bus,
    )
