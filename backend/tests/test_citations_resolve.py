"""Every citation in an investigation names a record the investigation holds.

The evidence spine's promise is that a conclusion references an evidence id
rather than copying a payload, and that the id can be followed. For as long as
the dependency graph existed, every edge derived from a pod spec cited
`k8s.pod.spec` — a kind, not an id — because the edge rules read a key
(`evidence_id`) deep entries never carried, and `tests/test_graph.py` built its
fixture with the same wrong key. The console rendered the citation as a chip
reading "Evidence 1" with nothing behind it, on the storage-blocked-pod signal
of a live investigation.

Each half was tested: the graph against a fixture, the console against a
citation index. Nothing followed a citation from a real pipeline run to the
record it names, which is what these do, for signals, hypotheses and edges.
"""

import pytest

from app.analysis.engine import AnalysisEngine
from tests.test_configuration_investigation import CONFIGMAP_REF, ConfigErrorCluster
from tests.test_investigation_service import FakeKubectl, build_service


@pytest.fixture(params=["crash-loop", "config-error"])
async def investigation(request):
    cluster = FakeKubectl() if request.param == "crash-loop" else ConfigErrorCluster(CONFIGMAP_REF)
    return await build_service(cluster).run()


def held(investigation) -> set[str]:
    return {entry["id"] for entry in investigation["evidence"]}


async def test_every_graph_edge_cites_a_held_record(investigation):
    edges = investigation["graph"]["edges"]
    # Vacuity: a run that derived no pod-spec edges cannot show the defect.
    assert any(edge["relation"] in {"owns", "mounts", "reads", "runs_as"} for edge in edges), [
        edge["relation"] for edge in edges
    ]
    unresolved = {
        (edge["source"], edge["relation"], cited)
        for edge in edges
        for cited in edge["evidence_ids"]
        if cited not in held(investigation)
    }
    assert not unresolved, unresolved


async def test_every_signal_and_hypothesis_cites_held_records(investigation):
    analysis = AnalysisEngine().analyze(investigation)
    assert analysis.signals and analysis.hypotheses

    records = held(investigation)
    unresolved = {
        (signal.id, cited)
        for signal in analysis.signals
        for cited in signal.evidence_ids
        if cited not in records
    }
    assert not unresolved, unresolved
    for hypothesis in analysis.hypotheses:
        cited = analysis.evidence_ids_for(hypothesis.supporting_signal_ids)
        assert cited and set(cited) <= records, (hypothesis.id, set(cited) - records)
