"""Turning an investigation into a report, in three formats.

Lifted out of `InvestigationHistoryService`, which was doing three jobs in one
998-line file: composing a report, rendering it, and keeping the history index.
The dependency between them only ever ran one way — rendering never reached
back into persistence — so this is the seam that was already there.

**The four public derivations are shared with the history index on purpose.**
A report's severity, incident status, environment and incident id are the same
facts the index shows beside a row, and two implementations of "how severe was
this" would eventually disagree about the same investigation depending on
whether you were reading the list or the PDF. One implementation, called from
both, is why they cannot.

The PDF is still hand-rolled object emission — base-14 fonts, no PDF
dependency — so section bodies arrive pre-wrapped and non-ASCII escaped. That
constraint is why `ReportSection.as_lines()` exists and why text cannot simply
be handed to the writer.
"""

import json
import re
from textwrap import wrap
from typing import Any

from app.reports.composer import IncidentReportComposer

_ENVIRONMENT_WORDS = {
    "Production": frozenset({"prod", "production", "prd"}),
    "Staging": frozenset({"stage", "staging", "stg"}),
    "Development": frozenset({"dev", "development"}),
}
_LOCAL_CLUSTER_WORDS = frozenset({"kind", "minikube", "k3d"})
_FINDING_STATUS = {
    "issues_found": "Issues found",
    "healthy": "No issues found",
    "error": "Could not investigate",
}


NAMED_LIMIT = 5


def _count(items: list, noun: str) -> str:
    return f"{len(items)} {noun}" + ("" if len(items) == 1 else "s")


def _named(names) -> str:
    """The first few names and how many more, so a line stays a line."""
    names = list(names)
    shown = ", ".join(names[:NAMED_LIMIT])
    rest = len(names) - NAMED_LIMIT
    return f"{shown}, and {rest} more." if rest > 0 else f"{shown}."


