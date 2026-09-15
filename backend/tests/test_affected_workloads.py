"""A Deployment and its pods are one affected workload.

Read off a console sweep of a namespace with eight broken Deployments: the
report said "17 workload(s) affected" (eight Deployments plus nine of their
pods) and "19 critical issue(s) observed", and severity's Critical threshold of
three workloads could be met by one Deployment with two failing replicas.
"""

from app.services.investigation_service import InvestigationService

DEPLOYMENTS = [
    "archiver",
    "batch-trainer",
    "checkout",
    "fraud-scorer",
    "gateway",
    "ledger",
    "notifier",
    "secret-reader",
]
PODS = [
    "archiver-6795b9bc5d-zhbs2",
    "batch-trainer-5874869894-4fgft",
    "checkout-5b5fd56dbf-4cnmv",
    "checkout-5b5fd56dbf-zm4ws",
    "fraud-scorer-75cbd9fb69-kcv7p",
    "gateway-765557fcc4-ljj5b",
    "ledger-55ffdd65fb-mjnvw",
    "notifier-6fbf6f6dc-v89dg",
    "secret-reader-7b79d59f9d-klmfz",
]


def summary(pods, deployments):
    service = InvestigationService.__new__(InvestigationService)
    return InvestigationService._severity_summary(
        service,
        {"problematic_pods": [{"namespace": "payments", "name": name} for name in pods]},
        {},
        {
            "unhealthy_deployments": [
                {"namespace": "payments", "name": name} for name in deployments
            ]
        },
        {},
        {},
        {},
        {},
    )


def test_pods_of_an_unhealthy_deployment_are_not_counted_again():
    assert summary(PODS, DEPLOYMENTS)["affected_workloads"] == 8


def test_a_pod_no_unhealthy_deployment_owns_counts_itself():
    # A bare pod, and a pod of a Deployment that is not among the unhealthy.
    pods = [*PODS, "debug-shell", "healthy-api-54c974858-c684f"]
    assert summary(pods, DEPLOYMENTS)["affected_workloads"] == 10


def test_one_deployment_with_two_failing_replicas_is_not_critical_on_count():
    result = summary(PODS[2:4], ["checkout"])
    assert result["affected_workloads"] == 1
    assert result["severity"] == "High"
    assert result["impact"] != "Production"  # nothing here knows what it serves
