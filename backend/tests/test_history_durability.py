"""Durability and path-safety of the investigation history.

Two findings from the 2026-07-26 review:

- **F20** the index was written in place after a read-modify-write, so a crash
  or full disk truncated it and a parse failure silently discarded every past
  investigation.
- **F16** investigation ids were interpolated into filesystem paths unchecked.
  Not reachable over HTTP (Starlette rejects the traversal), but a defence-in-
  depth failure that would become live behind a different proxy or a non-HTTP
  caller.
"""

import json
import threading
from pathlib import Path

import pytest

from app.services.history_service import InvestigationHistoryService

DIAGNOSIS = {"root_cause": "x", "confidence": 50}
INVESTIGATION = {"context": "test", "severity": {"severity": "High"}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return InvestigationHistoryService()


class TestPathSafety:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../../etc/passwd",
            "a/../../b",
            "..",
            "/etc/passwd",
            "report;rm -rf /",
            "id with spaces",
            "",
        ],
    )
    def test_malformed_ids_are_rejected(self, service, hostile):
        assert service.report_path(hostile, "json") is None
        assert service.read_report(hostile) is None

    def test_a_valid_uuid_still_resolves(self, service):
        item = service.save(DIAGNOSIS, INVESTIGATION)
        assert service.report_path(item["id"], "json") is not None

    def test_resolved_path_stays_inside_the_reports_directory(self, service):
        item = service.save(DIAGNOSIS, INVESTIGATION)
        path = service.report_path(item["id"], "pdf")

        assert path.resolve().is_relative_to(service.reports_dir.resolve())

    def test_traversal_cannot_read_a_planted_file(self, service):
        planted = service.data_dir / "secret.json"
        planted.write_text('{"diagnosis": {"root_cause": "LEAKED"}}')

        assert service.read_report("../secret") is None


class TestDurability:
    def test_index_write_is_atomic(self, service, monkeypatch):
        """A failure mid-write must leave the previous index intact."""
        service.save(DIAGNOSIS, INVESTIGATION)
        before = service.index_path.read_text()

        real_replace = __import__("os").replace

        def exploding_replace(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("os.replace", exploding_replace)
        with pytest.raises(OSError):
            service.save(DIAGNOSIS, INVESTIGATION)

        monkeypatch.setattr("os.replace", real_replace)
        assert service.index_path.read_text() == before
        assert json.loads(service.index_path.read_text())

    def test_no_temp_files_are_left_behind(self, service, monkeypatch):
        def exploding_replace(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("os.replace", exploding_replace)
        with pytest.raises(OSError):
            service.save(DIAGNOSIS, INVESTIGATION)

        assert list(Path(service.data_dir).glob("*.tmp")) == []

    def test_corrupt_index_is_quarantined_not_discarded(self, service):
        service.save(DIAGNOSIS, INVESTIGATION)
        service.index_path.write_text("{ this is not json")

        assert service.list_history() == []

        quarantined = list(service.data_dir.glob("history.corrupt-*.json"))
        assert len(quarantined) == 1, "corrupt history must be recoverable"
        assert "not json" in quarantined[0].read_text()

    def test_concurrent_saves_do_not_lose_entries(self, service):
        """The in-process lock must serialise the read-modify-write."""
        errors: list[Exception] = []

        def save():
            try:
                service.save(DIAGNOSIS, INVESTIGATION)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=save) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        history = service.list_history()
        assert len(history) == 12, f"lost entries: expected 12, got {len(history)}"
        assert len({entry["id"] for entry in history}) == 12

    def test_index_is_never_partially_written(self, service):
        for _ in range(5):
            service.save(DIAGNOSIS, INVESTIGATION)
            json.loads(service.index_path.read_text())


class TestClusterAttribution:
    """A history entry names the cluster it ran against.

    Without it an investigation can only be attributed to a cluster by joining
    against the job store, which in the single-process deployment does not
    survive a restart while history does — so a fleet view built on that join
    would quietly omit every run from before the last restart.
    """

    def test_the_context_is_recorded(self, service):
        item = service.save(DIAGNOSIS, {**INVESTIGATION, "context": "prod-eu-west"})
        assert item["context"] == "prod-eu-west"
        assert service.list_history()[0]["context"] == "prod-eu-west"

    def test_an_absent_context_is_empty_not_missing(self, service):
        item = service.save(DIAGNOSIS, {"severity": {"severity": "High"}})
        assert item["context"] == ""

    def test_regenerating_keeps_the_cluster(self, service):
        item = service.save(DIAGNOSIS, {**INVESTIGATION, "context": "prod-eu-west"})
        service.regenerate(item["id"])
        assert service.list_history()[0]["context"] == "prod-eu-west"


class TestHistorySeverityAgreesWithTheReport:
    """One investigation must not carry two severities.

    The history entry and the report body are derived separately, and they
    disagreed: the body said Healthy while the entry said Critical, because
    this class patched the upstream bug on its way out.
    """

    def test_an_unreadable_cluster_is_not_escalated_to_critical(self, service):
        unreadable = {
            "context": "prod",
            "severity": {"severity": "Unknown"},
            "health": {"status": "error", "message": "Kubernetes investigation failed."},
        }
        assert service._report_severity(unreadable) == "Unknown"

    def test_a_real_finding_is_still_reported_as_found(self, service):
        investigation = {"severity": {"severity": "Critical"}, "health": {"status": "error"}}
        assert service._report_severity(investigation) == "Critical"

    def test_a_legacy_report_claiming_health_after_a_failure_is_corrected(self, service):
        # Written before severity accounted for failed collectors.
        legacy = {"severity": {"severity": "Healthy"}, "health": {"status": "error"}}
        assert service._report_severity(legacy) == "Unknown"

    def test_a_genuinely_healthy_cluster_stays_healthy(self, service):
        healthy = {"severity": {"severity": "Healthy"}, "health": {"status": "healthy"}}
        assert service._report_severity(healthy) == "Healthy"
