"""The dependency graph: what it derives, and what it refuses to invent.

The graph's value is that a diagnosis can rest on a path. That only holds if
the path is real, so most of what is pinned here is what the graph does *not*
do — no node without evidence, no edge to a placeholder, no walk that runs
forever on a cluster that contains a cycle.
"""

import pytest

from app.evidence.models import ResourceRef
from app.graph import ClusterGraph, Edge, Relation, build_graph


def ref(kind: str, name: str, namespace: str | None = None) -> ResourceRef:
    return ResourceRef(kind=kind, name=name, namespace=namespace)


def edge(source: ResourceRef, relation: str, target: ResourceRef) -> Edge:
    return Edge(source=source, relation=relation, target=target, evidence_ids=("e1",))


class TestAnEdgeMustBeTraceable:
    def test_an_edge_without_evidence_is_refused(self):
        """The same rule signals follow. An untraceable edge is an assertion."""
        with pytest.raises(ValueError, match="no evidence"):
            Edge(
                source=ref("Pod", "web-0", "prod"),
                relation=Relation.MOUNTS,
                target=ref("PersistentVolumeClaim", "data", "prod"),
            )

    def test_edges_carry_their_provenance_through_a_round_trip(self):
        graph = ClusterGraph()
        graph.add(edge(ref("Pod", "web-0", "prod"), Relation.SCHEDULED_ON, ref("Node", "node-a")))

        restored = ClusterGraph.from_dict(graph.to_dict())

        assert restored.edges[0].evidence_ids == ("e1",)
        assert restored.edges[0].key == graph.edges[0].key


class TestTraversal:
    def build(self) -> ClusterGraph:
        graph = ClusterGraph()
        pod = ref("Pod", "web-0", "prod")
        claim = ref("PersistentVolumeClaim", "data", "prod")
        volume = ref("PersistentVolume", "pv-1")
        storage_class = ref("StorageClass", "fast")

        graph.add(edge(ref("Deployment", "web", "prod"), Relation.OWNS, pod))
        graph.add(edge(pod, Relation.SCHEDULED_ON, ref("Node", "node-a")))
        graph.add(edge(pod, Relation.MOUNTS, claim))
        graph.add(edge(claim, Relation.BINDS, volume))
        graph.add(edge(claim, Relation.PROVISIONED_BY, storage_class))
        return graph

    def test_depends_on_reaches_the_far_end_of_the_chain(self):
        """Pod → claim → class is the traversal §3.6 is written around."""
        reached = {e.target.key for e in self.build().depends_on("pod/prod/web-0")}

        assert "storageclass/_cluster/fast" in reached
        assert "persistentvolume/_cluster/pv-1" in reached
        assert "node/_cluster/node-a" in reached

    def test_dependents_answers_what_breaks_if_this_goes(self):
        reached = {e.source.key for e in self.build().dependents("storageclass/_cluster/fast")}

        assert "persistentvolumeclaim/prod/data" in reached

    def test_depth_is_bounded(self):
        """An unbounded walk on a large cluster is a request that never returns."""
        shallow = self.build().depends_on("pod/prod/web-0", max_depth=1)

        assert {e.relation for e in shallow} == {Relation.SCHEDULED_ON, Relation.MOUNTS}

    def test_zero_depth_walks_nothing(self):
        assert self.build().depends_on("pod/prod/web-0", max_depth=0) == []

    def test_a_cycle_terminates(self):
        """Kubernetes permits them — a ConfigMap written by the pod that reads it."""
        graph = ClusterGraph()
        first = ref("Pod", "a", "prod")
        second = ref("ConfigMap", "b", "prod")
        graph.add(edge(first, Relation.READS, second))
        graph.add(edge(second, Relation.READS, first))

        walked = graph.depends_on("pod/prod/a", max_depth=10)

        assert len(walked) <= 4  # not unbounded

    def test_an_unknown_key_reaches_nothing(self):
        assert self.build().depends_on("pod/prod/absent") == []

    def test_a_duplicate_edge_is_recorded_once(self):
        """Two collectors can both observe a pod's node; the graph says it once."""
        graph = ClusterGraph()
        for _ in range(3):
            graph.add(
                edge(ref("Pod", "web-0", "prod"), Relation.SCHEDULED_ON, ref("Node", "node-a"))
            )

        assert len(graph.edges) == 1


