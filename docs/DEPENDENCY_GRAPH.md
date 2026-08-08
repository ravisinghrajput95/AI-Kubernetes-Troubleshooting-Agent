# The cluster dependency graph

Backlog item 49. `investigation.graph` is part of every investigation result and
was not documented anywhere a consumer would look — in particular the `relation`
field, which is the only thing that makes an edge interpretable.

Not to be confused with the **collector** graph in
`docs/EVIDENCE_ARCHITECTURE.md`, which is the dependency ordering between
collectors. This is the graph of the customer's cluster.

## Shape

```json
{
  "graph": {
    "nodes": [
      {"key": "pod/prod/api-0", "kind": "Pod", "name": "api-0", "namespace": "prod"}
    ],
    "edges": [
      {
        "source": "pod/prod/api-0",
        "relation": "scheduled_on",
        "target": "node/_cluster/node-b",
        "evidence_ids": ["k8s.pods:cluster/_cluster/prod-east"]
      }
    ]
  }
}
```

`source` and `target` are node **keys**, not objects — join them against
`nodes`. A key is `kind/namespace/name` lowercased, with `_cluster` standing in
for the namespace of a cluster-scoped object.

## `relation` — a closed, directional set

**Direction is a contract:** `A -[relation]-> B` always reads "A depends on B"
or "A is placed on B". Keeping it consistent is what makes "what does this pod
depend on" a forward walk and "what breaks if this node goes" a reverse one,
rather than two special cases in every consumer.

| `relation` | Reads as | Typical source → target |
|---|---|---|
| `owns` | A owns B, in the direction `ownerReferences` point | Deployment → ReplicaSet → Pod |
| `scheduled_on` | A is placed on B | Pod → Node |
| `mounts` | A mounts B | Pod → PersistentVolumeClaim |
| `binds` | A binds B | PersistentVolumeClaim → PersistentVolume |
| `provisioned_by` | A is provisioned by B | PersistentVolumeClaim → StorageClass |
| `reads` | A reads configuration from B | Pod → ConfigMap, Pod → Secret |
| `selects` | A selects B | Service → Pod |
| `routes_to` | A routes to B | Ingress → Service |
| `runs_as` | A runs under identity B | Pod → ServiceAccount |

The set is **closed**. A consumer may switch on it exhaustively; a value not in
this table is a bug, not an extension point. Adding one is a change to
`app/graph/models.py::Relation` and to this table together.

## `evidence_ids` is mandatory

Every edge names the evidence it was read from, and an edge constructed without
any **raises**. That is the same rule signals follow: a dependency a diagnosis
rests on has to be defensible, and an edge with no provenance cannot be.

Graph-derived signals cite **every edge walked**, not just the destination —
so a finding like "this Pending pod is blocked by that claim, and that claim's
class is blocking others too" can be traced hop by hop.

## Two properties worth relying on

**No rule invents a node.** An edge is emitted only when *both* ends were
actually observed. So "depends on a ConfigMap we could not see" and "depends on
nothing" are different states in the data rather than the same absence — which
is what lets a traversal distinguish a missing dependency from no dependency.

Placeholders are refused explicitly: a pod whose `nodeName` reads `Pending` is
not placed on a node called `Pending`, and a claim whose class reads `none` is
not linked to a StorageClass called `none`.

**The graph is derived from evidence, not emitted by collectors.** It is
reproducible from a stored report, inherits redaction and fault isolation, and
adds no collection path on which the local and agent routes could diverge.
`POST /investigations/{id}/regenerate` rebuilds it without touching the cluster.

## Traversal

`ClusterGraph.depends_on(key)` and `.dependents(key)` are breadth-first,
**depth-limited to 5**, and cycle-safe. Kubernetes permits cycles — a ConfigMap
referenced by the pod that generates it — so the limit and the visited set are
correctness requirements, not tuning.

`neighbours(key, relation="")` and `sources(key, relation="")` give one hop,
optionally filtered to a single relation.

> A bug worth knowing about, since it produced *plausible* output rather than an
> error: the traversal direction was once selected with `step is self.out_edges`,
> which is always `False` because attribute access builds a new bound method
> each time. Every forward traversal stopped after one hop and still returned a
> sensible-looking answer. If you extend traversal, compare something other than
> bound-method identity.

## Size

Bounded by construction: the node count is the number of objects the
investigation actually collected, which `MAX_LIST_ITEMS` and the collection
budget already cap. At the 2,000-pod ceiling the graph is ~18% of a 2.7 MB
stored result — the third-largest section, after signals and pods.
