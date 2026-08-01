"""Report retention: old artefacts go, the record that they existed stays."""

import json
from datetime import UTC, datetime, timedelta

from app.services.report_store import DEFAULT_RETENTION_DAYS, FilesystemReportStore


def store(tmp_path) -> FilesystemReportStore:
    return FilesystemReportStore(tmp_path / "investigations")


def add(target: FilesystemReportStore, identifier: str, age_days: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    for report_format in ("pdf", "json", "markdown"):
        target.write(identifier, report_format, b"content")
    target.upsert_index({"id": identifier, "timestamp": stamp, "owner": ""})


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
