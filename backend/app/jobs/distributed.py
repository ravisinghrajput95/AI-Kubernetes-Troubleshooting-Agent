"""Job state in Postgres, job messages in Redis.

Satisfies the same `JobStore` protocol as the in-process store, so the API
handlers and the runner are unchanged by which one is installed.

Three properties are load-bearing and easy to regress:

- **Postgres is the truth, Redis is the latency.** Every message has a
  committed row behind it. Losing a message costs time, never correctness.
- **A claim is a conditional UPDATE, not a lock.** Exactly one worker can move
  a row out of `pending`, so two workers cannot run the same investigation.
- **Subscribe before replaying.** `subscribe()` opens the live subscription
  before it reads the backlog, and de-duplicates by sequence afterwards. The
  other order loses every event published during the read.
"""

# `JobStore` requires a method named `list`, which shadows the builtin for every
# annotation evaluated later in the class body. Python 3.12 evaluates those
# eagerly and fails at import; 3.14 defers them and hides the problem entirely.
# Deferring here keeps the protocol's method name and the builtin annotation.
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from loguru import logger

from app.jobs.base import CancelListener, EventSequencer
from app.jobs.models import InvestigationJob, JobEvent, JobEventType, JobStatus
from app.persistence.postgres import Database
from app.persistence.redis_bus import RedisBus

_JOB_COLUMNS = (
    "id, owner, principal, status, request, result, error, cancel_requested, "
    "lease_worker, lease_expires_at, created_at, started_at, finished_at"
)

# The same columns without `result`, for the paths that do not want it.
#
# `result` is the whole investigation and diagnosis — measured at 2.7 MB on a
# cluster at the `MAX_LIST_ITEMS` ceiling (`scripts/payload_bench.py`). A
# listing selected it for every row and the API then dropped it in Python via
# `to_dict(include_result=False)`: 67.5 MB read out of Postgres and 0 bytes of
# it returned, on every dashboard load. Excluding it in SQL is the whole fix.
#
# Kept as a separate constant rather than a flag on one string, so that adding
# a column means adding it to both and a mismatch is a visible diff rather than
# a silent absence.
_JOB_SUMMARY_COLUMNS = (
    "id, owner, principal, status, request, error, cancel_requested, "
    "lease_worker, lease_expires_at, created_at, started_at, finished_at"
)


