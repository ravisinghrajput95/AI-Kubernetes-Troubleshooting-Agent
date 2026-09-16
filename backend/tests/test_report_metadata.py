"""The Reports table's Environment and Status columns say only what is known.

Read off a console sweep: one kind cluster was Development through its
kubeconfig context and Unknown through its agent; `kind-prod` would have been
Development; and every row's Status read "Open" — an incident lifecycle
nothing tracks, which a healthy run turned into "Resolved".
"""

from typing import ClassVar

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


class TestWhatIsAffected:
    """Named observations, not hedges chosen by which sections had findings.

    Read off a sweep: every report of a nine-fault namespace carried the same
    "Affected workloads may be unavailable or unstable" and "Service routing or
    in-cluster connectivity may be impacted" under "Business Impact", which is a
    consequence the platform cannot know.
    """

    INVESTIGATION: ClassVar[dict] = {
        "health": {"status": "issues_found"},
        "pods": {
            "problematic_pods": [
                {
                    "name": "checkout-5b5fd56dbf-4cnmv",
                    "namespace": "payments",
                    "status": "CrashLoopBackOff",
                },
                {
                    "name": "metrics-server-649dd47df4-gvzbh",
                    "namespace": "kube-system",
                    "status": "CrashLoopBackOff",
                    "reported_now": False,
                },
            ]
        },
        "network": {
            "findings": [
                {
                    "namespace": "payments",
                    "service": "checkout-svc",
                    "issue": "Service has no ready endpoints",
                }
            ]
        },
        "storage": {
            "findings": [
                {
                    "namespace": "payments",
                    "name": "archive-data",
                    "issue": "PersistentVolumeClaim is not bound",
                }
            ]
        },
    }

    def lines(self, investigation=None):
        return RENDERER._business_impact(investigation or self.INVESTIGATION)

    def test_each_line_names_what_it_is_about(self):
        text = "\n".join(self.lines())
        for name in (
            "payments/checkout-5b5fd56dbf-4cnmv",
            "payments/checkout-svc",
            "payments/archive-data",
        ):
            assert name in text

    def test_nothing_is_a_guess_about_consequences(self):
        assert not [
            line for line in self.lines() if " may " in line and "may not be everything" not in line
        ]

    def test_restart_history_is_not_called_failing(self):
        failing = next(line for line in self.lines() if "failing now" in line)
        assert "metrics-server" not in failing
        assert any(
            "metrics-server" in line and "restarted recently" in line for line in self.lines()
        )

    def test_a_long_list_stays_one_line(self):
        pods = [
            {"name": f"p-{index}", "namespace": "ns", "status": "Pending"} for index in range(9)
        ]
        lines = self.lines({"pods": {"problematic_pods": pods}})
        assert lines == [
            "9 pods failing now: ns/p-0 (Pending), ns/p-1 (Pending), ns/p-2 (Pending), ns/p-3 (Pending), ns/p-4 (Pending), and 4 more."
        ]

    def test_an_unread_section_is_said_not_passed_over(self):
        lines = self.lines({"health": {"status": "healthy"}, "pods": {"error": "Forbidden"}})
        assert lines[0].startswith("Could not read pods")
        assert "what could be read" in lines[-1]

    def test_a_truncated_list_says_there_may_be_more(self):
        lines = self.lines(
            {**self.INVESTIGATION, "collection_limits": {"truncated": True, "max_list_items": 3}}
        )
        assert lines[-1] == "Lists were cut at 3 items, so there may be more."
