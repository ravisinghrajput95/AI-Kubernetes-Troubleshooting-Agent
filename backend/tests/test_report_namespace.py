"""Which namespace a report says it is about.

An all-namespaces investigation of the QA cluster concluded "Service has no
ready endpoints (service/payments/checkout-svc)" and was filed, in its own
Executive Summary and in the Reports table, under `local-path-storage` — the
namespace of whichever problematic pod the collector listed first. Its
"Primary namespace affected" line beside it was a set's first element, which
Python orders by a hash it randomises per process.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from app.kubernetes.kubectl_executor import KubectlResult
from app.services.history_service import InvestigationHistoryService
from app.services.report_store import FilesystemReportStore
from tests.test_configuration_investigation import (
    CONFIGMAP_REF,
    PODS,
    ConfigErrorCluster,
    diagnose,
)

BACKEND = Path(__file__).resolve().parents[1]

# A pod in another namespace that sorts, and is listed, first — a local-path
# provisioner that restarted, as on the cluster this was found on.
BYSTANDER = {
    "metadata": {"name": "provisioner-0", "namespace": "aaa-storage"},
    "spec": {"nodeName": "node-1", "containers": [{"name": "p", "image": "p:1"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {
                "name": "p",
                "ready": False,
                "restartCount": 2,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            }
        ],
    },
}


class TwoNamespaces(ConfigErrorCluster):
    def run(self, args, parse_json=False):
        result = super().run(args, parse_json)
        resource = args[1] if len(args) > 1 else ""
        named = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
        if resource == "pod" and named == "provisioner-0":
            detail = {**BYSTANDER, "metadata": {**BYSTANDER["metadata"], "ownerReferences": []}}
            return KubectlResult(result.command, True, json.dumps(detail), "", 0, data=detail)
        if resource in {"pods", "pod"} and not named:
            payload = {"items": [BYSTANDER, *PODS["items"]]}
            return KubectlResult(result.command, True, json.dumps(payload), "", 0, data=payload)
        return result


async def test_the_report_is_filed_under_the_namespace_of_what_it_concluded(tmp_path):
    investigation, diagnosis = await diagnose(TwoNamespaces(CONFIGMAP_REF))

    # Vacuity: the bystander really is listed first, which is what the old
    # derivation read.
    assert investigation["pods"]["problematic_pods"][0]["namespace"] == "aaa-storage"
    assert diagnosis["selected_hypothesis"] == "workload.missing_configuration"

    item = InvestigationHistoryService(FilesystemReportStore(tmp_path)).save(
        diagnosis, investigation
    )
    assert item["namespace"] == "payments"
    report = json.loads((tmp_path / "reports" / f"{item['id']}.json").read_text())
    summary = next(s for s in report["report"]["sections"] if s["title"] == "Executive Summary")
    assert {"label": "Namespace", "value": "payments"} in summary["fields"]


async def test_a_scoped_investigation_is_filed_under_its_scope(tmp_path):
    investigation, diagnosis = await diagnose(TwoNamespaces(CONFIGMAP_REF))
    investigation = {**investigation, "scope": {"namespace": "aaa-storage"}}
    item = InvestigationHistoryService(FilesystemReportStore(tmp_path)).save(
        diagnosis, investigation
    )
    assert item["namespace"] == "aaa-storage"


PRIMARY = """
from app.services.investigation_service import InvestigationService
pods = {"problematic_pods": [
    {"namespace": "alpha"}, {"namespace": "delta"}, {"namespace": "charlie"},
    {"namespace": "bravo"}, {"namespace": "bravo"},
]}
summary = InvestigationService._severity_summary(
    InvestigationService.__new__(InvestigationService), pods, {}, {}, {}, {}, {}, {}
)
print(summary["affected_namespace"])
"""


def test_the_primary_namespace_does_not_depend_on_the_process():
    # Hash randomisation is per process, so only separate processes can show
    # it: four namespaces, one with two workloads, answered under three seeds.
    answers = set()
    for seed in ("1", "2", "3"):
        out = subprocess.run(
            [sys.executable, "-c", PRIMARY],
            cwd=BACKEND,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        answers.add(out.stdout.strip().splitlines()[-1])
    assert answers == {"bravo"}


async def test_the_history_item_records_what_was_asked(tmp_path):
    # The console's `isWholeCluster` reads exactly these keys and values; a
    # history item without them makes every run look whole-cluster, which is
    # how a one-deployment investigation became the fleet card's headline.
    from tests.test_scoped_diagnosis import Namespace, diagnose_scoped

    investigation, diagnosis = await diagnose_scoped(Namespace())
    store = FilesystemReportStore(tmp_path)
    scoped = InvestigationHistoryService(store).save(diagnosis, investigation)
    assert scoped["scope"] == {
        "namespace": "payments",
        "resource_kind": "deployment",
        "resource_name": "checkout",
    }

    whole, whole_diagnosis = await diagnose(TwoNamespaces(CONFIGMAP_REF))
    item = InvestigationHistoryService(store).save(whole_diagnosis, whole)
    assert item["scope"] == {"namespace": "all", "resource_kind": "cluster", "resource_name": ""}

    # Regenerating re-renders from the stored JSON through a second, separate
    # item builder; it must not drop what the live save recorded.
    regenerated = InvestigationHistoryService(store).regenerate(scoped["id"])
    indexed = store.find(scoped["id"])
    assert regenerated is not None and indexed["scope"]["resource_name"] == "checkout"
