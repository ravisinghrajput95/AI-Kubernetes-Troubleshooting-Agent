import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from app.reports.rendering import ReportRenderer
from app.services.report_store import get_report_store


class InvestigationHistoryService:
    """Records investigation reports, and keeps the history index.

    Rendering moved to `app.reports.rendering.ReportRenderer`. This file was
    998 lines doing three jobs — compose, render, index — and the dependency
    between them only ever ran one way, so the seam was already there to take.

    Backend-agnostic, and that has not changed: bytes are produced and handed
    to a `ReportStore`, which decides whether they land on local disk or in
    Postgres. That is what lets `/investigations/{id}/pdf` answer on a worker
    that never rendered the file.
    """

    def __init__(self, store=None) -> None:
        self._store = store if store is not None else get_report_store()
        # The index shares the renderer's derivations rather than repeating
        # them: a row's severity and the PDF's severity are the same fact about
        # the same investigation, and two implementations would drift.
        self._renderer = ReportRenderer()

    # Filesystem details, exposed for the single-process deployment and its
    # path-safety tests. Absent — and unused — on the Postgres backend.
    @property
    def data_dir(self) -> Path:
        return self._store.data_dir

    @property
    def reports_dir(self) -> Path:
        return self._store.reports_dir

    @property
    def index_path(self) -> Path:
        return self._store.index_path

    def save(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        status: str = "success",
        investigation_id: str | None = None,
        owner: str = "",
    ) -> dict[str, Any]:
        # Callers that already published an id (the job API) pin the report to it.
        investigation_id = investigation_id or str(uuid4())
        timestamp = datetime.now(UTC).isoformat()
        incident_id = self._renderer.incident_id(timestamp, investigation_id)
        namespace = self._namespace(investigation)
        confidence = int(diagnosis.get("confidence", 0))
        root_cause = diagnosis.get("root_cause", "Unknown root cause")

        # Reserve the record first: on the Postgres backend the report blobs
        # hang off the investigation row, and the synchronous endpoint saves a
        # report without ever having created a job.
        self._store.ensure(investigation_id, owner)
        self._render_all(
            investigation_id, diagnosis, investigation, timestamp, namespace, status, incident_id
        )

        item = {
            "id": investigation_id,
            "owner": owner,
            # The cluster this ran against. Without it the console can only
            # attribute an investigation to a cluster by joining against the
            # job store, which does not survive a restart in the single-process
            # deployment — so a fleet view would silently omit older runs.
            "context": str(investigation.get("context") or ""),
            "incident_id": incident_id,
            "timestamp": timestamp,
            "root_cause": root_cause,
            "namespace": namespace,
            "confidence": confidence,
            "status": status,
            "severity": self._renderer.severity(investigation),
            "incident_status": self._renderer.incident_status(investigation),
            "environment": self._renderer.environment(
                investigation.get("context")
                or investigation.get("topology", {}).get("cluster")
                or "Current Context"
            ),
            "pdf_url": f"/investigations/{investigation_id}/pdf",
            "json_url": f"/investigations/{investigation_id}/json",
            "markdown_url": f"/investigations/{investigation_id}/markdown",
        }

        self._store.upsert_index(item)
        logger.info("Saved investigation report {id}", id=investigation_id)
        return item

    def _render_all(
        self,
        investigation_id: str,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        timestamp: str,
        namespace: str,
        status: str,
        incident_id: str,
    ) -> None:
        """Render all three formats from one composition and store them."""
        args = (diagnosis, investigation, timestamp, namespace, status, incident_id)
        self._store.write(investigation_id, "pdf", self._renderer.render_pdf(*args))
        self._store.write(
            investigation_id, "json", self._renderer.render_json(*args).encode("utf-8")
        )
        self._store.write(
            investigation_id, "markdown", self._renderer.render_markdown(*args).encode("utf-8")
        )

    def list_history(self, owner: str | None = None) -> list[dict[str, Any]]:
        """History entries, optionally restricted to one owner.

        `owner=None` returns everything and is for internal callers only. API
        handlers must pass the caller's subject: one user's investigations are
        not another's to read.
        """
        return self._store.read_index(owner=owner)

    def report_path(self, investigation_id: str, report_type: str = "pdf") -> Path | None:
        """On-disk location of a report. Filesystem backend only; None otherwise.

        Kept for the single-process deployment and its path-safety tests. The
        API reads bytes via `read_report_bytes`, because a report rendered by
        another worker has no path here.
        """
        return self._store.path(investigation_id, report_type)

    def read_report_bytes(
        self,
        investigation_id: str,
        report_type: str,
        owner: str | None = None,
    ) -> bytes | None:
        """Raw report content, whichever worker rendered it."""
        if not self.owns(investigation_id, owner):
            return None
        return self._store.read(investigation_id, report_type)

    def owns(self, investigation_id: str, owner: str | None) -> bool:
        """True when `owner` may read this investigation.

        An unknown id returns True so the caller still 404s rather than 403s —
        a distinct response would confirm the id exists.
        """
        if owner is None:
            return True
        entry = self._store.find(investigation_id)
        if entry is None:
            return True
        return entry.get("owner", "") == owner

    def read_report(
        self,
        investigation_id: str,
        owner: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.owns(investigation_id, owner):
            return None

        raw = self._store.read(investigation_id, "json")
        if raw is None:
            return None

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Investigation report JSON is invalid: {id}", id=investigation_id)
            return None

        return data if isinstance(data, dict) else None

    def regenerate(self, investigation_id: str) -> dict[str, Any] | None:
        report = self.read_report(investigation_id)
        if report is None:
            return None

        diagnosis = report.get("diagnosis", {})
        investigation = report.get("investigation", {})
        if not isinstance(diagnosis, dict) or not isinstance(investigation, dict):
            return None

        timestamp = str(report.get("timestamp") or datetime.now(UTC).isoformat())
        status = str(report.get("status") or "success")
        namespace = str(report.get("namespace") or self._namespace(investigation))
        incident_id = str(
            report.get("incident_id") or self._renderer.incident_id(timestamp, investigation_id)
        )

        self._render_all(
            investigation_id, diagnosis, investigation, timestamp, namespace, status, incident_id
        )
        self._upsert_history_item(
            investigation_id,
            incident_id,
            timestamp,
            status,
            namespace,
            diagnosis,
            investigation,
        )
        logger.info("Regenerated investigation report {id}", id=investigation_id)
        return self.read_report(investigation_id)

    def _upsert_history_item(
        self,
        investigation_id: str,
        incident_id: str,
        timestamp: str,
        status: str,
        namespace: str,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
    ) -> None:
        cluster = (
            investigation.get("context")
            or investigation.get("topology", {}).get("cluster")
            or "Current Context"
        )
        existing = self._store.find(investigation_id) or {}
        item = {
            "id": investigation_id,
            # Carried across, not recomputed: regenerating a report must not
            # silently orphan it from the user who owns it.
            "owner": existing.get("owner", ""),
            "context": str(investigation.get("context") or existing.get("context") or ""),
            "incident_id": incident_id,
            "timestamp": timestamp,
            "root_cause": diagnosis.get("root_cause", "Unknown root cause"),
            "namespace": namespace,
            "confidence": int(diagnosis.get("confidence", 0)),
            "status": status,
            "severity": self._renderer.severity(investigation),
            "incident_status": self._renderer.incident_status(investigation),
            "environment": self._renderer.environment(cluster),
            "pdf_url": f"/investigations/{investigation_id}/pdf",
            "json_url": f"/investigations/{investigation_id}/json",
            "markdown_url": f"/investigations/{investigation_id}/markdown",
        }
        self._store.upsert_index(item)

    def _namespace(self, investigation: dict[str, Any]) -> str:
        pods = investigation.get("pods", {}).get("problematic_pods", [])
        if pods:
            return pods[0].get("namespace", "unknown")

        deployments = investigation.get("deployments", {}).get("unhealthy_deployments", [])
        if deployments:
            return deployments[0].get("namespace", "unknown")

        network = investigation.get("network", {}).get("findings", [])
        if network:
            return network[0].get("namespace", "unknown")

        return "unknown"