class PostgresRedisJobStore:
    distributed = True

    def __init__(self, database: Database, bus: RedisBus) -> None:
        self._db = database
        self._bus = bus
        self._cancel_listeners: list[CancelListener] = []

    # --- lifecycle ----------------------------------------------------------

    def create(
        self,
        request: dict[str, Any],
        owner: str = "",
        principal: dict[str, Any] | None = None,
    ) -> InvestigationJob:
        from psycopg.types.json import Jsonb

        job_id = str(uuid4())
        with self._db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO investigations (id, owner, principal, status, request)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    owner,
                    Jsonb(principal) if principal is not None else None,
                    str(JobStatus.PENDING),
                    Jsonb(request),
                ),
            )

        self.publish(job_id, JobEvent(JobEventType.QUEUED, "Investigation queued"))
        job = self.get(job_id)
        if job is None:  # pragma: no cover - the row was just committed
            raise RuntimeError(f"Investigation {job_id} vanished immediately after insert")
        return job

    def get(self, job_id: str) -> InvestigationJob | None:
        row = self._fetch_row(job_id)
        if row is None:
            return None
        job = self._to_job(row)
        job.events = self.events_since(job_id)
        return job

    def list(self, limit: int = 25, owner: str | None = None) -> list[InvestigationJob]:
        from psycopg.rows import dict_row

        clause = "" if owner is None else "WHERE owner = '' OR owner = %s"
        params: tuple = () if owner is None else (owner,)

        with self._db.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {_JOB_SUMMARY_COLUMNS} FROM investigations {clause} "
                "ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            rows = cursor.fetchall()
            jobs = [self._to_job(row) for row in rows]
            if not jobs:
                return jobs

            # One query for every timeline, rather than one per job.
            cursor.execute(
                "SELECT investigation_id, seq, type, message, data, at "
                "FROM investigation_events WHERE investigation_id = ANY(%s) ORDER BY seq",
                ([job.id for job in jobs],),
            )
            timelines: dict[str, list[JobEvent]] = {job.id: [] for job in jobs}
            for event_row in cursor.fetchall():
                timelines[event_row["investigation_id"]].append(self._to_event(event_row))

        for job in jobs:
            job.events = timelines[job.id]
        return jobs

    def mark_running(self, job_id: str) -> None:
        self._transition(
            job_id,
            "UPDATE investigations SET status = %s, started_at = now() WHERE id = %s",
            (str(JobStatus.RUNNING), job_id),
        )
        self.publish(job_id, JobEvent(JobEventType.STARTED, "Investigation started"))

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        self._transition(
            job_id,
            "UPDATE investigations SET status = %s, result = %s, finished_at = now(), "
            "lease_worker = NULL, lease_expires_at = NULL WHERE id = %s",
            (str(JobStatus.SUCCEEDED), Jsonb(result), job_id),
        )
        self.publish(job_id, JobEvent(JobEventType.COMPLETED, "Investigation complete"))

    def mark_failed(
        self,
        job_id: str,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        # COALESCE so a failure with nothing to show does not erase a result
        # an earlier transition already stored.
        self._transition(
            job_id,
            "UPDATE investigations SET status = %s, error = %s, "
            "result = COALESCE(%s, result), finished_at = now(), "
            "lease_worker = NULL, lease_expires_at = NULL WHERE id = %s",
            (
                str(JobStatus.FAILED),
                error,
                Jsonb(result) if result is not None else None,
                job_id,
            ),
        )
        self.publish(job_id, JobEvent(JobEventType.FAILED, error))

    def mark_cancelled(self, job_id: str) -> None:
        self._transition(
            job_id,
            "UPDATE investigations SET status = %s, finished_at = now(), "
            "lease_worker = NULL, lease_expires_at = NULL WHERE id = %s",
            (str(JobStatus.CANCELLED), job_id),
        )
        self.publish(job_id, JobEvent(JobEventType.CANCELLED, "Investigation cancelled"))

    def _transition(self, job_id: str, sql: str, params: tuple) -> None:
        with self._db.cursor() as cursor:
            cursor.execute(sql, params)

    # --- cancellation -------------------------------------------------------

    def request_cancel(self, job_id: str) -> bool:
        """Commit the intent, then announce it.

        In that order: if the announcement is lost, the worker's watchdog finds
        the committed flag. If the order were reversed, a crash between the two
        would leave a job that a message said to cancel and nothing to prove it.
        """
        with self._db.cursor() as cursor:
            cursor.execute(
                "UPDATE investigations SET cancel_requested = true "
                "WHERE id = %s AND status IN (%s, %s)",
                (job_id, str(JobStatus.PENDING), str(JobStatus.RUNNING)),
            )
            updated = cursor.rowcount

        if not updated:
            return self._fetch_row(job_id) is not None

        self._bus.request_cancel(job_id)
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        """The watchdog's question: has anyone asked for this to stop?"""
        with self._db.cursor() as cursor:
            cursor.execute("SELECT cancel_requested FROM investigations WHERE id = %s", (job_id,))
            row = cursor.fetchone()
        return bool(row and row[0])

    def on_cancel(self, listener: CancelListener) -> None:
        self._cancel_listeners.append(listener)

    def notify_cancel(self, job_id: str) -> None:
        """Invoked by the control listener when a cancel message arrives."""
        for listener in self._cancel_listeners:
            try:
                listener(job_id)
            except Exception as exc:
                logger.opt(exception=exc).warning(
                    "Cancellation listener failed for {id}", id=job_id
                )

    # --- queue and leases ---------------------------------------------------

    def enqueue(self, job_id: str, worker_id: str = "") -> None:
        self._bus.enqueue(job_id, worker_id)

    def claim(self, job_id: str, worker: str, lease_seconds: int) -> InvestigationJob | None:
        """Take ownership of a pending job, or return None if someone else did.

        The `WHERE status = 'pending'` is the mutual exclusion: two workers can
        both pop the id, but only one UPDATE will match a row.
        """
        from psycopg.rows import dict_row

        with self._db.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"""
                UPDATE investigations
                   SET status = %s,
                       started_at = now(),
                       lease_worker = %s,
                       lease_expires_at = now() + make_interval(secs => %s)
                 WHERE id = %s AND status = %s AND NOT cancel_requested
             RETURNING {_JOB_COLUMNS}
                """,
                (str(JobStatus.RUNNING), worker, lease_seconds, job_id, str(JobStatus.PENDING)),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        self.publish(job_id, JobEvent(JobEventType.STARTED, "Investigation started"))
        return self._to_job(row)

    def renew_lease(self, job_id: str, worker: str, lease_seconds: int) -> None:
        with self._db.cursor() as cursor:
            cursor.execute(
                "UPDATE investigations "
                "SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE id = %s AND lease_worker = %s",
                (lease_seconds, job_id, worker),
            )

    def reap_expired(self, error: str) -> list[str]:
        """Fail jobs whose worker died, and re-queue ones never claimed.

        This is what "jobs survive a restart" means in practice: the record is
        durable and reaches a terminal state, rather than sitting in `running`
        forever because the process that owned it is gone.
        """
        reaped: list[str] = []
        with self._db.cursor() as cursor:
            cursor.execute(
                "UPDATE investigations SET status = %s, error = %s, finished_at = now(), "
                "lease_worker = NULL, lease_expires_at = NULL "
                "WHERE status = %s AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < now() RETURNING id",
                (str(JobStatus.FAILED), error, str(JobStatus.RUNNING)),
            )
            reaped = [row[0] for row in cursor.fetchall()]

        for job_id in reaped:
            self.publish(job_id, JobEvent(JobEventType.FAILED, error))
            logger.warning("Reaped investigation {id}: lease expired", id=job_id)
        return reaped

    def requeue_unclaimed(self, older_than_seconds: int) -> list[str]:
        """Re-offer pending jobs whose queue message never reached a worker.

        Always to the **shared** queue, never back to a worker queue. A job
        that has waited this long was either routed to a worker that died or
        lost outright, and re-offering it to the same worker would strand it
        permanently in the first case. Whoever picks it up either holds the
        agent's stream or refuses honestly in `select_provider`, so the shared
        queue cannot produce a wrong answer — only a slower one.
        """
        with self._db.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM investigations WHERE status = %s "
                "AND created_at < now() - make_interval(secs => %s)",
                (str(JobStatus.PENDING), older_than_seconds),
            )
            stale = [row[0] for row in cursor.fetchall()]

        for job_id in stale:
            self._bus.enqueue(job_id)
            logger.info("Re-queued unclaimed investigation {id}", id=job_id)
        return stale

    # --- events -------------------------------------------------------------

    def publish(self, job_id: str, event: JobEvent) -> None:
        """Persist the event, then announce it with the sequence it was given.

        Postgres assigns the sequence, so ordering does not depend on worker
        clocks agreeing, and a subscriber can always resume from a position.
        """
        from psycopg.types.json import Jsonb

        try:
            with self._db.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO investigation_events (investigation_id, type, message, data, at) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                    (job_id, str(event.type), event.message, Jsonb(event.data), event.at),
                )
                row = cursor.fetchone()
        except Exception as exc:
            # Progress reporting must never take an investigation down with it.
            logger.opt(exception=exc).warning("Could not record event for job {id}", id=job_id)
            return

        if row is None:  # pragma: no cover
            return

        payload = {**event.to_dict(), "seq": row[0]}
        try:
            self._bus.publish_event(job_id, payload)
        except Exception as exc:
            # The row is committed; a subscriber will still see it on replay.
            logger.opt(exception=exc).warning("Could not fan out event for job {id}", id=job_id)

    def events_since(self, job_id: str, after_seq: int = 0) -> list[JobEvent]:
        from psycopg.rows import dict_row

        with self._db.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT seq, type, message, data, at FROM investigation_events "
                "WHERE investigation_id = %s AND seq > %s ORDER BY seq",
                (job_id, after_seq),
            )
            return [self._to_event(row) for row in cursor.fetchall()]

    async def subscribe(
        self,
        job_id: str,
        heartbeat: float | None = None,
        after_seq: int = 0,
    ) -> AsyncIterator[JobEvent | None]:
        """Replay, then go live, dropping and duplicating nothing.

        The order below is the whole correctness argument, and the comments
        mark the two steps that must not be swapped.
        """
        if await asyncio.to_thread(self._fetch_row, job_id) is None:
            return

        sequencer = EventSequencer(after_seq)
        # 1. Live first. Anything published from here on is buffered by Redis
        #    for this subscription, including events published during step 2.
        subscription = await self._bus.subscribe_events(job_id)

        try:
            # 2. Then the backlog, which is authoritative and ordered.
            for event in await asyncio.to_thread(self.events_since, job_id, after_seq):
                if sequencer.accept(event):
                    yield event

            # 3. Anything buffered during step 2 that the backlog already
            #    covered is discarded here, by sequence.
            for payload in await subscription.drain():
                event = JobEvent.from_dict(payload)
                if sequencer.accept(event):
                    yield event
                    if event.type in _TERMINAL_EVENTS:
                        return

            row = await asyncio.to_thread(self._fetch_row, job_id)
            if row is not None and JobStatus(row["status"]).terminal:
                return

            # 4. Live, with the same filter.
            wait = heartbeat if heartbeat is not None else 30.0
            while True:
                payload = await subscription.next_event(timeout=wait)
                if payload is None:
                    if heartbeat is not None:
                        yield None
                    continue
                event = JobEvent.from_dict(payload)
                if not sequencer.accept(event):
                    continue
                yield event
                if event.type in _TERMINAL_EVENTS:
                    return
        finally:
            await subscription.close()

    # --- row mapping --------------------------------------------------------

    def _fetch_row(self, job_id: str) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        with self._db.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {_JOB_COLUMNS} FROM investigations WHERE id = %s", (job_id,))
            return cursor.fetchone()

    def _to_job(self, row: dict[str, Any]) -> InvestigationJob:
        return InvestigationJob(
            id=row["id"],
            request=row["request"] or {},
            owner=row["owner"] or "",
            principal=row["principal"],
            cancel_requested=bool(row["cancel_requested"]),
            status=JobStatus(row["status"]),
            # Absent on a summary read, which is different from a job that has
            # no result yet — but only to this constructor, because every
            # caller of the summary query discards it either way.
            result=row.get("result"),
            error=row["error"] or "",
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def _to_event(self, row: dict[str, Any]) -> JobEvent:
        return JobEvent(
            type=JobEventType(row["type"]),
            message=row["message"],
            at=row["at"],
            data=row["data"] or {},
            seq=row["seq"],
        )


_TERMINAL_EVENTS = {
    JobEventType.COMPLETED,
    JobEventType.FAILED,
    JobEventType.CANCELLED,
}
