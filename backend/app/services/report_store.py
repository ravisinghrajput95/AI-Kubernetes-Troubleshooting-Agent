"""Where rendered reports and the history index live.

`InvestigationHistoryService` composes and renders; this decides where the
bytes go. Two backends, chosen by the same configuration that chooses the job
store, so a deployment cannot end up with durable jobs and local-disk reports.

The interface returns **bytes**, never a path. That is what lets
`/investigations/{id}/pdf` answer on a worker that did not render the file, and
it is the seam M8 replaces with object storage: the read method changes, the
endpoints do not.
"""

import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

# Investigation ids are UUIDs. Anything else is rejected before it can be used
# to build a filesystem path or a database key.
SAFE_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Formats and the extension each is stored under.
EXTENSIONS = {"pdf": "pdf", "json": "json", "markdown": "md"}

HISTORY_LIMIT = 25

# How long a rendered report is kept.
#
# Investigations accumulate: a fleet under load produces them faster than
# anyone reads them, and the value of a report falls off a cliff once the
# incident is closed. Two weeks covers a fortnightly review and a
# post-incident writeup, and it is the operator's to change.
#
# Deleting a report does not rewrite history — the *record* that an
# investigation happened is cheap and stays; the rendered PDF, JSON and
# Markdown are what get pruned. An operator following a stale link gets
# "expired", not a lie about the investigation never having existed.
DEFAULT_RETENTION_DAYS = 14

# Serialises the read-modify-write of the history index within this process.
# Across processes the atomic replace prevents a torn file, but a concurrent
# writer can still lose an entry — which is one of the reasons the Postgres
# backend exists.
_HISTORY_LOCK = threading.Lock()


def _parse_timestamp(value: str) -> datetime | None:
    """History timestamps, tolerantly. An unparseable one is never pruned."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class FilesystemReportStore:
    """Reports on local disk, history in a JSON index. The original behaviour."""

    distributed = False

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or Path("data") / "investigations"
        self.reports_dir = self.data_dir / "reports"
        self.index_path = self.data_dir / "history.json"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def ensure(self, investigation_id: str, owner: str = "") -> None:
        """Nothing to reserve on a filesystem."""

    def prune(self, older_than_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete report files past retention. Returns how many were removed.

        Index entries for pruned investigations gain `expired: true` rather
        than disappearing, so the console can say "this report has expired"
        instead of behaving as though the investigation never ran.
        """
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        removed = 0

        with _HISTORY_LOCK:
            entries = self._read_index()
            changed = False

            for entry in entries:
                if entry.get("expired"):
                    continue
                stamp = _parse_timestamp(entry.get("timestamp", ""))
                if stamp is None or stamp >= cutoff:
                    continue

                for report_format in EXTENSIONS:
                    path = self.reports_dir / (f"{entry.get('id')}.{EXTENSIONS[report_format]}")
                    if path.exists():
                        path.unlink(missing_ok=True)
                        removed += 1

                entry["expired"] = True
                changed = True

            if changed:
                self._write_atomic(
                    self.index_path,
                    json.dumps(entries[:HISTORY_LIMIT], indent=2).encode("utf-8"),
                )

        if removed:
            logger.info(
                "Pruned {count} report file(s) older than {days} days",
                count=removed,
                days=older_than_days,
            )
        return removed

    def write(self, investigation_id: str, report_format: str, content: bytes) -> None:
        path = self.reports_dir / f"{investigation_id}.{EXTENSIONS[report_format]}"
        self._write_atomic(path, content)

    def read(self, investigation_id: str, report_format: str) -> bytes | None:
        path = self.path(investigation_id, report_format)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            logger.warning("Could not read report {id}", id=investigation_id)
            return None

    def path(self, investigation_id: str, report_format: str = "pdf") -> Path | None:
        """The on-disk location, or None. Filesystem backend only."""
        extension = EXTENSIONS.get(report_format)
        if extension is None:
            return None
        if not SAFE_ID.match(investigation_id or ""):
            logger.warning(
                "Rejecting malformed investigation id: {id}", id=str(investigation_id)[:80]
            )
            return None

        path = (self.reports_dir / f"{investigation_id}.{extension}").resolve()

        # Defence in depth: even with a validated id, never serve a path that
        # resolves outside the reports directory.
        if not path.is_relative_to(self.reports_dir.resolve()):
            logger.error("Path traversal attempt blocked: {id}", id=investigation_id)
            return None

        if not path.exists():
            return None
        return path

    def upsert_index(self, item: dict[str, Any]) -> None:
        with _HISTORY_LOCK:
            history = [entry for entry in self._read_index() if entry.get("id") != item["id"]]
            history.insert(0, item)
            self._write_atomic(
                self.index_path,
                json.dumps(history[:HISTORY_LIMIT], indent=2).encode("utf-8"),
            )

    def read_index(
        self,
        owner: str | None = None,
        limit: int = HISTORY_LIMIT,
    ) -> list[dict[str, Any]]:
        entries = self._read_index()
        if owner is not None:
            entries = [entry for entry in entries if entry.get("owner", "") == owner]
        return entries[:limit]

    def find(self, investigation_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self._read_index() if item.get("id") == investigation_id),
            None,
        )

    def _read_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []

        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Never silently discard history: move it aside so it can be
            # recovered, and make the event loud.
            quarantine = self.index_path.with_suffix(
                f".corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
            )
            try:
                self.index_path.rename(quarantine)
                logger.error("History index was unreadable; quarantined to {path}", path=quarantine)
            except OSError:
                logger.error("History index was unreadable and could not be quarantined")
            return []

        return data if isinstance(data, list) else []

    def _write_atomic(self, path: Path, content: bytes) -> None:
        """Write via a temp file and atomic rename.

        A crash or a full disk part-way through leaves the previous file intact
        rather than a truncated one. The previous implementation wrote in place,
        so an interrupted write corrupted the history index.
        """
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise


