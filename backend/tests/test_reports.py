"""Incident report composition."""

from app.reports.composer import IncidentReportComposer

COMPOSER = IncidentReportComposer()

DIAGNOSIS = {
    "root_cause": "Pod references configuration that does not exist",
    "explanation": "The container cannot start until the reference resolves.",
    "fix": "Add the missing key.",
    "prevention": "Validate references in CI.",
    "next_steps": ["Patch the ConfigMap", "Redeploy"],
    "evidence_gaps": ["Previous container logs"],
    "confidence": 94,
    "ai_generated": False,
    "confidence_breakdown": [
        {
            "component": "Evidence Strength",
            "weight": 70,
            "score": 92,
            "contribution": 64,
            "detail": "Top hypothesis well supported.",
        },
        {
            "component": "Evidence Completeness",
            "weight": 30,
            "score": 100,
            "contribution": 30,
            "detail": "All evidence collected.",
        },
    ],
    "grounding": {
        "valid": True,
        "reason": "",
        "cited_signals": [],
        "rejected_citations": ["signal.invented"],
    },
    "signals": [
        {
            "id": "config.key_missing:pod/prod/web-0",
            "severity": "critical",
            "summary": "ConfigMap web-config is missing DB_HOST",
        },
        {
            "id": "pod.crash_loop:pod/prod/web-0",
            "severity": "critical",
            "summary": "Pod prod/web-0 is in CrashLoopBackOff",
        },
    ],
    "cited_signals": ["config.key_missing:pod/prod/web-0"],
    "selected_hypothesis": "workload.missing_configuration",
    "hypotheses": [
        {
            "id": "workload.missing_configuration",
            "title": "Missing configuration",
            "confidence": 92,
            "severity": "critical",
            "refuting_signals": [],
        },
        {
            "id": "workload.application_startup_failure",
            "title": "Startup failure",
            "confidence": 25,
            "severity": "critical",
            "refuting_signals": ["config.key_missing:pod/prod/web-0"],
        },
    ],
    "remediation_risk": {"level": "Low", "impact": ["Restart Required"]},
    "remediation": {
        "summary": "Create the missing key.",
        "requires_approval": True,
        "risk": {
            "level": "Low",
            "change_kind": "configuration",
            "restart_required": True,
            "estimated_downtime": "None",
            "blast_radius": "Workloads in prod",
            "reversible": True,
            "notes": [],
        },
        "preconditions": [
            {
                "description": "Check the ConfigMap",
                "command": "kubectl get configmap web-config -n prod",
                "manual": False,
            }
        ],
        "remediation": [
            {"description": "Create the key", "command": "kubectl create ...", "manual": False}
        ],
        "verification": [
            {
                "description": "Confirm present",
                "command": "kubectl describe configmap web-config -n prod",
                "manual": False,
            }
        ],
        "rollback": [
            {"description": "Restore", "command": "kubectl apply -f before.yaml", "manual": False}
        ],
        "required_permissions": [{"check_command": "kubectl auth can-i patch configmaps -n prod"}],
        "caveats": ["The Deployment name was derived."],
        "patches": [],
    },
}

INVESTIGATION = {
    "context": "prod-east",
    "severity": {"severity": "Critical", "affected_workloads": 1, "affected_namespace": "prod"},
    "overview": {"critical_issues": 1},
    "timeline": [
        {"time": "10:00:00", "message": "Investigation Started"},
        {"time": "10:00:02", "message": "Retrieved Pods"},
    ],
    "evidence_coverage": {
        "total": 15,
        "applicable": 13,
        "usable": 13,
        "not_applicable": 2,
        "completeness": 100,
        "degraded": [
            {
                "kind": "prometheus.pod.metrics",
                "status": "not_applicable",
                "detail": "Prometheus is not configured.",
            }
        ],
    },
    "playbook_rounds": [{"round": 1, "playbooks": ["crashloop"], "evidence_added": 4}],
    "executed_commands": ["kubectl get pods -A -o json"],
}


def compose(diagnosis=None, investigation=None):
    return COMPOSER.compose(
        diagnosis if diagnosis is not None else DIAGNOSIS,
        investigation if investigation is not None else INVESTIGATION,
        incident_id="INC-001",
        timestamp="2026-07-26T10:00:00Z",
        namespace="prod",
        status="success",
    )


def lines_of(report, title):
    section = report.section(title)
    return "\n".join(section.as_lines()) if section else ""


