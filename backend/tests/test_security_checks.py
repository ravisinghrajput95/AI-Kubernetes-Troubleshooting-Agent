"""Security checks name what was checked, and are matched on by id.

The labels read as outcomes — "No Privileged Containers", "High CVEs Found" —
whatever the status, so the console listed a warning titled "High CVEs Found"
for a deployment with no vulnerability scanner at all. Renaming them is only
safe because `ResourceLimitsRule` matches the check's id; it used to match the
label text, and the two lived in different modules with nothing joining them.
"""

from app.analysis.engine import AnalysisEngine
from app.analysis.models import SignalType
from tests.test_configuration_investigation import CONFIGMAP_REF, ConfigErrorCluster, diagnose

OUTCOME_WORDS = ("found", "no ", "missing", "used")


async def test_labels_name_the_check_and_the_rule_still_reads_it():
    # The config-error fixture's pod has no CPU limit, so the limits check warns.
    investigation, _ = await diagnose(ConfigErrorCluster(CONFIGMAP_REF))
    findings = investigation["security"]["findings"]

    for finding in findings:
        assert finding["id"]
        assert not any(word in finding["label"].lower() for word in OUTCOME_WORDS), finding

    limits = next(f for f in findings if f["id"] == "resource_limits")
    assert limits["status"] == "warning"
    types = {signal.type for signal in AnalysisEngine().analyze(investigation).signals}
    assert SignalType.CONTAINER_MISSING_LIMITS in types


async def test_a_check_that_did_not_run_does_not_claim_a_result():
    investigation, _ = await diagnose(ConfigErrorCluster(CONFIGMAP_REF))
    unchecked = [f for f in investigation["security"]["findings"] if f["status"] == "unknown"]
    assert unchecked
    for finding in unchecked:
        assert finding["detail"].startswith("Not checked")


def test_a_report_stored_before_the_rename_still_produces_the_signal():
    from app.analysis.signal_rules import AnalysisInput, ResourceLimitsRule

    stored = {
        "evidence": [{"id": "k8s.pods:cluster/_cluster/t", "kind": "k8s.pods"}],
        "security": {
            "findings": [
                {"label": "Missing Resource Limits", "status": "warning", "detail": "1 container"}
            ]
        },
    }
    assert ResourceLimitsRule().extract(AnalysisInput(stored))
