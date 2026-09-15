"""Two names for one cluster are recognisable as one cluster.

Read off a console sweep: a kind cluster reached through its kubeconfig
context and through two enrolled agents was "the same failure on 3 clusters,
counted as one incident" on the Fleet page and "3 clusters" on Ask. Enrolling
an agent for a cluster already read through a kubeconfig is the documented
path onto agents, so this is the normal shape of a migration, not a test rig.
"""

import json

from app.kubernetes.kubectl_executor import KubectlResult
from app.services.history_service import InvestigationHistoryService
from app.services.report_store import FilesystemReportStore
from tests.test_configuration_investigation import CONFIGMAP_REF, ConfigErrorCluster, diagnose
from tests.test_investigation_service import NODES

UIDS = ["9b1d6c4e-0000-4000-8000-000000000002", "1f0e2a7c-0000-4000-8000-000000000001"]


class NamedNodes(ConfigErrorCluster):
    def run(self, args, parse_json=False):
        result = super().run(args, parse_json)
        if len(args) > 1 and args[1] == "nodes":
            node = NODES["items"][0]
            payload = {
                "items": [
                    {**node, "metadata": {**node["metadata"], "name": f"node-{i}", "uid": uid}}
                    for i, uid in enumerate(UIDS)
                ]
            }
            return KubectlResult(result.command, True, json.dumps(payload), "", 0, data=payload)
        return result


async def test_the_nodes_a_run_read_reach_the_history_row(tmp_path):
    investigation, diagnosis = await diagnose(NamedNodes(CONFIGMAP_REF))
    assert investigation["cluster_identity"] == {"node_uids": sorted(UIDS)}

    store = FilesystemReportStore(tmp_path)
    item = InvestigationHistoryService(store).save(diagnosis, investigation)
    assert item["node_uids"] == sorted(UIDS)

    # The regenerate path builds its row separately; scope was once lost there.
    InvestigationHistoryService(store).regenerate(item["id"])
    assert store.find(item["id"])["node_uids"] == sorted(UIDS)


async def test_a_run_that_read_no_nodes_claims_no_identity(tmp_path):
    investigation, _ = await diagnose(ConfigErrorCluster(CONFIGMAP_REF))
    assert investigation["cluster_identity"] == {"node_uids": []}
