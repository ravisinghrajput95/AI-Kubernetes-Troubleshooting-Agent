"""The Reports table's Environment and Status columns say only what is known.

Read off a console sweep: one kind cluster was Development through its
kubeconfig context and Unknown through its agent; `kind-prod` would have been
Development; and every row's Status read "Open" — an incident lifecycle
nothing tracks, which a healthy run turned into "Resolved".
"""

import pytest

from app.reports.rendering import ReportRenderer

RENDERER = ReportRenderer()


@pytest.mark.parametrize(
    ("cluster", "environment"),
    [
        ("kind-prod", "Production"),
        ("kind-k8s-agent-dev", "Development"),
        ("kind-m4b", "Development"),
        ("docker-desktop", "Development"),
        ("prod-eu-1", "Production"),
        ("gke_acme_europe-west1_staging", "Staging"),
        ("api-cut", "Unknown"),
        ("kindling-prod", "Production"),  # "kind" is a word, not a substring
        ("mankind", "Unknown"),
        ("devops-tools", "Unknown"),  # "dev" inside a word states nothing
        ("prod-dev-mirror", "Unknown"),  # two environments stated is none
    ],
)
def test_environment_is_what_the_name_states(cluster, environment):
    assert RENDERER.environment(cluster) == environment


@pytest.mark.parametrize(
    ("health", "status"),
    [
        ("issues_found", "Issues found"),
        ("healthy", "No issues found"),
        ("error", "Could not investigate"),
        ("", "Unknown"),
    ],
)
def test_status_is_what_the_investigation_found(health, status):
    assert RENDERER.incident_status({"health": {"status": health}}) == status


def test_status_never_claims_an_incident_lifecycle():
    for health in ("issues_found", "healthy", "error", "degraded", ""):
        assert RENDERER.incident_status({"health": {"status": health}}) not in {"Open", "Resolved"}


async def test_the_history_row_the_console_reads_carries_both(tmp_path):
    from app.services.history_service import InvestigationHistoryService
    from app.services.report_store import FilesystemReportStore
    from tests.test_report_namespace import CONFIGMAP_REF, TwoNamespaces, diagnose

    investigation, diagnosis = await diagnose(TwoNamespaces(CONFIGMAP_REF))
    investigation = {**investigation, "context": "kind-prod"}
    item = InvestigationHistoryService(FilesystemReportStore(tmp_path)).save(
        diagnosis, investigation
    )
    assert (item["environment"], item["incident_status"]) == ("Production", "Issues found")
