import json
from datetime import UTC, datetime
from pathlib import Path
from textwrap import wrap
from typing import Any
from uuid import uuid4

from loguru import logger

from app.reports.composer import IncidentReportComposer
from app.services.report_store import get_report_store


class InvestigationHistoryService:
    """Composes, renders and records investigation reports.

    Rendering is unchanged and backend-agnostic: this class produces bytes and
    hands them to a `ReportStore`, which decides whether they land on local
    disk or in Postgres. That is what lets `/investigations/{id}/pdf` answer on
    a worker that never rendered the file.
    """

    def __init__(self, store=None) -> None:
        self._store = store if store is not None else get_report_store()

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
        incident_id = self._incident_id(timestamp, investigation_id)
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
            "severity": self._report_severity(investigation),
            "incident_status": self._incident_status(investigation),
            "environment": self._environment(
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
        self._store.write(investigation_id, "pdf", self._render_pdf(*args))
        self._store.write(investigation_id, "json", self._render_json(*args).encode("utf-8"))
        self._store.write(
            investigation_id, "markdown", self._render_markdown(*args).encode("utf-8")
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
            report.get("incident_id") or self._incident_id(timestamp, investigation_id)
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
            "severity": self._report_severity(investigation),
            "incident_status": self._incident_status(investigation),
            "environment": self._environment(cluster),
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

    def _render_pdf(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        timestamp: str,
        namespace: str,
        status: str,
        incident_id: str,
    ) -> bytes:
        # Only the cover-page metadata is derived here; the body comes from the
        # composer. The per-section helpers this used to call became dead work
        # when the composer replaced the hardcoded sections.
        severity = self._report_severity(investigation)
        cluster = (
            investigation.get("context")
            or investigation.get("topology", {}).get("cluster")
            or "Current Context"
        )
        environment = self._environment(cluster)
        incident_status = self._incident_status(investigation)

        meta = [
            ("Incident", incident_id),
            ("Cluster", self._short_cluster(cluster)),
            ("Confidence", f"{diagnosis.get('confidence', 0)}%"),
            ("Severity", severity),
            ("Status", incident_status),
            ("Environment", environment),
        ]
        # Sections come from the shared composer, so the PDF, Markdown and
        # JSON reports present one composition rather than three.
        report = IncidentReportComposer().compose(
            diagnosis, investigation, incident_id, timestamp, namespace, status
        )
        sections = [
            {
                "title": section.title,
                "body": section.as_lines(),
                "monospace": section.title.startswith("Appendix"),
            }
            for section in report.sections
        ]

        return self._styled_pdf(
            title="AI Kubernetes Investigation Report",
            subtitle=f"Generated {timestamp}",
            meta=meta,
            sections=sections,
        )

    def _render_json(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        timestamp: str,
        namespace: str,
        status: str,
        incident_id: str,
    ) -> str:
        cluster = (
            investigation.get("context")
            or investigation.get("topology", {}).get("cluster")
            or "Current Context"
        )
        payload = {
            "incident_id": incident_id,
            "timestamp": timestamp,
            "status": status,
            "namespace": namespace,
            "report_metadata": {
                "cluster": cluster,
                "environment": self._environment(cluster),
                "severity": self._report_severity(investigation),
                "incident_status": self._incident_status(investigation),
                "business_impact": self._business_impact(investigation),
                "confidence_breakdown": [
                    {"source": label, "contribution": value}
                    for label, value in self._confidence_breakdown(diagnosis, investigation)
                ],
                "evidence_matrix": [
                    {"source": source, "status": state}
                    for source, state in self._evidence_matrix(investigation)
                ],
            },
            "diagnosis": diagnosis,
            "investigation": investigation,
            # The same composition the PDF and Markdown render, so a consumer of
            # the JSON sees the report rather than having to rebuild it.
            "report": IncidentReportComposer()
            .compose(diagnosis, investigation, incident_id, timestamp, namespace, status)
            .to_dict(),
        }
        return json.dumps(payload, indent=2)

    def _render_markdown(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        timestamp: str,
        namespace: str,
        status: str,
        incident_id: str,
    ) -> str:
        """Render the composed report as Markdown.

        Shares the composition with the PDF, so the two cannot describe the
        same incident differently.
        """
        report = IncidentReportComposer().compose(
            diagnosis, investigation, incident_id, timestamp, namespace, status
        )

        parts = [f"# {report.title}", "", f"_Incident {report.incident_id}_", ""]

        for section in report.sections:
            parts.append(f"## {section.title}")
            parts.append("")

            if section.fields:
                parts.append("| Field | Value |")
                parts.append("| --- | --- |")
                parts.extend(
                    f"| {field.label} | {self._md_escape(field.value)} |"
                    for field in section.fields
                )
                parts.append("")

            if section.body:
                parts.extend(self._md_line(line) for line in section.body)
                parts.append("")

            if section.table:
                width = max([len(row) for row in section.table] + [len(section.headers)])
                headers = [
                    *section.headers,
                    *([""] * (width - len(section.headers))),
                ]
                parts.append("| " + " | ".join(headers) + " |")
                parts.append("| " + " | ".join(["---"] * width) + " |")
                for row in section.table:
                    padded = [*row, *([""] * (width - len(row)))]
                    parts.append("| " + " | ".join(self._md_escape(cell) for cell in padded) + " |")
                parts.append("")

            if section.note:
                parts.append(f"> {section.note}")
                parts.append("")

        return "\n".join(parts).rstrip() + "\n"

    def _md_line(self, line: str) -> str:
        """Preserve command lines as code, leave prose as prose."""
        stripped = line.strip()
        if stripped.startswith("$ ") or stripped.startswith("kubectl "):
            return f"    {stripped}"
        return line

    def _md_escape(self, value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    def _incident_id(self, timestamp: str, investigation_id: str) -> str:
        date_part = timestamp[:10].replace("-", "")
        return f"INC-{date_part}-{investigation_id[:8].upper()}"

    def _environment(self, cluster: str) -> str:
        lowered = cluster.lower()
        if "docker-desktop" in lowered or "minikube" in lowered or "kind" in lowered:
            return "Development"
        if "prod" in lowered or "production" in lowered:
            return "Production"
        if "stage" in lowered or "staging" in lowered:
            return "Staging"
        if "dev" in lowered:
            return "Development"
        return "Unknown"

    def _short_cluster(self, cluster: str) -> str:
        if "/" in cluster:
            return cluster.rstrip("/").split("/")[-1]
        return cluster

    def _incident_status(self, investigation: dict[str, Any]) -> str:
        health = investigation.get("health", {}).get("status", "")
        if health in {"error", "issues_found"}:
            return "Open"
        return "Resolved" if health == "healthy" else "Open"

    def _report_severity(self, investigation: dict[str, Any]) -> str:
        if investigation.get("health", {}).get("status") == "error":
            return "Critical"
        severity = investigation.get("severity", {}).get("severity", "Not assessed")
        return (
            "Critical"
            if severity == "Healthy" and self._has_failed_evidence(investigation)
            else severity
        )

    def _business_impact(self, investigation: dict[str, Any]) -> list[str]:
        if investigation.get("health", {}).get("status") == "healthy":
            return ["No active business impact detected."]

        impact = []
        if self._has_failed_evidence(investigation):
            impact.extend(
                [
                    "Unable to retrieve one or more Kubernetes resource groups.",
                    "Monitoring and troubleshooting visibility are degraded.",
                    "New deployments may be blocked until cluster access is restored.",
                ]
            )

        if investigation.get("pods", {}).get("problematic_pods"):
            impact.append("Affected workloads may be unavailable or unstable.")
        if investigation.get("network", {}).get("findings"):
            impact.append("Service routing or in-cluster connectivity may be impacted.")
        if investigation.get("storage", {}).get("findings"):
            impact.append("Persistent workloads may be blocked by storage issues.")
        if investigation.get("nodes", {}).get("findings"):
            impact.append("Node health issues may reduce available capacity.")

        return impact or ["Operational impact requires SRE review based on collected evidence."]

    def _confidence_breakdown(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
    ) -> list[tuple[str, int]]:
        if self._api_connectivity_issue(investigation):
            return [
                ("Pod Analysis", 0),
                ("Event Analysis", 0),
                ("Node Analysis", 25),
                ("Network Analysis", 35),
                ("API Connectivity", 40),
            ]

        signals = [
            ("Pod Analysis", self._signal_score(investigation.get("pods", {}), 25)),
            ("Event Analysis", self._signal_score(investigation.get("events", {}), 20)),
            ("Logs Analysis", 20 if investigation.get("logs", {}).get("logs") else 5),
            ("Deployment Analysis", self._signal_score(investigation.get("deployments", {}), 20)),
            ("Network Analysis", self._signal_score(investigation.get("network", {}), 15)),
        ]
        total = sum(value for _, value in signals)
        if total == 0:
            return [("Evidence Available", int(diagnosis.get("confidence", 0)))]
        return signals

    def _signal_score(self, section: dict[str, Any], weight: int) -> int:
        if section.get("error"):
            return 0
        if (
            section.get("findings")
            or section.get("problematic_pods")
            or section.get("unhealthy_deployments")
        ):
            return weight
        if section.get("healthy") is True:
            return max(5, weight // 3)
        return 0

    def _evidence_matrix(self, investigation: dict[str, Any]) -> list[tuple[str, str]]:
        rows = [
            ("Pods", self._evidence_status(investigation.get("pods", {}))),
            ("Events", self._evidence_status(investigation.get("events", {}))),
            ("Nodes", self._evidence_status(investigation.get("nodes", {}))),
            ("Deployments", self._evidence_status(investigation.get("deployments", {}))),
            ("Services", self._evidence_status(investigation.get("network", {}))),
            ("Storage", self._evidence_status(investigation.get("storage", {}))),
            ("Extended Workloads", self._evidence_status(investigation.get("workloads", {}))),
            ("API Connectivity", self._api_status(investigation)),
            (
                "Port 6443",
                "Closed" if self._api_connection_refused(investigation) else "Unverified",
            ),
        ]
        return rows

    def _evidence_status(self, section: dict[str, Any]) -> str:
        if section.get("error"):
            return "Failed"
        if (
            section.get("findings")
            or section.get("problematic_pods")
            or section.get("unhealthy_deployments")
        ):
            return "Findings"
        if section.get("healthy") is True:
            return "Passed"
        return "Not Available"

    def _topology_lines(self, investigation: dict[str, Any]) -> list[str]:
        topology = investigation.get("topology", {})
        cluster = topology.get("cluster") or investigation.get("context") or "Current Context"
        nodes = topology.get("nodes", [])
        if not nodes:
            return [
                f"Cluster: {cluster}",
                "Node",
                "  `-- Unavailable",
                "Service",
                "  `-- Deployment",
                "      `-- Pods",
            ]

        lines = [f"Cluster: {cluster}", "Nodes"]
        for node in nodes[:8]:
            lines.append(f"  |-- {node.get('name', 'unknown')} ({node.get('pod_count', 0)} pods)")
            namespaces = sorted({pod.get("namespace", "default") for pod in node.get("pods", [])})
            for namespace in namespaces[:5]:
                lines.append(f"  |   `-- namespace/{namespace}")
        lines.extend(["Service", "  `-- Deployment", "      `-- Pods"])
        return lines

    def _timeline_lines(self, investigation: dict[str, Any]) -> list[str]:
        timeline = investigation.get("timeline", [])
        return [
            f"{item.get('time', '--:--:--')} {item.get('message', 'Unknown step')}"
            for item in timeline
        ] or ["No investigation timeline captured."]

    def _has_failed_evidence(self, investigation: dict[str, Any]) -> bool:
        return any(
            investigation.get(key, {}).get("error")
            for key in ("pods", "events", "deployments", "network", "nodes", "storage", "workloads")
        )

    def _api_connectivity_issue(self, investigation: dict[str, Any]) -> bool:
        text = self._combined_error_text(investigation)
        return any(
            phrase in text
            for phrase in (
                "connection refused",
                "unable to connect",
                "couldn't get current server api group list",
                "invalidclienttokenid",
                "api?timeout",
                "port 6443",
            )
        )

    def _api_connection_refused(self, investigation: dict[str, Any]) -> bool:
        text = self._combined_error_text(investigation)
        return "connection refused" in text or "port 6443" in text

    def _api_status(self, investigation: dict[str, Any]) -> str:
        text = self._combined_error_text(investigation)
        if "invalidclienttokenid" in text or "getting credentials" in text:
            return "Authentication Failed"
        if self._api_connection_refused(investigation):
            return "Refused"
        if "unable to connect" in text or "couldn't get current server api group list" in text:
            return "Unavailable"
        return "Available"

    def _combined_error_text(self, investigation: dict[str, Any]) -> str:
        text = " ".join(
            str(investigation.get(key, {}).get("error", ""))
            for key in ("pods", "events", "deployments", "network", "nodes", "storage", "workloads")
        ).lower()
        return text

    def _summary(self, investigation: dict[str, Any]) -> dict[str, Any]:
        return {
            "metrics": investigation.get("metrics", {}),
            "security": investigation.get("security", {}),
            "topology": investigation.get("topology", {}),
            "pods": investigation.get("pods", {}),
            "events": investigation.get("events", {}),
            "deployments": investigation.get("deployments", {}),
            "network": investigation.get("network", {}),
            "nodes": investigation.get("nodes", {}),
            "storage": investigation.get("storage", {}),
            "workloads": investigation.get("workloads", {}),
        }

    def _markdown_list(self, values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) or "- None recorded."

    def _styled_pdf(
        self,
        title: str,
        subtitle: str,
        meta: list[tuple[str, str]],
        sections: list[dict[str, Any]],
    ) -> bytes:
        pages: list[list[str]] = []
        page: list[str] = []
        y = 0

        def new_page() -> None:
            nonlocal page, y
            if page:
                pages.append(page)
            page = []
            y = 740
            page.extend(
                [
                    "0.04 0.07 0.11 rg 0 0 612 792 re f",
                    "0.08 0.14 0.22 rg 0 720 612 72 re f",
                    "0.19 0.78 0.92 rg 0 720 6 72 re f",
                    self._pdf_text(42, 762, title, "F2", 18, (1, 1, 1)),
                    self._pdf_text(42, 740, subtitle, "F1", 9, (0.72, 0.8, 0.9)),
                    self._pdf_text(500, 740, "Confidential", "F2", 9, (0.58, 0.76, 0.95)),
                ]
            )

        def ensure_space(required: int) -> None:
            if y - required < 56:
                new_page()

        def add_text(
            text: str,
            font: str = "F1",
            size: int = 10,
            color: tuple[float, float, float] = (0.82, 0.88, 0.95),
            width: int = 88,
            indent: int = 0,
            leading: int = 15,
        ) -> None:
            nonlocal y
            for line in wrap(str(text), width=width) or [""]:
                ensure_space(leading + 2)
                page.append(self._pdf_text(42 + indent, y, line, font, size, color))
                y -= leading

        def add_section(title_text: str, body: list[str], monospace: bool = False) -> None:
            nonlocal y
            ensure_space(54)
            y -= 10
            page.append("0.09 0.14 0.21 rg 36 " + str(y - 8) + " 540 28 re f")
            page.append("0.17 0.55 0.75 RG 36 " + str(y - 8) + " 540 28 re S")
            page.append(self._pdf_text(48, y, title_text, "F2", 12, (1, 1, 1)))
            y -= 32
            for item in body:
                if monospace:
                    add_text(item, "F3", 8, (0.78, 0.93, 1), width=96, indent=10, leading=13)
                else:
                    add_text(item, "F1", 10, (0.82, 0.88, 0.95), width=92, indent=6)
                y -= 4

        new_page()
        page.append("0.07 0.10 0.15 rg 36 608 540 88 re f")
        page.append("0.12 0.20 0.30 RG 36 608 540 88 re S")
        x_positions = [52, 225, 398]
        for index, (label, value) in enumerate(meta):
            x = x_positions[index % 3]
            row_y = 668 if index < 3 else 630
            page.append(self._pdf_text(x, row_y, label.upper(), "F2", 8, (0.48, 0.65, 0.85)))
            page.append(self._pdf_text(x, row_y - 18, str(value), "F2", 12, (1, 1, 1)))
        y = 580

        for section in sections:
            add_section(
                section["title"],
                [str(item) for item in section.get("body", [])],
                bool(section.get("monospace")),
            )

        pages.append(page)
        return self._pdf_bytes(pages)

    def _pdf_bytes(self, pages: list[list[str]]) -> bytes:
        page_count = len(pages)
        page_ids = list(range(3, 3 + page_count))
        font_regular_id = 3 + page_count
        font_bold_id = font_regular_id + 1
        font_mono_id = font_regular_id + 2
        content_ids = list(range(font_regular_id + 3, font_regular_id + 3 + page_count))

        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_ids)}] /Count {page_count} >>",
        ]

        for _page_id, content_id in zip(page_ids, content_ids, strict=True):
            objects.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R /F3 {font_mono_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            )

        objects.extend(
            [
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            ]
        )

        for page in pages:
            stream = "\n".join(page)
            objects.append(
                f"<< /Length {len(stream.encode('latin-1', errors='replace'))} >>\n"
                f"stream\n{stream}\nendstream"
            )

        pdf = "%PDF-1.4\n"
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(pdf.encode("latin-1", errors="replace")))
            pdf += f"{index} 0 obj\n{obj}\nendobj\n"

        xref_offset = len(pdf.encode("latin-1", errors="replace"))
        pdf += f"xref\n0 {len(objects) + 1}\n"
        pdf += "0000000000 65535 f \n"
        for offset in offsets[1:]:
            pdf += f"{offset:010d} 00000 n \n"
        pdf += (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        )

        return pdf.encode("latin-1", errors="replace")

    def _pdf_text(
        self,
        x: int,
        y: int,
        value: str,
        font: str,
        size: int,
        color: tuple[float, float, float],
    ) -> str:
        red, green, blue = color
        safe = self._escape_pdf_text(value)
        return f"BT {red:.2f} {green:.2f} {blue:.2f} rg /{font} {size} Tf 1 0 0 1 {x} {y} Tm ({safe}) Tj ET"

    def _escape_pdf_text(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