class TestDerivationFromEvidence:
    def test_pods_are_linked_to_their_nodes(self):
        graph = build_graph(
            {
                "pods": {
                    "pod_inventory": [
                        {"name": "web-0", "namespace": "prod", "node": "node-a"},
                    ]
                }
            }
        )

        assert graph.neighbours("pod/prod/web-0", Relation.SCHEDULED_ON)[0].name == "node-a"

    def test_an_unscheduled_pod_is_not_placed_on_a_node_called_pending(self):
        """`Pending` is the inspector's placeholder, not a node.

        An edge to it would be a fiction that "what is on node Pending" could
        then be asked about.
        """
        graph = build_graph(
            {"pods": {"pod_inventory": [{"name": "web-0", "namespace": "prod", "node": "Pending"}]}}
        )

        assert graph.edges == []

    def test_a_claim_with_no_class_is_not_linked_to_one_called_none(self):
        graph = build_graph(
            {
                "storage": {
                    "claims": [
                        {"name": "data", "namespace": "prod", "storage_class": "none", "volume": ""}
                    ]
                }
            }
        )

        assert graph.edges == []

    def test_the_storage_chain_is_derived(self):
        graph = build_graph(
            {
                "storage": {
                    "claims": [
                        {
                            "name": "data",
                            "namespace": "prod",
                            "storage_class": "fast",
                            "volume": "pv-1",
                        }
                    ]
                }
            }
        )

        relations = {e.relation for e in graph.out_edges("persistentvolumeclaim/prod/data")}
        assert relations == {Relation.BINDS, Relation.PROVISIONED_BY}

    def test_services_are_linked_to_the_pods_their_selector_matches(self):
        graph = build_graph(
            {
                "network": {"selectors": {"shop/checkout": {"app": "checkout"}}},
                "pods": {
                    "pod_inventory": [
                        {"name": "checkout-0", "namespace": "shop", "labels": {"app": "checkout"}},
                        {"name": "other-0", "namespace": "shop", "labels": {"app": "other"}},
                        # Right labels, wrong namespace: a selector is namespaced.
                        {"name": "checkout-9", "namespace": "prod", "labels": {"app": "checkout"}},
                    ]
                },
            }
        )

        selected = [e.target.key for e in graph.out_edges("service/shop/checkout")]
        assert selected == ["pod/shop/checkout-0"]

    def test_deep_evidence_adds_volumes_and_owners(self):
        graph = build_graph(
            {
                "deep_evidence": {
                    "k8s.pod.spec": [
                        {
                            "evidence_id": "k8s.pod.spec:pod/prod/web-0",
                            "data": {
                                "pod": "web-0",
                                "namespace": "prod",
                                "service_account": "web",
                                "owner": {"workload_kind": "Deployment", "workload_name": "web"},
                                "volumes": [
                                    {"type": "PersistentVolumeClaim", "claim": "data"},
                                    {"type": "ConfigMap", "name_ref": "web-config"},
                                ],
                            },
                        }
                    ]
                }
            }
        )

        assert {e.relation for e in graph.out_edges("pod/prod/web-0")} == {
            Relation.MOUNTS,
            Relation.READS,
            Relation.RUNS_AS,
        }
        # Ownership points the way ownerReferences do, so "what does this
        # deployment own" is a forward walk like every other relation.
        assert graph.neighbours("deployment/prod/web", Relation.OWNS)[0].name == "web-0"

    def test_an_empty_investigation_yields_an_empty_graph(self):
        graph = build_graph({})

        assert graph.edges == []
        assert graph.to_dict()["counts"] == {"nodes": 0, "edges": 0}

    def test_one_broken_rule_costs_one_relation(self):
        """Same fault isolation as the signal loop: a partial graph beats none."""
        from app.graph.edge_rules import EdgeRule

        def explode(_data):
            raise RuntimeError("bad payload")
            yield  # pragma: no cover

        def fine(_data):
            yield edge(ref("Pod", "web-0", "prod"), Relation.SCHEDULED_ON, ref("Node", "node-a"))

        graph = build_graph(
            {},
            rules=[EdgeRule("broken", "x", explode), EdgeRule("fine", "y", fine)],
        )

        assert len(graph.edges) == 1


class TestTheGraphIsReproducible:
    def test_a_stored_graph_rebuilds_identically(self):
        """A report is not a lossy snapshot: the graph an operator opens six
        weeks later is the one the diagnosis was made against."""
        original = build_graph(
            {
                "pods": {
                    "pod_inventory": [{"name": "web-0", "namespace": "prod", "node": "node-a"}]
                },
                "storage": {
                    "claims": [
                        {
                            "name": "data",
                            "namespace": "prod",
                            "storage_class": "fast",
                            "volume": "pv-1",
                        }
                    ]
                },
            }
        )

        restored = ClusterGraph.from_dict(original.to_dict())

        assert restored.to_dict()["edges"] == original.to_dict()["edges"]
        assert restored.to_dict()["counts"] == original.to_dict()["counts"]

    def test_an_edge_referencing_an_unknown_node_is_dropped_not_invented(self):
        restored = ClusterGraph.from_dict(
            {
                "nodes": [
                    {"key": "pod/prod/web-0", "kind": "Pod", "name": "web-0", "namespace": "prod"}
                ],
                "edges": [
                    {
                        "source": "pod/prod/web-0",
                        "relation": "mounts",
                        "target": "persistentvolumeclaim/prod/ghost",
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        )

        assert restored.edges == []