class TestOutline:
    def test_follows_the_incident_report_outline(self):
        titles = [section.title for section in compose().sections]

        for expected in (
            "Executive Summary",
            "Impact",
            "Investigation Timeline",
            "Root Cause",
            "Evidence",
            "Confidence Assessment",
            "Resolution",
            "Verification",
            "Lessons Learned",
            "Preventive Actions",
        ):
            assert expected in titles

    def test_executive_summary_leads(self):
        assert compose().sections[0].title == "Executive Summary"

    def test_empty_sections_are_omitted_not_padded(self):
        bare = compose(
            diagnosis={"root_cause": "Something", "confidence": 10},
            investigation={"timeline": []},
        )
        titles = [section.title for section in bare.sections]

        # Nothing to say about remediation, so no Resolution section is invented.
        assert "Resolution" not in titles
        assert "Preventive Actions" not in titles
        assert "Executive Summary" in titles


class TestContent:
    def test_executive_summary_states_provenance_and_completeness(self):
        text = lines_of(compose(), "Executive Summary")

        assert "INC-001" in text
        assert "prod-east" in text
        assert "94%" in text
        assert "Deterministic analysis" in text

    def test_ai_assisted_diagnoses_are_labelled_as_such(self):
        text = lines_of(compose({**DIAGNOSIS, "ai_generated": True}), "Executive Summary")
        assert "AI-assisted, citations validated" in text

    def test_root_cause_lists_alternatives_and_marks_the_selected_one(self):
        section = compose().section("Root Cause")
        flattened = "\n".join(section.as_lines())

        assert "SELECTED" in flattened
        assert "Missing configuration" in flattened
        assert "argued against" in section.note
        assert "Startup failure" in section.note

    def test_evidence_reports_gaps_and_excludes_inapplicable_from_the_ratio(self):
        text = lines_of(compose(), "Evidence")

        assert "13 of 13 applicable" in text
        assert "2 record(s) did not apply" in text
        assert "Prometheus is not configured" in text

    def test_evidence_marks_which_signals_the_diagnosis_cited(self):
        text = lines_of(compose(), "Evidence")
        assert "cited" in text

    def test_confidence_shows_the_weighting_and_rejected_citations(self):
        section = compose().section("Confidence Assessment")
        flattened = "\n".join(section.as_lines())

        assert "Evidence Strength" in flattened
        assert "weight 70%" in flattened
        assert "1 unsupported citation(s) were rejected" in section.note

    def test_resolution_states_it_was_not_applied(self):
        section = compose().section("Resolution")

        assert "No change was applied by the platform" in section.note
        flattened = "\n".join(section.as_lines())
        assert "Approval required: Yes" in flattened
        assert "kubectl create" in flattened

    def test_verification_includes_permission_checks(self):
        text = lines_of(compose(), "Verification")
        assert "kubectl auth can-i patch configmaps -n prod" in text

    def test_lessons_learned_credits_the_deep_investigation(self):
        text = lines_of(compose(), "Lessons Learned")

        assert "crashloop" in text
        assert "4 evidence record(s)" in text
        assert "Previous container logs" in text
        assert "derived" in text

    def test_appendix_notes_that_all_commands_were_read_only(self):
        section = compose().section("Appendix: Commands Executed")

        assert "kubectl get pods -A -o json" in section.as_lines()
        assert "cannot modify a cluster" in section.note


class TestSerialization:
    def test_round_trips_to_a_dict(self):
        payload = compose().to_dict()

        assert payload["incident_id"] == "INC-001"
        assert payload["sections"]
        first = payload["sections"][0]
        assert set(first) == {"title", "body", "fields", "table", "headers", "note"}

    def test_tables_carry_headers(self):
        timeline = compose().section("Investigation Timeline")
        assert timeline.headers == ("Time", "Step")

    def test_operational_noise_is_kept_out_of_the_root_cause(self):
        noisy = {
            **DIAGNOSIS,
            "explanation": "The reference is missing. OpenAI status: no API key",
        }
        text = lines_of(compose(noisy), "Root Cause")

        assert "The reference is missing." in text
        assert "OpenAI status" not in text

    def test_survives_a_diagnosis_with_nothing_in_it(self):
        report = COMPOSER.compose({}, {}, "INC-002", "2026-01-01T00:00:00Z", "default", "error")
        assert report.incident_id == "INC-002"
        assert report.to_dict()["sections"] is not None