class PostgresReportStore:
    """Reports as blobs, history as a column on the investigation row.

    The history item is stored whole rather than re-derived from columns: the
    API returns that dict verbatim, so keeping it intact means a query change
    cannot drift the response shape.
    """

    distributed = True

    def __init__(self, database) -> None:
        self._db = database

    def ensure(self, investigation_id: str, owner: str = "") -> None:
        """Make sure the investigation row exists before anything references it.

        The synchronous `/investigate` endpoint saves a report without ever
        creating a job, so the row it hangs off may not exist yet.
        """
        with self._db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO investigations (id, owner, status) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (investigation_id, owner, "succeeded"),
            )

    def prune(self, older_than_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete report content past retention. Returns how many blobs went.

        Three statements, one transaction, and the second is the one that was
        missing.

        **`investigations.result` is pruned too, and it is the larger copy.**
        Retention deleted the rendered PDF/JSON/Markdown and left the JSON
        payload they were rendered *from* — measured at 2.7 MB against ~200 KB
        of blobs — so `/investigations/{id}/pdf` 404'd on an expired
        investigation while `GET /investigations/{id}` still served its whole
        contents. The same data, deleted on one path and retained on another,
        under a setting an operator reasonably reads as a deletion schedule.
        `docs/DATA_PROTECTION.md` recorded it as a gap and offered a hand-run
        `UPDATE` to close it; the gap was that this method did not run it.

        Nulled rather than deleted, and the row still survives: an investigation
        that happened must not come to look like one that never did. That is
        the same distinction the history projection makes by being marked
        `expired` rather than removed — done here in the same transaction, so a
        listing cannot offer a download that is no longer there.
        """
        with self._db.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM investigation_reports
                 WHERE investigation_id IN (
                       SELECT id FROM investigations
                        WHERE created_at < now() - %s::interval
                 )
                """,
                (f"{older_than_days} days",),
            )
            removed = cursor.rowcount

            cursor.execute(
                """
                UPDATE investigations
                   SET result = NULL
                 WHERE created_at < now() - %s::interval
                   AND result IS NOT NULL
                """,
                (f"{older_than_days} days",),
            )
            payloads = cursor.rowcount

            cursor.execute(
                """
                UPDATE investigations
                   SET history_item = jsonb_set(history_item, '{expired}', 'true'::jsonb)
                 WHERE created_at < now() - %s::interval
                   AND history_item IS NOT NULL
                   AND COALESCE(history_item->>'expired', 'false') <> 'true'
                """,
                (f"{older_than_days} days",),
            )

        if removed or payloads:
            logger.info(
                "Pruned {count} report blob(s) and {payloads} stored payload(s) "
                "older than {days} days",
                count=removed,
                payloads=payloads,
                days=older_than_days,
            )
        return removed

    def write(self, investigation_id: str, report_format: str, content: bytes) -> None:
        with self._db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO investigation_reports (investigation_id, format, content) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (investigation_id, format) "
                "DO UPDATE SET content = EXCLUDED.content, created_at = now()",
                (investigation_id, report_format, content),
            )

    def read(self, investigation_id: str, report_format: str) -> bytes | None:
        if report_format not in EXTENSIONS or not SAFE_ID.match(investigation_id or ""):
            logger.warning(
                "Rejecting malformed report request: {id}", id=str(investigation_id)[:80]
            )
            return None
        with self._db.cursor() as cursor:
            cursor.execute(
                "SELECT content FROM investigation_reports "
                "WHERE investigation_id = %s AND format = %s",
                (investigation_id, report_format),
            )
            row = cursor.fetchone()
        return bytes(row[0]) if row else None

    def path(self, investigation_id: str, report_format: str = "pdf") -> Path | None:
        """There is no path. Callers must use `read()`."""
        return None

    def upsert_index(self, item: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO investigations (id, owner, status, history_item) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET history_item = EXCLUDED.history_item",
                (item["id"], item.get("owner", ""), "succeeded", Jsonb(item)),
            )

    def read_index(
        self,
        owner: str | None = None,
        limit: int = HISTORY_LIMIT,
    ) -> list[dict[str, Any]]:
        clause = "" if owner is None else "AND owner = %s"
        params: tuple = () if owner is None else (owner,)
        with self._db.cursor() as cursor:
            cursor.execute(
                f"SELECT history_item FROM investigations "
                f"WHERE history_item IS NOT NULL {clause} "
                f"ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            return [row[0] for row in cursor.fetchall()]

    def find(self, investigation_id: str) -> dict[str, Any] | None:
        with self._db.cursor() as cursor:
            cursor.execute(
                "SELECT history_item FROM investigations WHERE id = %s",
                (investigation_id,),
            )
            row = cursor.fetchone()
        return row[0] if row and row[0] else None


_default_store: PostgresReportStore | None = None


def get_report_store():
    """The report store for this deployment.

    A fresh `FilesystemReportStore` per call, not a cached one: its paths are
    relative to the working directory, and resolving them at call time is what
    the existing behaviour — and the tests that chdir into a temp directory —
    depend on. The Postgres store holds a connection pool, so startup installs
    that one once.
    """
    return _default_store if _default_store is not None else FilesystemReportStore()


def set_report_store(store) -> None:
    """Install the process-wide report store; `None` reverts to the filesystem."""
    global _default_store
    _default_store = store
