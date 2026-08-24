"""Report retention: old artefacts go, the record that they existed stays."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services.report_store import DEFAULT_RETENTION_DAYS, FilesystemReportStore
from tests.distributed_backend import DistributedBackend, requires_backend


def store(tmp_path) -> FilesystemReportStore:
    return FilesystemReportStore(tmp_path / "investigations")


def add(target: FilesystemReportStore, identifier: str, age_days: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    for report_format in ("pdf", "json", "markdown"):
        target.write(identifier, report_format, b"content")
    target.upsert_index({"id": identifier, "timestamp": stamp, "owner": ""})


@pytest.fixture
async def backend():
    target = DistributedBackend(with_bus=False)
    try:
        yield target
    finally:
        await target.close()


class TestRetention:
    def test_reports_past_retention_are_deleted(self, tmp_path):
        target = store(tmp_path)
        add(target, "11111111-1111-1111-1111-111111111111", age_days=20)

        assert target.prune() == 3
        assert target.read("11111111-1111-1111-1111-111111111111", "pdf") is None

    def test_recent_reports_are_kept(self, tmp_path):
        target = store(tmp_path)
        add(target, "22222222-2222-2222-2222-222222222222", age_days=1)

        assert target.prune() == 0
        assert target.read("22222222-2222-2222-2222-222222222222", "pdf") == b"content"

    def test_the_boundary_is_the_configured_window(self, tmp_path):
        target = store(tmp_path)
        add(target, "33333333-3333-3333-3333-333333333333", age_days=DEFAULT_RETENTION_DAYS - 0.5)
        add(target, "44444444-4444-4444-4444-444444444444", age_days=DEFAULT_RETENTION_DAYS + 0.5)

        assert target.prune() == 3
        assert target.read("33333333-3333-3333-3333-333333333333", "pdf") == b"content"
        assert target.read("44444444-4444-4444-4444-444444444444", "pdf") is None

    def test_the_history_entry_survives_and_says_it_expired(self, tmp_path):
        """Retention is not amnesia.

        Deleting the record too would make an investigation that happened look
        like one that never did. The entry stays and is marked, so the console
        can say "expired" rather than 404 on a link an operator still holds.
        """
        target = store(tmp_path)
        add(target, "55555555-5555-5555-5555-555555555555", age_days=30)
        target.prune()

        entry = target.find("55555555-5555-5555-5555-555555555555")
        assert entry is not None
        assert entry["expired"] is True

    def test_pruning_twice_is_idempotent(self, tmp_path):
        target = store(tmp_path)
        add(target, "66666666-6666-6666-6666-666666666666", age_days=30)

        assert target.prune() == 3
        assert target.prune() == 0

    def test_an_unparseable_timestamp_is_never_pruned(self, tmp_path):
        """A report whose age cannot be established is kept, not guessed at."""
        target = store(tmp_path)
        target.write("77777777-7777-7777-7777-777777777777", "pdf", b"content")
        target.upsert_index({"id": "77777777-7777-7777-7777-777777777777", "timestamp": "soon"})

        assert target.prune() == 0
        assert target.read("77777777-7777-7777-7777-777777777777", "pdf") == b"content"

    def test_retention_is_configurable(self, tmp_path):
        target = store(tmp_path)
        add(target, "88888888-8888-8888-8888-888888888888", age_days=3)

        assert target.prune(older_than_days=30) == 0
        assert target.prune(older_than_days=2) == 3

    def test_the_index_stays_valid_json(self, tmp_path):
        target = store(tmp_path)
        add(target, "99999999-9999-9999-9999-999999999999", age_days=30)
        target.prune()

        assert isinstance(json.loads(target.index_path.read_text()), list)


@requires_backend
class TestTheStoredPayloadGoesToo:
    """Retention must not delete the small copy and keep the large one.

    `investigations.result` is the JSON the PDF, Markdown and JSON reports are
    rendered *from* — measured at 2.7 MB at the `MAX_LIST_ITEMS` ceiling against
    a couple of hundred kilobytes of rendered blobs. `prune()` deleted the
    blobs and left it, so an expired investigation 404'd on
    `/investigations/{id}/pdf` while `GET /investigations/{id}` still served its
    entire contents from the row.

    Postgres-only because there is no equivalent on the filesystem backend: the
    JSON report *is* the stored payload there, and it is already deleted. The
    shared property both backends must satisfy is the last test in this class —
    after retention there is no path left to the content, and the record that
    the investigation happened survives.
    """

    IDENTIFIER = "5c0de1ab-0000-4000-8000-00000000fee1"

    def _aged(self, backend, age_days: float, result: dict | None = None):
        store = backend.reports()
        store.ensure(self.IDENTIFIER, "alice")
        store.write(self.IDENTIFIER, "pdf", b"%PDF-1.4 fake")
        store.upsert_index({"id": self.IDENTIFIER, "owner": "alice", "timestamp": "2026-01-01"})
        with backend.database.cursor() as cursor:
            cursor.execute(
                "UPDATE investigations SET result = %s, created_at = now() - %s::interval "
                "WHERE id = %s",
                (
                    json.dumps(result or {"investigation": {"pods": ["one"] * 50}}),
                    f"{age_days} days",
                    self.IDENTIFIER,
                ),
            )
        return store

    def _result(self, backend):
        with backend.database.cursor() as cursor:
            cursor.execute("SELECT result FROM investigations WHERE id = %s", (self.IDENTIFIER,))
            return cursor.fetchone()[0]

    async def test_the_stored_payload_is_nulled_past_retention(self, backend):
        store = self._aged(backend, age_days=DEFAULT_RETENTION_DAYS + 1)

        store.prune()

        assert self._result(backend) is None

    async def test_a_recent_payload_is_untouched(self, backend):
        store = self._aged(backend, age_days=1)

        store.prune()

        assert self._result(backend) is not None

    async def test_the_row_survives_so_the_investigation_still_happened(self, backend):
        """Nulled, never deleted — same decision the history entry makes."""
        store = self._aged(backend, age_days=DEFAULT_RETENTION_DAYS + 1)

        store.prune()

        with backend.database.cursor() as cursor:
            cursor.execute("SELECT id, owner FROM investigations WHERE id = %s", (self.IDENTIFIER,))
            row = cursor.fetchone()
        assert row is not None and row[1] == "alice"

    async def test_pruning_twice_is_idempotent(self, backend):
        store = self._aged(backend, age_days=DEFAULT_RETENTION_DAYS + 1)

        store.prune()
        store.prune()

        assert self._result(backend) is None

    async def test_no_path_to_the_content_survives(self, backend):
        """The property both backends share, and the reason this test exists.

        Asserted over every way the content can be read rather than over the
        column, so a future path to it — a new endpoint, a new projection — is
        caught by this test rather than by a customer.
        """
        store = self._aged(backend, age_days=DEFAULT_RETENTION_DAYS + 1)

        store.prune()

        assert store.read(self.IDENTIFIER, "pdf") is None
        assert store.read(self.IDENTIFIER, "json") is None
        assert store.read(self.IDENTIFIER, "markdown") is None
        assert self._result(backend) is None
        entry = next(
            item for item in store.read_index(owner="alice") if item["id"] == self.IDENTIFIER
        )
        assert entry["expired"] is True
