"""Composes an incident report from a finished investigation.

Follows the standard incident-report outline: what happened, when, on what
evidence, why, what it affected, what to do, and what to change so it does not
recur.

Every section is built only from data the investigation actually produced. A
section with nothing behind it is omitted rather than padded — an incident
report with invented content is worse than a short one.
"""

from typing import Any

from app.reports.models import IncidentReport, ReportField, ReportSection

MAX_SIGNALS = 15
MAX_EVIDENCE_ROWS = 20
MAX_TIMELINE_ROWS = 30


class IncidentReportComposer:
    def compose(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        incident_id: str,
        timestamp: str,
        namespace: str,
        status: str,
    ) -> IncidentReport:
        sections = [
            self._executive_summary(diagnosis, investigation, incident_id, namespace, status),
            self._impact(investigation, diagnosis),
            self._timeline(investigation),
            self._root_cause(diagnosis),
            self._evidence(investigation, diagnosis),
            self._confidence(diagnosis),
            self._resolution(diagnosis),
            self._verification(diagnosis),
            self._lessons_learned(diagnosis, investigation),
            self._preventive_actions(diagnosis),
            self._appendix(investigation),
        ]

        return IncidentReport(
            incident_id=incident_id,
            title=str(diagnosis.get("root_cause") or "Kubernetes investigation"),
            generated_at=timestamp,
            sections=tuple(section for section in sections if not section.empty),
        )

    # -- sections ---------------------------------------------------------

    def _executive_summary(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
        incident_id: str,
        namespace: str,
        status: str,
    ) -> ReportSection:
        severity = investigation.get("severity", {})
        coverage = investigation.get("evidence_coverage", {})
        cluster = (
            investigation.get("context")
            or investigation.get("topology", {}).get("cluster")
            or "Current context"
        )

        return ReportSection(
            title="Executive Summary",
            fields=(
                ReportField("Incident", incident_id),
                ReportField("Cluster", str(cluster)),
                ReportField("Namespace", namespace),
                ReportField("Severity", str(severity.get("severity", "Unknown"))),
                ReportField("Status", status),
                ReportField("Confidence", f"{diagnosis.get('confidence', 0)}%"),
                ReportField("Evidence completeness", f"{coverage.get('completeness', 0)}%"),
                ReportField(
                    "Diagnosis source",
                    "AI-assisted, citations validated"
                    if diagnosis.get("ai_generated")
                    else "Deterministic analysis",
                ),
            ),
            body=(str(diagnosis.get("root_cause", "")),),
        )

    def _impact(self, investigation: dict[str, Any], diagnosis: dict[str, Any]) -> ReportSection:
        severity = investigation.get("severity", {})
        overview = investigation.get("overview", {})
        risk = diagnosis.get("remediation_risk", {})

        body = []
        affected = severity.get("affected_workloads")
        if affected:
            body.append(f"{affected} workload(s) affected.")
        if severity.get("affected_namespace") not in (None, "none"):
            body.append(f"Primary namespace affected: {severity['affected_namespace']}.")
        if overview.get("critical_issues"):
            body.append(f"{overview['critical_issues']} critical issue(s) observed.")

        remediation = diagnosis.get("remediation") or {}
        remediation_risk = remediation.get("risk", {})
        if remediation_risk.get("blast_radius"):
            body.append(f"Remediation blast radius: {remediation_risk['blast_radius']}.")
        if remediation_risk.get("estimated_downtime"):
            body.append(
                f"Estimated remediation downtime: {remediation_risk['estimated_downtime']}."
            )
        elif risk.get("impact"):
            body.extend(str(item) for item in risk["impact"])

        return ReportSection(title="Impact", body=tuple(body))

    def _timeline(self, investigation: dict[str, Any]) -> ReportSection:
        rows = [
            (str(entry.get("time", "")), str(entry.get("message", "")))
            for entry in investigation.get("timeline", [])[:MAX_TIMELINE_ROWS]
        ]
        return ReportSection(
            title="Investigation Timeline",
            headers=("Time", "Step"),
            table=tuple(rows),
            note="Times are when the platform completed each collection step.",
        )

    def _root_cause(self, diagnosis: dict[str, Any]) -> ReportSection:
        explanation = str(diagnosis.get("explanation", ""))
        # Strip the operational suffix the analyzer appends for the console.
        explanation = explanation.split("OpenAI status:")[0].strip()
        body = [explanation]

        hypotheses = diagnosis.get("hypotheses") or []
        selected = diagnosis.get("selected_hypothesis")
        rows = []
        for item in hypotheses[:5]:
            marker = "SELECTED" if item.get("id") == selected else ""
            rows.append(
                (
                    str(item.get("title", "")),
                    f"{item.get('confidence', 0)}%",
                    str(item.get("severity", "")),
                    marker,
                )
            )

        note = ""
        refuted = [item.get("title", "") for item in hypotheses if item.get("refuting_signals")]
        if refuted:
            note = "Alternatives the evidence argued against: " + ", ".join(refuted) + "."

        return ReportSection(
            title="Root Cause",
            body=tuple(line for line in body if line),
            headers=("Candidate cause", "Confidence", "Severity", "Selection"),
            table=tuple(rows),
            note=note,
        )

    def _evidence(
        self,
        investigation: dict[str, Any],
        diagnosis: dict[str, Any],
    ) -> ReportSection:
        signals = diagnosis.get("signals") or []
        cited = set(diagnosis.get("cited_signals") or [])

        rows = []
        for signal in signals[:MAX_SIGNALS]:
            rows.append(
                (
                    str(signal.get("severity", "")).upper(),
                    str(signal.get("summary", "")),
                    "cited" if signal.get("id") in cited else "",
                )
            )

        coverage = investigation.get("evidence_coverage", {})
        body = []
        if coverage:
            body.append(
                f"{coverage.get('usable', 0)} of {coverage.get('applicable', 0)} "
                f"applicable evidence records were collected successfully."
            )
            if coverage.get("not_applicable"):
                body.append(
                    f"{coverage['not_applicable']} record(s) did not apply to this "
                    f"cluster and were excluded from that figure."
                )

        degraded = coverage.get("degraded") or []
        for gap in degraded[:MAX_EVIDENCE_ROWS]:
            body.append(f"Gap — {gap.get('kind', '')}: {gap.get('detail', '')}")

        return ReportSection(
            title="Evidence",
            body=tuple(body),
            headers=("Severity", "Observation", "Provenance"),
            table=tuple(rows),
        )

    def _confidence(self, diagnosis: dict[str, Any]) -> ReportSection:
        components = diagnosis.get("confidence_breakdown") or []
        rows = [
            (
                str(item.get("component", "")),
                f"{item.get('score', 0)}%",
                f"weight {item.get('weight', 0)}%",
                f"contributes {item.get('contribution', 0)}",
            )
            for item in components
        ]

        grounding = diagnosis.get("grounding") or {}
        note = ""
        rejected = grounding.get("rejected_citations") or []
        if rejected:
            note = (
                f"{len(rejected)} unsupported citation(s) were rejected from the "
                f"model response and excluded from this diagnosis."
            )

        return ReportSection(
            title="Confidence Assessment",
            headers=("Component", "Score", "Weight", "Contribution"),
            table=tuple(rows),
            body=(f"Overall confidence: {diagnosis.get('confidence', 0)}%.",),
            note=note,
        )

    def _resolution(self, diagnosis: dict[str, Any]) -> ReportSection:
        plan = diagnosis.get("remediation") or {}
        if not plan:
            return ReportSection(
                title="Resolution",
                body=(str(diagnosis.get("fix", "")),) if diagnosis.get("fix") else (),
            )

        risk = plan.get("risk", {})
        body = [str(plan.get("summary", ""))]

        for label, key in (
            ("Before the change", "preconditions"),
            ("The change", "remediation"),
            ("Rollback", "rollback"),
        ):
            steps = plan.get(key) or []
            if not steps:
                continue
            body.append(f"{label}:")
            for index, step in enumerate(steps, start=1):
                suffix = " [manual step]" if step.get("manual") else ""
                body.append(f"  {index}. {step.get('description', '')}{suffix}")
                if step.get("command"):
                    for line in str(step["command"]).splitlines():
                        body.append(f"     $ {line}")

        return ReportSection(
            title="Resolution",
            fields=(
                ReportField("Change type", str(risk.get("change_kind", "unknown"))),
                ReportField("Risk", str(risk.get("level", "unknown"))),
                ReportField(
                    "Approval required",
                    "Yes" if plan.get("requires_approval") else "No",
                ),
                ReportField("Restart required", "Yes" if risk.get("restart_required") else "No"),
            ),
            body=tuple(line for line in body if line),
            note="No change was applied by the platform; these steps are for review.",
        )

    def _verification(self, diagnosis: dict[str, Any]) -> ReportSection:
        plan = diagnosis.get("remediation") or {}
        steps = plan.get("verification") or []
        body = []

        for index, step in enumerate(steps, start=1):
            body.append(f"{index}. {step.get('description', '')}")
            if step.get("command"):
                body.append(f"   $ {step['command'].splitlines()[0]}")

        permissions = plan.get("required_permissions") or []
        if permissions:
            body.append("Access required to carry this out:")
            body.extend(f"   $ {item.get('check_command', '')}" for item in permissions)

        return ReportSection(title="Verification", body=tuple(body))

    def _lessons_learned(
        self,
        diagnosis: dict[str, Any],
        investigation: dict[str, Any],
    ) -> ReportSection:
        body = []

        gaps = diagnosis.get("evidence_gaps") or []
        if gaps:
            body.append("Evidence that would have shortened this investigation:")
            body.extend(f"  - {gap}" for gap in gaps[:8])

        rounds = investigation.get("playbook_rounds") or []
        for entry in rounds:
            playbooks = ", ".join(entry.get("playbooks", []))
            body.append(
                f"Deep investigation ({playbooks}) added "
                f"{entry.get('evidence_added', 0)} evidence record(s); the initial "
                f"pass alone would not have reached this conclusion."
            )

        caveats = (diagnosis.get("remediation") or {}).get("caveats") or []
        if caveats:
            body.append("Caveats carried by the proposed remediation:")
            body.extend(f"  - {caveat}" for caveat in caveats)

        return ReportSection(title="Lessons Learned", body=tuple(body))

    def _preventive_actions(self, diagnosis: dict[str, Any]) -> ReportSection:
        body = []

        prevention = diagnosis.get("prevention")
        if prevention:
            body.append(str(prevention))

        next_steps = diagnosis.get("next_steps") or []
        if next_steps:
            body.append("Follow-up actions:")
            body.extend(f"  - {step}" for step in next_steps[:8])

        return ReportSection(title="Preventive Actions", body=tuple(body))

    def _appendix(self, investigation: dict[str, Any]) -> ReportSection:
        commands = investigation.get("executed_commands") or []
        return ReportSection(
            title="Appendix: Commands Executed",
            body=tuple(str(command) for command in commands[:60]),
            note=(
                "Every command the platform ran during this investigation. All are "
                "read-only; the platform cannot modify a cluster."
            )
            if commands
            else "",
        )
