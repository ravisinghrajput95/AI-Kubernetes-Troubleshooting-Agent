# Investigation Playbooks

Deep, targeted investigation of a specific failure class — and the loop that
decides when to run one.

Builds on [EVIDENCE_ARCHITECTURE.md](EVIDENCE_ARCHITECTURE.md) and
[REASONING_ARCHITECTURE.md](REASONING_ARCHITECTURE.md).

## A playbook is a planner, not an executor

A playbook never runs `kubectl`. It reads the analysis produced from baseline
evidence and **emits targeted collectors**; the existing scheduler runs them.

That single decision is why playbooks inherit fault isolation, redaction,
concurrency, and budget enforcement rather than reimplementing any of it. A
playbook that returns a broken collector degrades exactly one piece of evidence.

## The loop

```
baseline collect → signals → hypotheses → select playbooks → targeted collect → re-analyze → diagnose
```

Each hypothesis already declares `missing_evidence` — what would confirm or
refute it. Playbooks collect precisely that. The gap the deterministic layer
knew it had becomes the collection plan.

Worked example, from the test fixture:

| | Baseline only | After the crashloop playbook |
|---|---|---|
| Signals | 2 | 6 |
| Root cause | "Application fails on startup and restarts repeatedly" | "Pod references configuration that does not exist" |
| Confidence | 76 | 94 |
| `application_startup_failure` | 65 | **25** — refuted |

The last row matters most. Deep evidence did not merely add confidence; the exit
code and the missing ConfigMap key **refuted** the baseline hypothesis and
replaced it with a specific, actionable one.

## Built-in playbooks

| Playbook | Triggers on | Collects |
|---|---|---|
| `crashloop` | CrashLoopBackOff, BackOff, OOMKilled, container errors | Pod spec (probes, exit codes, limits), previous-container logs, scoped events, ConfigMap/Secret resolution |
| `pending` | Pending, FailedScheduling, stuck creating | Pod scheduling constraints, scheduler events, ResourceQuotas, LimitRanges |
| `imagepull` | ImagePullBackOff, ErrImagePull | Pod spec, pull events, imagePullSecrets, service account credentials |
| `network` | No endpoints, no selector, DNS missing, probe failures | EndpointSlices, NetworkPolicies, Ingresses, CoreDNS health |
| `storage` | Unbound PVC, unavailable PV, mount failures | StorageClasses and binding modes, VolumeAttachments, claim events |

## Two conventions in targeted collectors

**Structured summaries, not raw dumps.** One `get pod -o json` already contains
probes, exit codes, restart counts, limits, and config references.
`PodSpecCollector` extracts them into a compact record. This avoids parsing
`kubectl describe` text — which is not stable across versions — and keeps report
size bounded.

**Secret values are never read.** Referenced Secrets go through
`kubectl describe secret`, which by design prints key names and byte counts but
no values. ConfigMaps use `get -o json`, but the collector emits key names only.
Safe by construction rather than by remembering to redact. A test asserts no
command ever issues `get secret`.

This answers the two diagnostic questions that matter — does the referenced
object exist, and does it contain the referenced key — without the platform ever
holding a credential.

## Signals only reachable with deep evidence

| Signal | Why it matters |
|---|---|
| `container.oom_exit_code` | Exit code 137 **confirms** an OOM kill that pod status only suggested |
| `config.reference_missing` / `config.key_missing` | The container cannot start; usually the actual root cause |
| `container.no_memory_limit` | A restarting container with no limit is evictable |
| `probe.aggressive_timing` | Liveness probe with no startup probe restarts a slow-starting container |
| `scheduling.insufficient_resources` / `scheduling.taint_blocked` | Distinguishes capacity from policy |
| `image.pull_unauthorized` / `image.not_found` | Distinguishes credentials from a wrong tag |
| `quota.exceeded` | Rejection happens before scheduling |
| `storage.no_default_class` | A claim without a class will never bind |
| `network.policy_denies_all` | Traffic dropped before reaching the container |
| `network.dns_workload_unhealthy` | CoreDNS deployed but no ready replica |

Rules that could fire broadly are gated on evidence of the specific failure —
`image.no_pull_secret` only fires when a container is actually failing to pull,
because most pods legitimately have no imagePullSecrets and reporting all of
them would bury real findings.

## Bounds

Deep investigation multiplies cluster reads, so it is bounded on three axes:

- **Targets**: `max_targets` (default 5) per playbook. A cluster-wide failure
  can produce hundreds of matching signals; investigating every one costs budget
  without adding diagnostic value. Targets are chosen most-severe first.
- **Rounds**: `max_rounds` (default 1). Raising it lets a refined hypothesis
  request further evidence; collectors already run are never repeated, so rounds
  converge.
- **Budget**: rounds share the investigation's `CollectionBudget`. A round that
  starts with no remaining deadline is skipped and recorded, not queued.

## Adding a playbook

```python
class NodePressurePlaybook(BasePlaybook):
    id = "node_pressure"
    title = "Node pressure deep investigation"
    triggers = frozenset({SignalType.NODE_PRESSURE})

    def plan(self, context):
        return [ResourceEventsCollector(target) for target in self.targets(context)]
```

Register it in `DEFAULT_PLAYBOOKS`. Then add signal rules over whatever new
evidence it produces, and hypotheses over those signals. Selection, ordering,
concurrency, and deduplication are handled by the orchestrator.

`self.targets(context)` returns the resources named by the triggering signals,
deduplicated, severity-ordered, and capped — this is how a playbook knows *which*
pod to investigate rather than collecting cluster-wide.

## Audit trail

Every investigation records what deep work ran, under `playbook_rounds`:

```json
[{"round": 1, "playbooks": ["crashloop"],
  "collectors": ["k8s.pod.spec:pod/prod/web-0", "..."], "evidence_added": 4}]
```

Targeted payloads land in `deep_evidence`, keyed by evidence kind, each entry
carrying its own evidence id — so a signal derived from one pod's deep evidence
cites that pod's record, not the kind. Baseline evidence is excluded, since it
is already carried in the named investigation sections.

The timeline shows the deep phase as its own section, so an operator can see
that the platform went looking for more.
