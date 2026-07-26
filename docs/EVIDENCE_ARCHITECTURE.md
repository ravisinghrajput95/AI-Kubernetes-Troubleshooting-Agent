# Evidence and Collection Architecture

This document describes the foundation layer that investigations are built on:
addressable evidence, a pluggable collector graph, and the guarantees the
scheduler enforces. It is the substrate for playbooks, hypothesis-driven
investigation, and additional evidence backends.

## Why this layer exists

The original pipeline collected evidence into one flat, untyped dictionary. That
worked, but it could not support three things the platform requires:

- **Explainability.** A conclusion cannot cite evidence that has no identity.
- **Reliability.** One inspector raising an unexpected exception aborted the
  entire investigation.
- **Extensibility.** Ordering was a hardcoded call sequence, so every new
  inspection path meant editing the orchestrator.

## Core concepts

### Evidence

Every fact is an `Evidence` record (`app/evidence/models.py`) carrying a
deterministic id, the command that produced it, timing, and a status.

```python
Evidence.create(
    kind=EvidenceKind.PODS,
    status=EvidenceStatus.OK,
    target=ResourceRef(kind="Pod", name="web-0", namespace="prod"),
    data=payload,
    collector_id="k8s.pods",
)
# id == "k8s.pods:pod/prod/web-0"
```

Ids are deterministic rather than random so the same fact carries the same
identifier across investigations, and so a diagnosis can reference evidence by
id instead of duplicating its payload.

### Status is data, not an exception

| Status | Meaning |
|---|---|
| `ok` | Collected successfully |
| `empty` | Collected successfully, nothing to report |
| `unavailable` | Backend absent (no metrics-server, Prometheus down) |
| `forbidden` | RBAC denied the read |
| `timeout` | Exceeded the collector or investigation budget |
| `not_applicable` | Skipped — out of scope, or a dependency was missing |
| `failed` | Unexpected error inside the collector |

`ok` and `empty` are *usable*. Everything else records **why** a fact is
missing, which is what lets a diagnosis state "metrics were not consulted
because metrics-server is unavailable" rather than silently reasoning over a
gap. `EvidenceStore.coverage()` aggregates this into a completeness percentage.

## The collector graph

A collector declares what it produces and what it needs:

```python
class PodLogsCollector(BaseCollector):
    id = "k8s.pods.logs"
    provides = frozenset({EvidenceKind.POD_LOGS})
    requires = frozenset({EvidenceKind.PODS})
```

- `requires` is **hard**. No registered provider is a wiring error raised at
  resolve time, not a silent gap at runtime.
- `optional_requires` is **soft**. It orders the collector after a provider when
  one is registered, and is ignored otherwise — this is how optional backends
  (Prometheus, Loki) can be absent without breaking the graph.

`CollectorRegistry.resolve()` topologically sorts collectors into waves. The
built-in graph resolves to two waves: everything independent runs concurrently,
then pod logs run once problematic pods are known.

## Scheduler guarantees

`CollectionScheduler` holds three invariants regardless of collector behavior:

1. **Fault isolation.** A collector that raises, hangs, or overruns its budget
   degrades only its own evidence. Other collectors complete normally.
2. **Total accounting.** Every declared evidence kind appears in the store, worst
   case as a non-usable record naming the reason. Nothing disappears silently.
3. **Redaction at the boundary.** Payloads are scrubbed before entering the
   store, so persisted reports, the HTTP API, and the LLM all observe the same
   redacted data. A future consumer cannot bypass it.

Point 3 closed a real leak: redaction previously ran only when building the LLM
prompt, so raw log lines were written to `data/investigations/reports/*.json`
and served over HTTP in plaintext.

## Read-only enforcement

`app/kubernetes/command_policy.py` allowlists kubectl verbs and the sub-verbs of
mixed commands such as `config`. `KubectlExecutor.run()` validates every call, so
a mutating command cannot be introduced by a new collector. Violations raise
`UnsafeKubectlCommand`, which the scheduler contains as failed evidence.

## Adding a collector

1. Pick or add an evidence kind in `EvidenceKind`.
2. Subclass `BaseCollector`, declaring `id`, `provides`, and any dependencies.
3. Return evidence — never raise for expected failures; return a non-usable
   status with a `detail` explaining the gap.
4. Register it in `build_default_collectors()`.

```python
class NetworkPolicyCollector(BaseCollector):
    id = "k8s.networkpolicies"
    provides = frozenset({EvidenceKind.NETWORK_POLICIES})
    optional_requires = frozenset({EvidenceKind.NETWORK})

    async def collect(self, context):
        args = ["get", "networkpolicies", "-A", "-o", "json"]
        result = await asyncio.to_thread(context.kubectl.run, args, True)

        if not result.success:
            status, detail = classify_error(result.stderr)
            return [Evidence.create(
                kind=EvidenceKind.NETWORK_POLICIES,
                status=status,
                target=context.scope.cluster_ref,
                detail=detail,
                command=" ".join(result.command),
                collector_id=self.id,
            )]

        items = result.data.get("items", [])
        return [Evidence.create(
            kind=EvidenceKind.NETWORK_POLICIES,
            status=EvidenceStatus.OK if items else EvidenceStatus.EMPTY,
            target=context.scope.cluster_ref,
            data={"items": items},
            command=" ".join(result.command),
            collector_id=self.id,
        )]
```

No orchestrator changes are required — the scheduler picks up ordering and
concurrency from the declaration.

## Budgets

`CollectionBudget` bounds an investigation: `max_concurrency` (default 8),
`per_collector_timeout` (60s), and `total_deadline` (240s). Collectors that
cannot start before the deadline are recorded as timed out rather than dropped,
so a large cluster degrades into a partial-but-honest investigation instead of
stalling.

## Adapted, not rewritten

The inspectors under `app/kubernetes/` are unchanged. `LegacyInspectorCollector`
runs each synchronous inspector off the event loop and maps its established
`{"error": ...}` contract onto evidence status via `app/kubernetes/errors.py`.
Their payload shapes are preserved verbatim, which is why the API response and
the frontend required no changes.