class ReportRenderer:
    """One composition, three renderings, so the formats cannot disagree.

    `IncidentReportComposer` builds the structured report; the PDF, Markdown
    and JSON writers all render *that*, which is what makes a new section one
    change rather than three.
    """

    def render_pdf(
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
        severity = self.severity(investigation)
        cluster = (
            investigation.get("context")
            or investigation.get("topology", {}).get("cluster")
            or "Current Context"
        )
        environment = self.environment(cluster)
        incident_status = self.incident_status(investigation)

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
                # Structured rather than pre-flattened: `as_lines()` joins table
                # rows with " | ", and a proportional font wrapping that string
                # produced ragged pseudo-columns with orphaned separators. The
                # PDF lays the same rows out as real columns instead. Markdown
                # and JSON still use the composer's own rendering.
                "fields": [(item.label, item.value) for item in section.fields],
                "body": list(section.body),
                "table": [list(row) for row in section.table],
                "headers": list(section.headers),
                "note": section.note,
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

    def render_json(
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
                "environment": self.environment(cluster),
                "severity": self.severity(investigation),
                "incident_status": self.incident_status(investigation),
                "business_impact": self._business_impact(investigation),
                "confidence_breakdown": self._confidence_breakdown(diagnosis),
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

    def render_markdown(
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

    def incident_id(self, timestamp: str, investigation_id: str) -> str:
        date_part = timestamp[:10].replace("-", "")
        return f"INC-{date_part}-{investigation_id[:8].upper()}"

    def environment(self, cluster: str) -> str:
        """The environment a cluster's *name* states, or Unknown.

        Nothing configures this; it is read from the name an operator chose.
        It used to be substring matches with local tooling checked first, so
        `kind-prod` was Development, `devops-prod` was Production only by
        luck of order, and `kind` matched inside any word containing it. Whole
        words only, and a name that states two environments states none.
        """
        words = set(re.split(r"[^a-z0-9]+", cluster.lower()))
        stated = {environment for environment, names in _ENVIRONMENT_WORDS.items() if words & names}
        if len(stated) == 1:
            return stated.pop()
        if not stated and (words & _LOCAL_CLUSTER_WORDS or cluster.startswith("docker-desktop")):
            return "Development"
        return "Unknown"

    def _short_cluster(self, cluster: str) -> str:
        if "/" in cluster:
            return cluster.rstrip("/").split("/")[-1]
        return cluster

    def incident_status(self, investigation: dict[str, Any]) -> str:
        """What the investigation found, not an incident lifecycle.

        This said "Open" for every run that found anything or failed, and
        "Resolved" for a healthy one — a ticket state nothing here tracks, so
        no report could ever become resolved, and one about a cluster that was
        never broken claimed something had been fixed.
        """
        health = (investigation.get("health") or {}).get("status", "")
        return _FINDING_STATUS.get(health, "Unknown")

    def severity(self, investigation: dict[str, Any]) -> str:
        """Severity for the history entry.

        This used to escalate a failed investigation to Critical, which was a
        workaround for severity reporting "Healthy" when no collector had run.
        That is fixed at the source now, and escalating here would be the same
        overclaim pointing the other way: a cluster nobody could read is not
        Critical, it is unknown. The correction is kept only for reports
        written before the upstream fix.
        """
        severity = investigation.get("severity", {}).get("severity", "Not assessed")
        if severity == "Healthy" and (
            investigation.get("health", {}).get("status") == "error"
            or self._has_failed_evidence(investigation)
        ):
            return "Unknown"
        return severity

    def _business_impact(self, investigation: dict[str, Any]) -> list[str]:
        """What the collected evidence shows is affected, by name.

        It printed hedges chosen by which sections had findings — "Affected
        workloads may be unavailable or unstable", "New deployments may be
        blocked until cluster access is restored" — under a heading that
        claimed business consequences the platform has no way to know. The
        same three sentences headed every report of a namespace with nine
        different faults. Each line now names what was observed, says when a
        read failed or a list was cut, and keeps a pod listed only for its
        restart history apart from one failing now.
        """
        lines = []
        unread = [
            key
            for key in ("pods", "events", "deployments", "network", "nodes", "storage", "workloads")
            if investigation.get(key, {}).get("error")
        ]
        if unread:
            lines.append(
                f"Could not read {', '.join(unread)}, so this may not be everything affected."
            )

        pods = investigation.get("pods", {}).get("problematic_pods") or []
        failing = [pod for pod in pods if pod.get("reported_now", True) is not False]
        restarted = [pod for pod in pods if pod.get("reported_now", True) is False]
        if failing:
            lines.append(
                f"{_count(failing, 'pod')} failing now: "
                + _named(
                    f"{pod.get('namespace')}/{pod.get('name')} ({pod.get('status')})"
                    for pod in failing
                )
            )
        if restarted:
            lines.append(
                f"{_count(restarted, 'pod')} running, but restarted recently: "
                + _named(f"{pod.get('namespace')}/{pod.get('name')}" for pod in restarted)
            )
        for finding in (investigation.get("network", {}).get("findings") or [])[:NAMED_LIMIT]:
            lines.append(
                f"Service {finding.get('namespace')}/{finding.get('service')}: {finding.get('issue')}."
            )
        for finding in (investigation.get("storage", {}).get("findings") or [])[:NAMED_LIMIT]:
            lines.append(
                f"PersistentVolumeClaim {finding.get('namespace')}/{finding.get('name')}: "
                f"{finding.get('issue')}."
            )
        for finding in (investigation.get("nodes", {}).get("findings") or [])[:NAMED_LIMIT]:
            reason = f" ({finding['reason']})" if finding.get("reason") else ""
            lines.append(
                f"Node {finding.get('node')}: {finding.get('type')} is {finding.get('status')}{reason}."
            )

        limits = investigation.get("collection_limits") or {}
        if limits.get("truncated"):
            lines.append(
                f"Lists were cut at {limits.get('max_list_items')} items, so there may be more."
            )

        if len(lines) == len(unread) + bool(limits.get("truncated")):
            lines.append(
                "Nothing failing was observed in what was collected."
                if not unread
                else "Nothing failing was observed in what could be read."
            )
        return lines

    def _confidence_breakdown(self, diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
        """The composition `app/analysis/confidence.py` actually computed.

        This invented its own: fixed weights per collected section — Pod
        Analysis 25, Event Analysis 20, Logs 20 — that sum to about a hundred,
        bear no relation to the confidence printed above them, and were
        rendered under the heading "AI Confidence Breakdown" on diagnoses no
        model had touched. An investigation whose API server was unreachable
        got a hardcoded `0/0/25/35/40`, which is a number for every component
        and a measurement of none. The real breakdown names each component,
        its weight and its contribution, and the contributions sum to the
        confidence; when a diagnosis carries none, the section is omitted
        rather than filled.
        """
        breakdown = diagnosis.get("confidence_breakdown") or []
        return [
            {
                "source": str(part.get("component", "")),
                "contribution": int(part.get("contribution", 0)),
                "weight": int(part.get("weight", 0)),
                "score": int(part.get("score", 0)),
                "detail": str(part.get("detail", "")),
            }
            for part in breakdown
            if isinstance(part, dict)
        ]

    def _evidence_matrix(self, investigation: dict[str, Any]) -> list[tuple[str, str]]:
        rows = [
            ("Pods", self._evidence_status(investigation.get("pods", {}))),
            ("Events", self._evidence_status(investigation.get("events", {}))),
            ("Nodes", self._evidence_status(investigation.get("nodes", {}))),
            ("Deployments", self._evidence_status(investigation.get("deployments", {}))),
            ("Services", self._evidence_status(investigation.get("network", {}))),
            ("Storage", self._evidence_status(investigation.get("storage", {}))),
            ("Extended Workloads", self._evidence_status(investigation.get("workloads", {}))),
            ("Cluster reads", self._read_outcome(investigation)),
        ]
        return rows

    def _read_outcome(self, investigation: dict[str, Any]) -> str:
        """What the collected evidence says about reaching the cluster.

        Two rows here were not evidence. **"Port 6443"** reported `Closed` or
        `Unverified` for a probe this platform does not perform — it has no
        port check, and on the agent path there is no connection to 6443 from
        here at all — so every healthy report carried a row for a test that
        never ran. **"API Connectivity"** was substring-matching kubectl's
        prose ("connection refused", "couldn't get current server api group
        list"), which is the classifier defect `app/kubernetes/errors.py`
        already had: an agent's `dial tcp …:6443: i/o timeout` matched by
        luck of the digits. The evidence store already counts what happened.
        """
        coverage = investigation.get("evidence_coverage") or {}
        applicable = int(coverage.get("total", 0)) - int(coverage.get("not_applicable", 0))
        usable = int(coverage.get("usable", 0))
        if applicable <= 0:
            return "Not Available"
        if usable == 0:
            return "None succeeded"
        if usable < applicable:
            return f"{usable} of {applicable} succeeded"
        return "All succeeded"

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

    # Layout constants for the hand-rolled PDF. Named because the relationship
    # between them is the thing that was wrong: the header band runs to
    # HEADER_BOTTOM, and body text has to start below it on every page.
    #
    # There is no PDF library here on purpose — see `_pdf_bytes` — so these are
    # the only thing standing between a section and the title it would
    # otherwise be drawn through.
    HEADER_BOTTOM = 720
    BODY_TOP = HEADER_BOTTOM - 30
    PAGE_BOTTOM = 56

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
            # The header band occupies HEADER_BOTTOM..792. Body text used to start at
            # 740, which is *inside* it, so every page after the first drew its
            # first lines through the title and the generation timestamp. Page
            # one never showed it because the meta box resets y to 580.
            y = self.BODY_TOP
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
            if y - required < self.PAGE_BOTTOM:
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
            lines = wrap(str(text), width=width) or [""]
            for position, line in enumerate(lines):
                ensure_space(leading + 2)
                # Continuation lines are indented, so a wrapped record reads as
                # one record. Flush-left continuations made every wrapped row
                # look like a new finding.
                offset = indent if position == 0 else indent + 12
                page.append(self._pdf_text(42 + offset, y, line, font, size, color))
                y -= leading

        def add_field(label: str, value: str) -> None:
            """A label/value pair on one baseline, in two aligned columns."""
            nonlocal y
            ensure_space(17)
            page.append(self._pdf_text(48, y, str(label), "F2", 9, (0.52, 0.66, 0.84)))
            for position, line in enumerate(wrap(str(value), width=64) or [""]):
                if position:
                    ensure_space(15)
                page.append(self._pdf_text(196, y, line, "F1", 10, (0.86, 0.91, 0.96)))
                y -= 15
            y -= 2

        def add_table(headers: list[str], rows: list[list[str]]) -> None:
            """Real columns, sized to their contents.

            Flattening a row to "a | b | c" and letting a proportional font wrap
            it is what produced the ragged output with separators stranded on
            their own line.

            Widths are proportional to the longest cell in each column rather
            than split evenly. An evenly split table gave a severity column
            reading "HIGH" the same 190pt as the sentence beside it, so the
            sentence wrapped five times against acres of empty page.
            """
            nonlocal y
            if not rows:
                return

            columns = max(len(row) for row in rows)
            if columns == 0:
                return

            longest = [1] * columns
            for row in [headers, *rows] if headers else rows:
                for index, cell in enumerate(row[:columns]):
                    longest[index] = max(longest[index], len(str(cell)))

            # Proportional, but no column may vanish or hog the page.
            available = 516
            total = sum(longest)
            widths = [max(46, min(300, int(available * portion / total))) for portion in longest]
            # Rescale if the floors pushed the row past the page.
            overflow = sum(widths) - available
            if overflow > 0:
                widest = widths.index(max(widths))
                widths[widest] = max(46, widths[widest] - overflow)

            positions = [48]
            for width in widths[:-1]:
                positions.append(positions[-1] + width)

            def draw(row: list[str], font: str, colour: tuple[float, float, float]) -> None:
                nonlocal y
                cells = [
                    wrap(str(cell), width=max(6, widths[index] // 5)) or [""]
                    for index, cell in enumerate(row[:columns])
                ]
                height = max(len(cell) for cell in cells)
                ensure_space(height * 13 + 4)
                top = y
                for index, lines in enumerate(cells):
                    for offset, line in enumerate(lines):
                        page.append(
                            self._pdf_text(
                                positions[index], top - offset * 13, line, font, 9, colour
                            )
                        )
                y = top - height * 13 - 3

            if headers:
                draw(list(headers), "F2", (0.52, 0.66, 0.84))
                page.append(f"0.20 0.28 0.38 RG 48 {y + 8} m 564 {y + 8} l S")
                y -= 6

            for row in rows:
                draw(row, "F1", (0.86, 0.91, 0.96))

        def add_section(section: dict[str, Any]) -> None:
            nonlocal y
            ensure_space(54)
            y -= 10
            page.append("0.09 0.14 0.21 rg 36 " + str(y - 8) + " 540 28 re f")
            page.append("0.17 0.55 0.75 RG 36 " + str(y - 8) + " 540 28 re S")
            page.append(self._pdf_text(48, y, str(section["title"]), "F2", 12, (1, 1, 1)))
            y -= 32

            monospace = bool(section.get("monospace"))

            for label, value in section.get("fields", []):
                add_field(label, value)

            for item in section.get("body", []):
                if monospace:
                    add_text(item, "F3", 8, (0.78, 0.93, 1), width=96, indent=10, leading=13)
                else:
                    add_text(item, "F1", 10, (0.82, 0.88, 0.95), width=92, indent=6)
                y -= 4

            table = section.get("table", [])
            if table:
                y -= 4
                add_table(
                    [str(cell) for cell in section.get("headers", [])],
                    [[str(cell) for cell in row] for row in table],
                )

            if section.get("note"):
                y -= 2
                add_text(section["note"], "F1", 9, (0.55, 0.66, 0.80), width=104, indent=6)

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
            add_section(section)

        pages.append(page)

        total = len(pages)
        for number, rendered in enumerate(pages, start=1):
            rendered.append(
                self._pdf_text(511, 32, f"Page {number} of {total}", "F1", 8, (0.45, 0.55, 0.68))
            )

        return self._pdf_bytes(pages)

    def _pdf_bytes(self, pages: list[list[str]]) -> bytes:
        """Emit a PDF by hand, with no PDF dependency.

        **The character set is latin-1, and that is a hard limit rather than a
        default.** The base-14 Type1 fonts used here carry no embedded glyphs,
        so anything outside latin-1 — CJK, Cyrillic, Greek, emoji — cannot be
        represented at all. `TRANSLITERATIONS` maps the punctuation this
        platform actually emits (em dashes, curly quotes, arrows, check marks)
        down to ASCII, and `errors="replace"` turns whatever survives into `?`.

        Accented latin text is fine and renders correctly; a Japanese namespace
        name or an emoji in a log line does not. If that ever needs to work it
        means embedding a font, which means a font file and a real PDF library,
        which is the dependency this writer exists to avoid — so it is a
        decision to revisit, not a bug to fix in place.

        The three font dictionaries below must keep `/Encoding /WinAnsiEncoding`;
        the reason is argued where they are declared, and it is the difference
        between `é` and `Ø`.
        """
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
                # `/Encoding /WinAnsiEncoding` is load-bearing, not decoration.
                # Text is written below with `.encode("latin-1")`, and a Type1
                # base-14 font with no `/Encoding` uses **StandardEncoding**,
                # where byte 0xE9 is not `é` — it is `Ø`. Every accented
                # character in a namespace, node label or log line therefore
                # rendered as mojibake in the one artefact most likely to be
                # attached to a customer's incident record, silently and with
                # no error anywhere. WinAnsiEncoding agrees with latin-1 across
                # the printable range, which is what makes that encode call
                # correct rather than accidental.
                #
                # Characters outside latin-1 (CJK, emoji) still cannot be
                # represented by a base-14 font and are transliterated or
                # replaced by `_escape_pdf_text`. That is a real limit of
                # having no font-embedding dependency, and it is stated here
                # rather than left to be discovered in a report.
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                "/Encoding /WinAnsiEncoding >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
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

    # Typographic characters the composer emits, and their ASCII equivalents.
    #
    # The PDF is written with base-14 fonts and encoded latin-1, so anything
    # outside that range was silently turned into `?` by `errors="replace"` —
    # which is how "Gap — k8s.quotas" reached operators as "Gap ? k8s.quotas".
    # Transliterating first keeps the punctuation meaningful; the fallback
    # still exists for genuinely unrepresentable text, but it no longer fires
    # on the dashes and quotes the reports actually contain.
    # Written as escapes, not literals: the whole point is that these
    # characters are hard to tell apart from their ASCII lookalikes, which is
    # also how they reached the writer unnoticed in the first place.
    TRANSLITERATIONS = str.maketrans(
        {
            "\u2014": "-",  # em dash
            "\u2013": "-",  # en dash
            "\u2018": "'",  # left single quote
            "\u2019": "'",  # right single quote
            "\u201c": '"',  # left double quote
            "\u201d": '"',  # right double quote
            "\u2022": "-",  # bullet
            "\u00b7": "-",  # middle dot
            "\u2026": "...",  # ellipsis
            "\u2192": "->",  # rightwards arrow
            "\u00a0": " ",  # non-breaking space
            "\u2713": "OK",  # check mark
            "\u2717": "X",  # ballot X
        }
    )

    def _escape_pdf_text(self, value: str) -> str:
        """Transliterate to latin-1, then escape PDF string syntax.

        Two separate jobs in one pass. The transliteration is the **character
        set** limit described on `_pdf_bytes`: anything not in latin-1 either
        maps to an ASCII stand-in above or becomes `?` at encode time. The
        backslash and parenthesis escaping is PDF **syntax** — an unescaped `)`
        in a pod name or a log line closes the string literal early and corrupts
        every byte offset in the cross-reference table after it, producing a
        file no reader will open.
        """
        readable = str(value).translate(self.TRANSLITERATIONS)
        return readable.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
