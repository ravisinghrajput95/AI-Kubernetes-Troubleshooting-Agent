# Enterprise Platform Architecture

**Status:** Proposed · **Audience:** Engineering leadership, platform architects
**Scope:** Deployment and platform architecture. Investigation *functionality* is
not redesigned.

---

## 0. Corrections to the stated starting point

Two capabilities in the brief are not present in the repository. An architecture
that assumed them would plan migrations for components that do not exist, so
they are corrected here before anything is designed.

**There is no knowledge graph.** The only "graph" in the codebase is
`CollectorGraphError`, which belongs to the collector dependency DAG.
`_cluster_topology()` produces a flat dictionary of pods grouped by node — no
edges, no relationships, no traversal. A knowledge graph is *designed* in this
document (§3.6) as new work, not migrated.

**There is no multi-agent investigation.** No agent abstraction exists. During
the original build, LLM-per-domain agents were considered and deliberately
rejected: they multiply cost, latency and hallucination surface in a system whose
core promise is that it does not fabricate. The same auditability was achieved
with deterministic per-domain signal rules and a single grounded reasoning call.
That decision still holds and §3.4 explains why. Note the word "agent" in this
brief means something entirely different — a *cluster connector* — and that
naming collision is worth eliminating early.

**One assumption is wrong in the platform's favour.** The brief states the
implementation "likely has tight coupling between investigation logic and
Kubernetes access." It does not. Measured:

| Layer | Imports `KubectlExecutor` |
|---|---|
| `app/analysis/`, `app/ai/`, `app/remediation/`, `app/reports/`, `app/jobs/` | **zero** |
| `app/collectors/base.py` | one field: `CollectionContext.kubectl` |
| `app/collectors/{kubernetes,targeted}.py` | via that field |
| `app/kubernetes/*` (9 legacy inspectors + executor + context service) | direct |

The reasoning stack references the string `kubectl` only when *generating
commands for humans to read*. It never executes anything.

This is the single most consequential finding in this review: **the abstraction
the brief asks for is a substitution at one field, not a refactor of the
investigation engine.** The migration is therefore far cheaper than the brief
anticipates, and §9 is scoped accordingly.

---

## 1. Executive summary

The platform today is a single-process application that shells out to `kubectl`
against whatever kubeconfig it holds. It is architecturally sound *inside* —
evidence is addressable, reasoning is deterministic-first and grounded, cluster
access is read-only by construction — and architecturally absent *outside*: no
fleet concept, no tenancy, no horizontal scale, no remote clusters.

The proposal is to keep the inside almost entirely intact and build a fleet
around it.

Three decisions carry the design:

**1. The connection direction, not the protocol, is the architectural
constraint.** No bank will open an inbound firewall port to a production cluster
so a SaaS platform can call in. Every agent must dial *outbound* to the platform
and receive work over that connection. This eliminates request/response REST
between platform and agent regardless of its other merits, and is the reason
§7 recommends gRPC bidirectional streaming.

**2. The read-only guarantee must move into the agent.** Today it is enforced in
the platform's executor. Once the platform is remote and multi-tenant, that
becomes a promise rather than a control. The agent must independently refuse any
mutating verb, so a compromised platform, a malicious tenant, or a bug cannot
mutate a customer cluster. The same allowlist ships on both sides.

**3. Evidence is the contract.** The `Evidence` record — addressable id, status,
originating command — already exists and is already the boundary between
collection and reasoning. Promoting it to the wire format between agent and
platform means the investigation engine changes almost not at all, and any future
source (a cloud API, a service mesh, a CMDB) becomes another producer of the same
record.

Target scale is 1,000 clusters and 5,000 concurrent investigations. The binding
constraint is not connection count — 1,000 long-lived streams is unremarkable —
but **evidence throughput and investigation state**, both of which must leave
process memory.

---

## 2. Current architecture assessment

### Strengths worth preserving

These are not incidental and should survive the migration unchanged.

- **Evidence spine.** Deterministic ids (`kind:target.key`), typed status, the
  originating command retained. Every conclusion is traceable to a command.
- **Degradation as data.** `unavailable`, `forbidden`, `timeout`,
  `not_applicable` are citable facts. A diagnosis can state what it could not
  see. This is rare and is a genuine differentiator.
- **Read-only by construction.** An allowlist on the executor, not a convention.
- **Deterministic-first reasoning.** Signals and hypotheses are produced by rules
  before any model call. The model selects and explains; it never diagnoses from
  raw JSON, and never produces a command.
- **Grounding.** Model output is rejected if citations do not resolve *or* if the
  prose contradicts what it cites.
- **Fault-isolated collection.** One collector failing degrades only its own
  evidence.
- **Playbook-as-planner.** Playbooks emit collectors; the scheduler runs them, so
  they inherit isolation, redaction, concurrency and budgets.

### Weaknesses

| Weakness | Consequence at fleet scale |
|---|---|
| Single process, in-memory job store | No HA, no horizontal scale, jobs lost on restart |
| No fleet or cluster registry | Cannot address 1,000 clusters |
| Tenancy is per-user ownership only | No organisation, no data isolation, unsellable to regulated customers |
| Investigation state in memory | Cannot survive a pod restart or be shared across replicas |
| Reports and evidence on local disk | Not durable, not shared, unbounded growth |
| `kubectl` subprocess per call | ~20 forks per investigation; will not sustain 5,000 concurrent |
| Peak memory ∝ cluster size | kubectl assembles whole lists before writing |
| No platform self-observability | An observability product that cannot observe itself |
| Single LLM provider | Blocks customers who cannot send cluster data to a third party |

### Technical debt

Carried forward and tracked in `PRODUCTION_READINESS.md`: `App.tsx` remains a
~1,500-line component; `history_service.py` does persistence *and* PDF rendering;
`FixRecommendationEngine` is largely vestigial since hypothesis-driven
remediation landed; nine legacy inspectors are adapted rather than native
collectors.

None of these block the migration. The legacy inspectors in particular are
**contained by design** — they sit behind `LegacyInspectorCollector` and will be
migrated to the provider interface one at a time.

---

## 3. Target architecture

```
                        ┌──────────────────────────────┐
   Browser / CLI ──────►│  Edge: API Gateway + OIDC    │
   (REST + SSE)         │  rate limit, tenant routing  │
                        └───────────────┬──────────────┘
                                        │
                ┌───────────────────────┼───────────────────────┐
                │                       │                       │
        ┌───────▼───────┐      ┌────────▼────────┐    ┌─────────▼────────┐
        │ Investigation │      │   Fleet API     │    │  Admin / IAM     │
        │      API      │      │ clusters, agents│    │ orgs, RBAC, keys │
        └───────┬───────┘      └────────┬────────┘    └──────────────────┘
                │                       │
        ┌───────▼───────────────────────▼───────┐
        │        Investigation Orchestrator      │◄─── scheduled + event-driven
        │  lifecycle, budgets, playbook rounds   │
        └───────┬───────────────────────┬───────┘
                │                       │
        ┌───────▼────────┐     ┌────────▼─────────┐
        │ Evidence Bus   │     │ Reasoning Workers│
        │ (queue/stream) │     │ signals→hypoth.  │
        └───────┬────────┘     │ →grounded LLM    │
                │              └────────┬─────────┘
        ┌───────▼────────┐              │
        │ Agent Gateway  │     ┌────────▼─────────┐   ┌──────────────┐
        │ gRPC, mTLS     │     │ Knowledge Graph  │   │Report Engine │
        │ stateless      │     └──────────────────┘   └──────────────┘
        └───────┬────────┘
                │  agent-initiated, outbound-only, bidi stream
    ┌───────────┼───────────┬───────────────┐
    │           │           │               │
┌───▼───┐  ┌────▼───┐  ┌────▼───┐      ┌────▼────┐
│Agent  │  │Agent   │  │Agent   │  ... │Agent    │      (1 per cluster)
│ EKS   │  │ AKS    │  │ GKE    │      │on-prem  │
└───┬───┘  └────┬───┘  └────┬───┘      └────┬────┘
    │           │           │               │
  K8s API   Prometheus    Loki          K8s API
```

Persistence: **PostgreSQL** (tenants, clusters, investigations, graph,
history) · **Redis** (job queue, leases, cache, pub/sub for SSE fan-out) ·
**Object storage** (evidence payloads, rendered reports).

### 3.1 Cluster Agent

A deliberately small binary, one per customer cluster. Its entire job is to turn
an evidence *request* into an evidence *record*.

**Does:** Kubernetes API reads · Prometheus and Loki queries · event streaming ·
resource discovery · **read-only policy enforcement** · **RBAC impersonation of
the requesting user** · redaction at source · local rate limiting and budgets.

**Does not:** AI, prompts, knowledge graph, reports, scheduling, database,
history, timeline generation, investigation logic.

Two responsibilities in that list deserve defending, because the brief excludes
"business logic" and these could be mistaken for it:

- **Read-only enforcement is a security control, not business logic.** It must be
  local, or the guarantee depends on a remote system the customer does not
  operate.
- **Redaction must happen at source.** A secret that leaves the cluster has left
  the cluster. Scrubbing centrally means the raw value already crossed a network
  boundary and may sit in a log or a queue.

The agent uses the **Kubernetes API directly** (client-go or the Python client),
not `kubectl`. This removes subprocess-per-call, enables server-side paging with
bounded memory, and permits watch semantics. The audit trail — today
`executed_commands` — becomes a structured *equivalent command* string generated
for human replay rather than for execution.

### 3.2 Agent Gateway

Stateless. Terminates mTLS, authenticates the agent certificate, resolves it to a
tenant and cluster, and bridges the bidirectional stream to the Evidence Bus.
Holds no investigation state, so it scales horizontally and any gateway can serve
any agent. Roughly 10k concurrent streams per replica; 1,000 clusters needs one
replica and runs three for availability.

### 3.3 Investigation Orchestrator

Owns the lifecycle that `InvestigationOrchestrator` owns today — baseline
collection, analysis, playbook selection, targeted collection, re-analysis —
but dispatches through the Evidence Bus instead of calling a scheduler in
process. State lives in Postgres; the in-memory `InvestigationJobStore` becomes a
persistence adapter behind the same interface.

### 3.4 Reasoning Workers

Stateless consumers running the existing `analysis`, `ai` and `remediation`
packages **unchanged**. This is the layer that already has zero Kubernetes
coupling, so it lifts and shifts.

*On multi-agent reasoning:* the recommendation remains a single grounded call
over a deterministic signal set, not an LLM per domain. Per-domain LLM agents
multiply cost and latency by the number of domains and multiply hallucination
surface by the same factor, in a product whose differentiator is that it does not
fabricate. The auditability people want from multi-agent — "which domain
contributed what" — is already provided by per-domain signal rules and the
evidence citation chain, deterministically and for free. If genuine agentic
behaviour is wanted later, the correct place is **adaptive collection** (an agent
choosing what evidence to gather next), which the hypothesis layer already sets
up, not parallel diagnosis.

### 3.5 Provider layer

```
Investigation Engine  →  ClusterProvider (protocol)
                              ├── RemoteAgentProvider   (fleet)
                              ├── LocalKubectlProvider  (today; dev, single-cluster)
                              └── ReplayProvider        (tests, evals, incident replay)
```

`ReplayProvider` is not decoration. It makes every investigation reproducible
from stored evidence, which is what turns the eval corpus (`docs/EVALUATION.md`)
into a fleet-scale regression asset and lets support re-run a customer incident
without touching their cluster.

### 3.6 Knowledge Graph — new work

Built as a **byproduct of collection**, not a separate ingestion pipeline:
collectors already fetch `ownerReferences`, selectors, volume claims and node
assignments. Edges are emitted alongside evidence.

```
Deployment → ReplicaSet → Pod → Node
                           ├──→ PersistentVolumeClaim → PersistentVolume → StorageClass
                           ├──→ ConfigMap / Secret
                           └──← Service ← Ingress
```

**Recommendation: PostgreSQL with recursive CTEs, not a graph database.** The
traversals required — "what does this pod depend on", "what is affected if this
node drains" — are bounded at three to five hops over a graph of thousands of
nodes per cluster. That is comfortably within Postgres. Introducing Neo4j or
similar adds an operational component, a second consistency model and a licence
conversation to every enterprise sale, in exchange for traversal depth this
workload does not need. Revisit only if cross-cluster, cross-time traversal
becomes a product requirement.

### 3.7 Integration surfaces

Two distinct northbound surfaces, deliberately separated:

- **Event ingress** (ArgoCD, Flux, Falco, Kyverno, alertmanager) — signed
  webhooks that *trigger* investigations. This is what turns the product from
  human-invoked to autonomous.
- **Action egress** (ServiceNow, PagerDuty, Slack, Teams, Jira, GitHub) —
  notification and ticketing, behind one outbound interface.

**MCP belongs here, not on the agent link.** Exposing the platform's evidence and
investigation capability as MCP tools lets a customer's own AI agents consume it.
That is a valuable product surface and a poor fleet transport (§7).

---

## 4. Component responsibilities

| Service | Owns | State | Scaling |
|---|---|---|---|
| API Gateway | TLS, OIDC, rate limit, tenant routing | none | horizontal |
| Investigation API | Submit, query, stream, reports | none | horizontal |
| Fleet API | Cluster registry, agent lifecycle, health | Postgres | horizontal |
| Admin/IAM | Orgs, users, roles, API keys, audit | Postgres | horizontal |
| Orchestrator | Lifecycle, budgets, playbook rounds | Postgres + Redis lease | horizontal, leased |
| Agent Gateway | mTLS, agent identity, stream bridging | none | horizontal |
| Evidence Bus | Request dispatch, evidence ingest | Redis / NATS | partitioned |
| Reasoning Workers | Signals, hypotheses, grounded LLM, remediation | none | horizontal, queue depth |
| Knowledge Graph | Relationship persistence and traversal | Postgres | read replicas |
| Report Engine | PDF, Markdown, JSON composition | Object storage | horizontal |
| Notifier | Outbound integrations | Redis queue | horizontal |
| Cluster Agent | Cluster access, policy, impersonation, redaction | none | one per cluster |

---

## 5. API contracts

### 5.1 Agent ↔ Platform (gRPC, agent-initiated)

```protobuf
service AgentGateway {
  // Long-lived, agent-dialled. Platform pushes work down; agent streams
  // evidence up. One stream carries many concurrent investigations.
  rpc Connect(stream AgentMessage) returns (stream PlatformMessage);

  // Separate unary call: bootstrap token exchanged for an mTLS certificate.
  rpc Register(RegistrationRequest) returns (RegistrationResponse);
}

message PlatformMessage {
  oneof payload {
    CollectionRequest  collect   = 1;   // gather this evidence
    CancelRequest      cancel    = 2;
    CapabilityQuery    capability= 3;   // what APIs/backends do you have
    ConfigUpdate       config    = 4;   // budgets, sampling, log level
    Heartbeat          heartbeat = 5;
  }
}

message AgentMessage {
  oneof payload {
    AgentHello      hello     = 1;   // version, capabilities, cluster identity
    EvidenceRecord  evidence  = 2;   // streamed as produced, not batched
    CollectionDone  done      = 3;
    AgentHealth     health    = 4;
    ClusterEvent    event     = 5;   // watch-driven, for autonomous triggers
  }
}

message CollectionRequest {
  string investigation_id = 1;
  string request_id       = 2;
  repeated EvidenceSpec specs = 3;   // WHAT is needed, never HOW
  Impersonation actor     = 4;       // user + groups; agent applies their RBAC
  Budget budget           = 5;       // deadline, max items, max bytes
}

message EvidenceSpec {
  string kind      = 1;              // "k8s.pods", "prometheus.pod.metrics"
  ResourceRef target = 2;
  map<string, string> parameters = 3;
}

message EvidenceRecord {            // mirrors app/evidence/models.py
  string id = 1; string kind = 2; string source = 3;
  Status status = 4;                 // ok|empty|unavailable|forbidden|timeout|
                                     // not_applicable|failed
  ResourceRef target = 5;
  bytes payload = 6;                 // redacted at source; JSON or protobuf
  string equivalent_command = 7;     // for the human audit trail
  string detail = 8;                 // why, when not usable
  int64 duration_ms = 9;
  google.protobuf.Timestamp collected_at = 10;
}
```

The critical property: `EvidenceSpec` says **what** evidence is wanted. It never
carries a command. The platform cannot instruct an agent to run something
arbitrary — it can only name a kind of evidence the agent knows how to collect.
This is what makes the read-only guarantee hold even against a compromised
platform.

### 5.2 Browser ↔ Platform

Unchanged: REST plus SSE, already built and working, with the polling fallback
that survives corporate proxies. No reason to disturb it.

---

## 6. Security architecture

### Trust model

| Boundary | Threat | Control |
|---|---|---|
| Browser → Platform | Impersonation, token theft | OIDC, short-lived JWT, per-tenant audience |
| Platform → Agent | Compromised platform issues mutating work | Agent-side allowlist; `EvidenceSpec` cannot express a command |
| Agent → Cluster | Over-privileged agent | Per-request user impersonation; agent SA holds only `impersonate` + minimal read |
| Tenant → Tenant | Cross-tenant data access | Tenant id on every row, enforced in the data layer, not in handlers |
| Cluster → Platform | Hostile evidence (prompt injection) | Redaction at source; commands never model-authored; grounding checks |

**Agent identity.** Registration presents a one-time bootstrap token, scoped to a
tenant and single-use with a short TTL. The platform issues a client certificate
naming the tenant and cluster, valid ~90 days, rotated automatically at ~2/3 life
with an overlap window. Compromise response is revocation at the gateway plus
CRL/OCSP. This is SPIFFE-compatible; customers already running SPIRE should be
able to bring their own identity rather than adopting a second scheme.

**Least privilege.** The agent's ServiceAccount gets `impersonate` on users and
groups plus a minimal read role. It does **not** get blanket cluster read: with
impersonation the caller's own RBAC applies, which is what makes "the platform
cannot see more than you can" true rather than aspirational.

**Tenant isolation.** Shared control plane with tenant id on every row and
per-tenant encryption keys for evidence payloads. A **single-tenant deployment**
option is required — not optional — for government and several banking customers.
Design for it from the start; retrofitting isolation is the expensive path.

**Audit.** Every investigation, evidence request, report download and
configuration change recorded with actor, action, target, tenant and outcome,
append-only, exportable to the customer's SIEM. The existing `app/audit` is the
seed.

---

## 7. Communication protocol

Evaluated for the **agent ↔ platform** link specifically. The browser link is
already solved.

| | REST | GraphQL | WebSocket | SSE | gRPC | MCP |
|---|---|---|---|---|---|---|
| Platform→agent without inbound ports | ✗ | ✗ | ✓ | ✗ | ✓ | ✓ |
| Bidirectional streaming | ✗ | partial | ✓ | ✗ (one-way) | ✓ | limited |
| Schema / codegen | OpenAPI | strong | none | none | strong | JSON-RPC |
| Wire efficiency | JSON | JSON | any | text | protobuf | JSON |
| Backpressure | — | — | manual | — | built-in | — |
| Multiplexing | — | — | manual | — | HTTP/2 native | — |
| Proxy friendliness | excellent | excellent | good | excellent | **fair** | good |
| Enterprise familiarity | universal | moderate | good | good | **strong in infra** | emerging |
| Ops complexity | low | medium | medium | low | medium | low |

**The decisive criterion is none of the above — it is connection direction.**
Enterprises will not open inbound firewall ports into production clusters. Any
protocol where the platform initiates is disqualified regardless of merit. That
removes REST, GraphQL and SSE for this link immediately.

Of the remainder:

- **WebSocket** works and traverses proxies well, but provides no schema, no
  codegen, no backpressure and no multiplexing. Every one of those would be
  hand-rolled and become bespoke maintenance for five years.
- **MCP** is a tool-calling protocol designed for models to invoke tools. Using it
  as a fleet transport for 1,000 clusters would be a category error: no mTLS
  identity model, no backpressure, no streaming evidence semantics. It is
  genuinely valuable at a *different* boundary — see §3.7.
- **gRPC bidirectional streaming** provides exactly the needed shape: agent dials
  out, one HTTP/2 connection multiplexes many concurrent investigations,
  protobuf gives a versioned contract with generated clients in every language an
  agent might be written in, and flow control is built in.

### Recommendation

**gRPC bidirectional streaming over mTLS on port 443 for agent ↔ platform.**
**REST + SSE for browser ↔ platform** (unchanged). **MCP as a northbound
integration surface** for customer AI agents.

The honest cost: gRPC is the *least* proxy-friendly option here. Some corporate
egress proxies terminate HTTP/2 or inspect traffic in ways that break long-lived
streams. Mitigation is planned, not hoped for: run on 443 with ALPN, support
explicit proxy configuration, and ship a **WebSocket fallback transport behind
the same protobuf schema** for hostile networks. The schema is the contract; the
transport is swappable. Roughly 5–10% of enterprise deployments should be
expected to need the fallback, and discovering that after GA would be expensive.

---

## 8. Repository structure

Multi-artifact, single repository. Three deployables share one protobuf contract.

```
/proto/                        # the contract; source of generated clients
  agent/v1/{agent,evidence,collection}.proto

/platform/                     # today's backend/, extended
  app/
    evidence/  collectors/  analysis/  ai/  remediation/  reports/   # unchanged
    providers/                 # NEW — ClusterProvider + implementations
      base.py  local_kubectl.py  remote_agent.py  replay.py
    orchestrator/  fleet/  tenancy/  graph/  integrations/           # NEW
    api/  auth/  audit/  jobs/
  evals/  tests/

/agent/                        # NEW — the cluster connector
  cmd/  internal/{policy,collectors,transport,identity}/
  deploy/helm/

/console/                      # today's frontend/
/deploy/                       # platform Helm chart, Terraform, reference topologies
/docs/
```

**On agent language.** Go is the right choice: single static binary, no runtime
in the customer's cluster, native client-go, and the language every Kubernetes
operator team already reads. The cost is that the read-only allowlist and the
redaction rules then exist in two languages. Mitigate by generating both from one
declarative specification in `/proto/` and testing both against a shared corpus —
the redaction corpus already exists and is the right shape for this.

---

## 9. Migration plan

Nine milestones. Each compiles, deploys, passes CI, and is independently
revertible. No milestone requires the next.

**M1 — Provider abstraction (no behaviour change). ✅ Delivered.**
Introduce `ClusterProvider`; implement `LocalKubectlProvider` wrapping today's
executor; replace `CollectionContext.kubectl` with `CollectionContext.provider`.
Because the reasoning stack has zero Kubernetes imports, the blast radius is the
collector layer only. *Exit:* all 438 tests pass unchanged; behaviour identical.

*Outcome:* the prediction held — the abstraction was a substitution at one field,
not a refactor. Every targeted collector plus `RawNodesCollector` and
`ResourceMetricsCollector` now issue `ResourceRequest`s; the eleven commands they
produce are byte-identical to the ones they replaced and are pinned as a
translation table in `tests/test_providers.py` (mutation-tested: dropping `-n`,
`-A` or `--all-containers` each fails the suite). 438 → 461 tests, evals
unchanged at 10/10 and 11/11. The remaining `raw_executor()` users are
`LegacyInspectorCollector` and the nine inspectors, which M6 migrates.

**M2 — Evidence as a wire contract. ✅ Delivered.**
Define `/proto/`; generate Python; assert protobuf ↔ `Evidence` round-trips
losslessly. Nothing uses it yet. *Exit:* round-trip property tests pass.

*Outcome:* `/proto/agent/v1/{evidence,collection,agent}.proto` plus a committed
Python binding and `app/wire/codec.py`. 59 round-trip cases and a seeded fuzz,
mutation-tested: dropping a field, treating `""` as absent, non-canonical key
order and lost sub-second precision each fail the suite. Two design corrections
found while building it — a `default=str` fallback in the payload encoder would
have round-tripped a datetime into a string silently (removed; it now raises),
and the claimed distinction between an absent payload and JSON `null` does not
exist in Python, so the decoder accepts both and canonicalises, which is what
actually matters for a Go agent. CI regenerates and diffs, so schema and
bindings cannot drift. 461 → 520 tests.

**M3 — State out of process. ✅ Delivered.**
Postgres for investigations and history; Redis for the job queue. The existing
`InvestigationJobStore` interface is preserved — this was designed as a swappable
seam. *Exit:* multi-replica deployment; jobs survive restart; HA achieved.

*Outcome:* the seam held — `JobStore` gained `PostgresRedisJobStore` beside the
in-memory one and no API handler changed shape. The governing rule turned out to
be worth stating explicitly: **Redis is the latency layer, Postgres is the
truth.** Every message has a committed row behind it, so a dropped message costs
time and never correctness.

Three things the design had to solve rather than inherit:

- **Cancellation became a message.** `Task.cancel()` only works in the process
  that owns the task, so a cancel commits `cancel_requested`, publishes on a
  Redis control channel, and the owning worker turns it back into a local
  cancel. A per-job watchdog polling the committed flag is the backstop that
  makes it a guarantee rather than best effort — verified by a test with the
  control loop absent, and by a second test with the watchdog pushed an hour
  out, because otherwise the two mechanisms are indistinguishable.
- **SSE replay-then-live.** `subscribe()` opens the Redis subscription *before*
  reading the backlog and de-duplicates by a Postgres-assigned sequence.
  Subscribe-first gives no drop; the sequence gives no duplicate. The sequence
  is also emitted as the SSE frame id, so `Last-Event-ID` resumes a broken
  stream instead of replaying it.
- **Reports became bytes, not paths.** `/investigations/{id}/pdf` cannot serve a
  `FileResponse` for a report another worker rendered. The store returns bytes
  and M8 swaps it for object storage without touching an endpoint.

Scope boundary, stated because it is easy to overclaim: M3 delivers **durable,
correctly-terminated job records**, not mid-run resumption. A killed worker's
investigation is reaped to a terminal state via lease expiry rather than resumed;
resuming half-collected work is a re-run, and genuine resumability needs
ADR-007's persisted state machine.

Migrations are numbered forward-only SQL under `pg_advisory_lock`, not Alembic —
there is no ORM here, so Alembic's autogenerate would be a dependency with no
payoff. 521 → 560 hermetic tests, plus 39 that run only against real Postgres
and Redis (CI runs both). Evals unchanged at 10/10 and 11/11.

Two findings from running it rather than testing it. The idle queue consumer
crashed every five seconds because redis-py defaults `socket_timeout` to exactly
the five seconds the consumer blocks for, so the client aborted the read at the
instant the server answered; the fix is headroom between the two, and the
regression test deliberately uses the real block length because a fast one
passes against the broken version. And **the single-process deployment stays a
supported default, not a dev-only fallback** — with neither `DATABASE_URL` nor
`REDIS_URL` set, nothing imports either driver and `uvicorn app.main:app
--reload` still needs no infrastructure. Exactly one of the two set is refused
at startup rather than silently half-configuring.

**M4 — Agent MVP + gateway. ✅ Delivered (M4a transport, M4b identity).**
Go agent implementing pods, events, deployments, logs. mTLS registration.
`RemoteAgentProvider`. Runs beside the local provider, selected per cluster
record. *Exit:* one cluster investigated end-to-end through an agent, producing
byte-identical evidence to the local path.

*Outcome (M4a — transport):* `/agent/` is a Go binary that dials out, advertises
the evidence kinds it serves, and answers `CollectionRequest`s with streamed
`EvidenceRecord`s. `app/gateway/` terminates the stream; `RemoteAgentProvider`
turns a `ResourceRequest` into an `EvidenceSpec` and satisfies `ClusterProvider`
unchanged. Verified against a real kind cluster: 16 tests, including the
engine's own `RawNodesCollector` running verbatim with nothing between it and
the cluster but a socket. M1's prediction — that swapping how a cluster is
reached is a substitution at one field — held a second time.

**The exit criterion did not survive contact and has been corrected.**
"Byte-identical evidence to the local path" is not achievable while the local
path is kubectl: kubectl replaces the API server's typed list envelope
(`PodList`) with a generic one (`List`), on every list read, chunked or not. The
agent reads the API directly and is therefore the *more* faithful of the two.
Nothing downstream reads the envelope — every collector works from `items` — so
the criterion is now **the objects, their contents, and the derived conditions
match**, which is what a diagnosis actually rests on. The envelope difference is
asserted explicitly by a test so it stays a known fact rather than a surprise.

Two decisions worth recording. The agent reads **raw JSON rather than typed
objects**: client-go's typed structs drop fields the binary's compiled-in schema
does not know and reorder keys on re-marshal, which would have made an agent's
evidence differ from the same read performed locally for reasons nobody could
see. And correlation lives in an `EvidenceEnvelope` on the transport rather than
on `EvidenceRecord`, because the record is also the storage and audit format and
a stored record has no request id.

*Outcome (M4b — identity):* the agent generates a P-256 key, exchanges a
single-use bootstrap token for a certificate through the `Register` RPC the
schema already carried, and dials `Connect` over mTLS. The gateway resolves the
peer certificate to a cluster instead of believing `AgentHello`. ADR-005's
first half is now real; its second half (OIDC user identity and per-request
impersonation) is deliberately not — `Impersonation` is on the wire and
populated from the caller's `Principal`, but making the *cluster* enforce it is
a separate change with a separate RBAC conversation, and claiming it here would
be the kind of overreach §9 has avoided so far.

Four decisions were forced by contact rather than chosen on paper.

**Two listeners, not one.** gRPC's Python bindings offer only "never request a
client certificate" or "require and verify one" — there is no
request-but-do-not-require mode, so a single port cannot serve both an
unauthenticated enrolment call and an mTLS stream. Measured before designing
around it rather than assumed. The split turned out to describe the intent
better than one port did: enrolment is the only surface an unauthenticated peer
reaches, and a fleet that has finished enrolling can firewall it off entirely.
`RegistrationResponse.gateway_endpoint` — a field written speculatively in M2 —
is what tells a newly enrolled agent where to go next.

**A contradiction between certificate and hello aborts the stream.** Preferring
the certificate silently would be sound against an attacker and quietly wrong
against a mistake: an agent redeployed with the wrong `--cluster` flag would
have its evidence filed under one name while its own logs and configuration
said another, permanently, with nothing to notice it by. The refusal names both
values, and the fix is one flag.

**Renewal must not be a reconnect.** Rotating at two-thirds of certificate life
leaves a third as overlap, and the new certificate is swapped into a holder that
Go consults per TLS handshake — so the *next* dial uses it and the open stream
is never touched. Revoking the old certificate at renewal time would have
undone the whole point. Verified by running it: a real agent on a short-lived
certificate renews while the same session object keeps answering reads, and
removing the renewal loop fails that test.

**Revocation is swept, not merely checked at connect.** This transport's
defining property is a connection that stays open for weeks, which makes
revocation-at-reconnect close to no revocation at all. A gateway sweep ends live
sessions whose serial was revoked; a test with the sweeper removed fails, which
is the only thing that stops it being deleted as redundant later.

The differential suite now runs **over mTLS** — every evidence comparison from
M4a, unchanged, on a stream whose identity a certificate established — plus a
hermetic Python-to-Python mTLS suite so the identity model is covered by
`python -m pytest` with no cluster, no Go binary and no CA on disk. 634 hermetic
tests, 24 opt-in cluster tests, 30 enrolment-store contract cases across both
backends. Five platform invariants and one agent invariant were mutation-tested
rather than assumed: removing the hello/certificate check, the sweeper, the
single-use condition, the CSR's SAN construction, its signature verification,
and the renewal loop each fail a specific test.

Plaintext survives as `AGENT_GATEWAY_TLS=disabled` — an explicit, logged
opt-in for local development, on the same reasoning as the single-process job
store. Sessions established that way report `identity_source: "declared"`, so a
deployment that left it on cannot be mistaken for one that did not, and an mTLS
gateway refuses a plaintext agent rather than downgrading.

*Known limitation, stated rather than buried:* an agent's first dial has nothing
to verify the platform with. `--ca-file` is the correct path and
`agentctl issue-token` prints it; without it, bootstrap is trust-on-first-use,
logged as such, and the CA bundle returned by `Register` is pinned thereafter.
The CA itself is a development CA generated on first start — appropriate for one
deployment, not for a regulated customer, and the seam for an external issuer is
a constructor.

The remaining collectors and `RemoteAgentProvider` selection per cluster record
follow with M5.

**M5 — Collector parity. ✅ Delivered.**
Migrate the remaining collectors, including Prometheus and Loki, to the agent.
Differential testing: same cluster, both providers, evidence compared. *Exit:*
parity across the full collector set; `LocalKubectlProvider` becomes a
development and single-cluster convenience.

*Outcome:* the seven inspectors and the log collector are off `KubectlExecutor`,
and **`raw_executor()` is gone** — from the protocol and from both
implementations. That is the milestone in one line: there is no longer a way for
a collector to reach past the provider, so "the engine cannot tell which
provider it has" stopped being an intention. `select_provider()` makes the
choice an investigation actually goes through, which is what turned the agent
path from something only tests constructed into the one a request takes.

The split was along one seam: an inspector declares `ResourceRequest`s and
analyses results, and the analysis bodies moved across unchanged. Deliberate —
a differential suite can only prove parity if the code on both sides of the
comparison is the same code. The existing engine tests passed the migration
unmodified, which is the evidence that the move was a substitution rather than a
rewrite. A side benefit worth having: an inspector's reads now go out as one
`fetch_many`, so `WorkloadInspector`'s four sequential kubectl calls became one
round trip on a stream that may cross a continent.

Three things the work found rather than assumed.

**`kubectl top` and the metrics API do not return the same thing**, and that was
the last real obstacle. kubectl prints a percentage it computed from node
allocatable; metrics.k8s.io returns usage and nothing else. Teaching the Go
agent to reproduce kubectl's column layout would have put a formatting contract
in a binary shipped to a thousand clusters — the trap M4a avoided by reading raw
JSON rather than typed objects. So usage is measured and **the percentage is
derived on the platform, for both providers**, from node evidence already
collected. kubectl's own percentage column is parsed and discarded. The number
now agrees with the node evidence beside it, which the transported one did not
have to.

**A latent bug surfaced, and was fixed separately from the migration.** The
cluster-DNS check read the same local the service loop assigned to, so after any
non-empty service list it held the last service's namespace — always truthy, so
the check never fired and a cluster with no DNS service reported nothing wrong
with its DNS. It was preserved verbatim through the migration so parity stayed
provable, then fixed with its own test. Keying it on the scope also gave
`analyse()` the scope, which is the right shape anyway: some conclusions depend
on what was *asked for*, not on what came back.

**The differential suite had a hole where it looked strongest.** Comparing live
metrics is impossible — CPU moves between two reads — so the first version
compared only node names, and a mutation that reinstated the transported
percentage passed it. The fix was to assert internal consistency instead: each
path's percentage must be derivable from the usage printed beside it. Six
mutations are now checked, including one in the Go agent that silently scoped
list reads to a single namespace; that one fails five differential tests.

*Deliberately not delivered:* Prometheus and Loki through the agent. Those
collectors build PromQL and LogQL strings, and a query is a command in
everything but name — passing one down would contradict the property that makes
`EvidenceSpec` safe to send to a customer's cluster. The right shape is a query
catalogue **inside** the agent, named by kind exactly as Kubernetes reads are,
which is a design decision worth making on its own rather than as a footnote to
a migration. Both remain reachable from the platform today, and the M6 tenancy
work is the natural moment to settle it.

**M6 — Tenancy and fleet. ✅ Delivered.**
Organisations, cluster registry, tenant id on every row, per-tenant keys,
single-tenant deployment mode. *Exit:* two tenants provably isolated by test.

*Outcome:* the exit criterion is met by tests that **do not go through the
store**. They open a raw cursor, run `SELECT id FROM investigations` with no
WHERE clause — the worst thing a future contributor could write — and get back
only the caller's rows. That is the difference ADR-006 was asking for between
isolation enforced in the data layer and isolation enforced by everyone
remembering, and it is only demonstrable by writing the forgetful query on
purpose.

The mechanism is an ambient tenant rather than a parameter. A `ContextVar` is
set when the caller is authenticated; `Database.cursor()` turns it into a `SET
LOCAL` on every transaction; the column default stamps inserts and the policy
filters reads. **No store method gained a tenant argument** — the job store,
report store and enrolment store are untouched, which is the strongest
available evidence that the enforcement is not in the handlers.

**The finding that mattered: the control was inert the first time it was
tested, and nothing looked wrong.** `ENABLE ROW LEVEL SECURITY` and `FORCE ROW
LEVEL SECURITY` were both set, the policies were correct, and every tenant read
every row — because the application connected to Postgres as `postgres`, and
superusers (and any `BYPASSRLS` role) skip policies entirely. A deployment in
that state has no isolation and no symptom, and the test suite went green
against it. Two things came out of that: the tenancy tests now connect as an
unprivileged application role, and a `shared` deployment asks the database what
role it is and **refuses to start** if that role can bypass RLS. It is the same
discipline as refusing half-configured distributed state, applied to the one
setting where being wrong is silent.

`system_scope()` is the single deliberate hole — the queue consumer and the
reaper cannot know a tenant before reading the row that names one — and a test
asserts `jobs/consumer.py` is its only caller, so the hole stays the size it
was designed to be.

Agent identity gained a tenant in the SPIFFE path
(`spiffe://<domain>/tenant/<t>/cluster/<id>`), with the untenanted M4b form
still parsing as `default` so an upgrade does not disconnect the fleet. The
registry is keyed by `(tenant, cluster)`: two customers may both call a cluster
`prod`, and before this the second to connect would have evicted the first and
been handed its evidence.

*Deliberately not delivered:* per-tenant encryption keys. ADR-006 lists them,
and they are a key-management decision rather than a code one — where keys
live, who can read them, how they rotate, what happens to evidence encrypted
under a key that has been destroyed. Answering that with a hardcoded local key
would produce the same shape of false assurance this milestone just spent its
time removing. The isolation that stops one tenant reading another is built and
proved; encryption at rest against a compromised *operator* is a separate
control and deserves the KMS conversation it implies.

**M7 — Knowledge graph. ✅ Delivered.**
Emit edges during collection; persist; expose traversal; add graph-aware
hypothesis rules ("this pod's PVC is on a failed StorageClass"). *Exit:* the eval
corpus gains cases only answerable by traversal.

*Outcome:* the exit criterion is met and measured. Three new corpus cases pass
at 13/13; removing the graph rules drops the corpus to 10/13, so the cases are
demonstrably unreachable without traversal rather than merely new.

**One deliberate departure from the brief.** "Emit edges during collection"
would mean every collector producing edges alongside evidence — a second
thing to keep correct in a layer that is already the widest in the system. The
graph is instead *derived* from evidence, exactly as signals are. That buys
reproducibility (a graph rebuilt from a stored report is identical to the one
the diagnosis was made against), redaction and fault isolation for free, and
no new collection path for the local and agent routes to diverge on. The edges
were always in the store; collectors already fetch `ownerReferences`,
`nodeName`, volume claims and selectors.

Postgres with recursive CTEs stays the right answer for cross-investigation
traversal and is **not** built here: the graph an investigation reasons over is
its own, in memory, and persisted with its report. A shared edge table earns
its place when fleet-wide "what else is on this storage class" becomes a
question the product answers, which is M8's territory.

The rule that shaped the edge extractors: **no rule invents a node.** An edge
exists only when both ends were observed, so "depends on a ConfigMap we could
not see" and "depends on nothing" cannot look the same to a traversal. Two
placeholders had to be refused explicitly — a pod whose node reads `Pending`
is not on a node called Pending, and a claim whose class reads `none` is not
on a class called none — both of which would have produced confident nonsense.

Verified against a live cluster, not only fixtures: two pods Pending on two
unbound claims sharing one storage class produced "A storage class is blocking
several workloads" as the top root cause, with every graph signal citing the
edges it walked. Section by section that cluster shows two Pending pods and two
unbound claims and no connection between them.

Two findings worth recording. The traversal silently returned direct
neighbours only, because the direction was decided by `step is self.out_edges`
— always False, since attribute access builds a new bound method each time.
Depth was configurable, honoured nowhere, and the results looked plausible. And
one new eval case failed honestly: co-located failures do not outrank a
crash-loop, and raising the rule's severity to make its own test pass would
have been inflating a control to satisfy the thing measuring it. The case now
asserts the graph hypothesis is *present* and says why it should not win.

**M6.5 — Tenant user RBAC. ✅ Delivered.**
Users and roles within a tenant, stored under the same row-level security;
permission checks that cannot be forgotten per route; OIDC group → role
mapping. *Exit:* a viewer is refused every destructive operation, and each
authorisation invariant fails a test when removed.

*Outcome:* M6 made a tenant a data boundary and stopped there — inside one,
every caller who could authenticate could start investigations, mint cluster
enrolment tokens and revoke certificates. Four roles now separate real
capabilities rather than tiers: `operator` is the only role that can cause a
read against a customer cluster, `admin` changes the fleet and who is in it,
and `owner` exists for one invariant — *you cannot grant a role you do not
hold* — without which an admin granting admin makes the two identical and there
is no ceiling on escalation.

**The check lives in one router-level dependency and a route → permission
table, and a route absent from that table is denied.** That is what makes a
forgotten endpoint fail closed rather than open; `tests/test_authz.py` derives
the route list from the OpenAPI schema and asserts every route has an entry, so
the failure arrives at review time rather than in production. There are no
per-route permission checks to forget because there are none at all.

**The milestone found an ownership hole while enumerating what to gate.**
`GET /investigations/{id}/events` took only the job store: every other
investigation route takes the principal and 404s on a foreign id, and this one
streamed another user's live investigation to anyone who knew it. Authentication
was applied at the router and authorisation simply was not — which is the exact
shape of gap the milestone existed to close, so it was closed rather than filed.

**And it found that M6's ambient tenant had never reached the database over
HTTP.** `require_principal` was a synchronous dependency; FastAPI runs those in
a worker thread, a worker thread receives a *copy* of the context, and
`_current.set(principal.tenant)` therefore applied to a context discarded the
moment the dependency returned. Every request ran as `default` regardless of
who called: rows from every tenant were written into one and every tenant could
read every other tenant's, with the policies enabled, forced and correct. It is
precisely the failure M6 already caught once at the database role — a control
present, correct and inert — one layer up, and it was invisible to the test
suite because the isolation tests entered `tenant_scope()` by hand and so proved
the schema rather than the request path. The dependency is now `async` (the
authenticator still runs in a thread, because a JWKS cache miss must not block
the loop), and the regression test asserts on the value the request would hand
to Postgres rather than on `principal.tenant`, which passes with the bug
present.

**A confidentiality regression was caught during implementation and removed.**
`investigation.read_all` was initially an `admin` permission. But
`RBAC_DEFAULT_ROLE=admin` is what keeps existing single-tenant installs working
unchanged, so every unbound caller is an admin — and the two composed into
upgrading to this milestone silently removing the per-user report isolation
those deployments already had. It is now owner-only. `test_auth.py`'s ownership
suite is what caught it, by asserting behaviour rather than a role table.

*Deliberately not delivered:* an invite flow. It would need email delivery,
single-use tokens, an acceptance endpoint and a second identity to reconcile
against whatever the IdP eventually asserts — and it could never grant access,
because the platform cannot authenticate anyone the IdP does not know. All an
invite can do is pre-assign a role, which is what `PUT /members/{subject}` does
for someone who has never signed in. Bootstrapping a tenant's first owner is
`OIDC_ROLE_MAPPINGS` or `python -m app.rbacctl`, and explicitly *not* "the first
caller to authenticate owns the tenant", which in a shared deployment hands a
tenant to whoever arrives first.

Authorisation has **no `system_scope` equivalent, and that is a conclusion
rather than an omission**: the tenancy escape exists because the queue consumer
must read a row before it can know the tenant that row names, and authorisation
has no such ordering problem — the decision is made at the HTTP boundary and
background work carries the principal it was submitted with. The single hole is
`AUTH_MODE=disabled` resolving to `owner`, in one function, pinned by a test
that greps for any other place a role is manufactured.

Thirteen mutations were applied one at a time and all thirteen failed a test.

**M8 — Scale hardening.** *In progress; split into three tranches on contact.*
Evidence payloads to object storage; streaming ingest; partitioned queues; load
tests at 1,000 clusters and 5,000 concurrent investigations. *Exit:* documented
performance envelope.

Two of the four listed items — partitioned queues, and the separately-tracked
"route an investigation to the worker holding an agent's stream" — turned out to
be **one mechanism**, and the remaining two are worth choosing by measurement
rather than by this document's guess. Split accordingly:

- **M8a — routing and partitioned queues.** ✅ Delivered.
- **M8b — payload reads.** ⏳ In progress. Measured first, and the measurement
  inverted the brief: at 2,000 pods (the `MAX_LIST_ITEMS` ceiling) a stored
  result is **2.7 MB**, of which `diagnosis.signals` is 34%,
  `investigation.pods` 27% and `investigation.graph` 18% — so the majority is
  *derived*, and "evidence payloads to object storage" would target about a
  quarter of it. The waste was not where the bytes were stored but that they
  were read by callers who discarded them: a 25-row listing pulled 67.5 MB out
  of Postgres and returned none of it, and the polling fallback asked
  `/investigations/{id}` every 1.5s to read two fields.

  Delivered so far: `scripts/payload_bench.py`; a summary column list so a
  listing never selects `result`; `get_summary()` on both stores for the four
  internal reads that want a fact rather than the investigation; and an
  additive `GET /investigations/{id}/status` measured at **73× smaller**
  (784 KB → 10.7 KB at 500 pods). Derived sections stay stored — re-deriving on
  read would make an archived report reflect today's rules rather than the ones
  that produced it. Six mutations applied, all six caught.

  Still open, to be decided against a re-measurement: whether the payload
  leaves the row at all, and streaming ingest so `fetch_many` stops buffering a
  whole batch.
- **M8c — the envelope**: 1,000 clusters and 5,000 concurrent, documented.
  Not started.

**M8a — Routing and partitioned queues. ✅ Delivered.**
Route an investigation to the worker holding the agent's stream. *Exit:* an
investigation of a cluster whose agent is attached to another worker is
collected **through that agent**, and the routing hit rate is measured rather
than asserted.

*Outcome:* the exit criterion is met and measured — 100% routing hit rate at
1,000 clusters across both 3 and 10 workers, against a pre-M8a baseline of 1/N
(33% and 10%).

**This was filed as a scale item and is a correctness one.** `select_provider`
consulted only the *local* `AgentRegistry`, so it could not distinguish "no
agent exists anywhere in the fleet" from "an agent exists, on another worker".
Both returned `LocalKubectlProvider`. On three replicas that meant roughly two
thirds of agent-cluster investigations were answered by the platform's own
kubeconfig rather than by the cluster — and `LocalKubectlProvider` resolves a
cluster's *name* against whatever contexts the platform holds, with no notion of
a tenant. M6 made `AgentRegistry` tenant-keyed precisely so two customers could
both call a cluster `prod`; this path undid it. Evidence filed under a cluster
it was not read from is the one outcome the evidence spine exists to prevent.

Two mechanisms, and the split between them is the design:

- **Routing is a hint.** The submit path asks the presence index who holds the
  stream and queues the work on that worker's queue. `BLPOP` already takes a key
  list and returns from the first non-empty one, so a consumer reading its own
  queue before the shared one is the same round trip it was already making, and
  affinity gets its priority with no fairness knob.
- **Refusal is the guarantee.** If selection still lands on a worker that cannot
  reach the agent, it raises `ClusterUnreachable` naming the holding worker
  rather than reading a same-named local context. A hint that is occasionally
  wrong costs a retry; a guarantee that is occasionally wrong costs a
  misdiagnosis.

Nothing about correctness rests on the routing being right: the row stays
`pending` and the claim stays the conditional `UPDATE`, so a mis-route is a
scheduling miss and never a double run.

**The load-bearing detail is an inequality.** A job routed to a worker that then
dies waits on that worker's queue until the reaper re-offers it after
`UNCLAIMED_GRACE_SECONDS` (60) — and the re-offer goes to the **shared** queue,
never back to a worker queue. That only terminates because `PRESENCE_TTL_SECONDS`
(45) is smaller: by the time the re-offer fires, the dead worker's agents have
lapsed from the index, so nothing routes the job back to it. Raising the TTL
above the grace period turns recovery into a permanent loop, so the ordering is
asserted by a test rather than left to a comment.

**Presence is read with `GET`, not a scan.** Routing runs on the submit path of
every investigation, and scanning a tenant's agents to answer a question about
one of them would make the fleet's size the cost of starting any investigation.
Measured: the affinity lookup is flat at ~0.33ms p50 / ~0.5ms p99 whether the
fleet is 200 clusters or 1,000. Submission throughput is dominated by the job
row `INSERT` (165/s with rows, 1,500/s without), not by routing.

Ten mutations applied one at a time; all ten failed a test, including reversing
the queue priority, raising the presence TTL above the grace period, and
re-offering a stranded job back to the worker that dropped it.

`scripts/routing_bench.py` prints the hit rate and the submit-path percentiles.
It needs Postgres and Redis and nothing else — standing up a thousand real agent
streams measures a different thing and belongs to M8c.

**M9 — Integration surfaces.**
Event ingress, action egress, MCP server. *Exit:* an alert triggers an
investigation that opens a ticket with no human involved.

Sequencing note: M1–M3 deliver most of the enterprise value (HA, scale-out,
durable state) without any agent work, and M3 alone closes the largest gap in
`PRODUCTION_READINESS.md`. If the roadmap must be cut, cut from M7 onward, not
from the front.

---

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| gRPC blocked by customer egress proxy | **High** | WebSocket fallback behind the same schema, planned from M4 |
| Policy logic drifts between Go and Python | **High** | Generate both from one spec; shared conformance corpus in CI |
| Agent upgrade across 1,000 clusters | High | Version negotiation in `AgentHello`; platform supports N-2; agent self-updates via its own Helm release |
| Evidence volume overwhelms platform | High | Budgets at source, streaming ingest, object storage, per-tenant quotas |
| Cross-tenant leakage | **Critical** | Tenant id enforced in the data layer not handlers; isolation tests; single-tenant option |
| Compromised platform issues harmful work | **Critical** | Agent-side allowlist; `EvidenceSpec` cannot express a command |
| Migration stalls half-done | High | Every milestone independently deployable; both providers coexist indefinitely |
| Agent becomes a dumping ground | Medium | ADR-002 boundary enforced at review; anything needing state belongs centrally |
| Postgres graph outgrown | Medium | Traversals bounded at 3–5 hops; revisit only on evidence, not anticipation |
| Losing the grounding guarantees under scale pressure | **High** | Eval corpus gates every change; treat as a release blocker |

---

## 11. Architecture Decision Records

**ADR-001 — Centralised platform, distributed collectors.**
*Context:* 20–1,000 clusters per customer. *Decision:* one platform deployment;
one lightweight agent per cluster. *Why:* reasoning improvements ship once;
customers keep cluster data egress under their control; per-cluster footprint
stays small enough to be approved. *Rejected:* per-cluster full deployment (1,000
upgrade targets, no cross-cluster correlation); direct kubeconfig access from the
platform (needs inbound access and long-lived cluster credentials — commercially
unsellable). *Consequence:* the platform becomes a availability-critical
dependency and must be HA from M3.

**ADR-002 — Agent is a connector, not a participant.**
*Decision:* agent performs cluster access, policy enforcement, impersonation and
redaction. No AI, state, scheduling or business logic. *Why:* every capability
added to the agent multiplies by the fleet size and must be upgraded across it.
*Exceptions defended:* read-only enforcement and redaction are security controls
that must be local to be real. *Consequence:* a new evidence type needs an agent
release; mitigated by version negotiation and N-2 support.

**ADR-003 — ClusterProvider abstraction.**
*Decision:* the investigation engine depends on `ClusterProvider`, never on
kubectl or a Kubernetes client. *Why:* the engine should express *what* evidence
it needs, not *how* to get it. *Enabler discovered in review:* the reasoning
stack already has zero Kubernetes imports, so this is a substitution at
`CollectionContext.kubectl`, not a refactor. *Consequence:* `ReplayProvider`
makes investigations reproducible and turns the eval corpus into a fleet-scale
asset.

**ADR-004 — gRPC bidirectional streaming.**
*Decision:* gRPC over mTLS on 443, agent-initiated, with a WebSocket fallback.
*Why:* connection direction is the binding constraint; of the outbound-capable
options only gRPC provides schema, codegen, multiplexing and backpressure
together. *Rejected:* REST/GraphQL/SSE (wrong direction); WebSocket alone (no
schema or flow control); MCP (not a fleet transport — but adopted northbound).
*Consequence:* proxy incompatibility is a real risk and the fallback is planned,
not contingent.

**ADR-005 — mTLS agent identity, OIDC user identity, impersonation throughout.**
*Decision:* agents authenticate by certificate issued at registration and rotated
automatically; users authenticate by OIDC; every cluster read runs as the calling
user. *Why:* authentication decides *whether* you get in; impersonation decides
*what you can see*. Without it a compromised platform reads everything the
agent's ServiceAccount can. *Consequence:* customers must grant `impersonate`,
which is a conversation to have during onboarding, and is easier to defend than
blanket cluster read.

*Status:* the agent half shipped with M4b — certificate identity resolved at the
gateway, single-use enrolment, automatic rotation at 2/3 life with an overlap
window, revocation swept against live streams. Two refinements the
implementation forced, recorded because they are the parts a reader would
otherwise assume: identity is carried as a **URI SAN in SPIFFE form** rather
than a Common Name, so a customer already running SPIRE has somewhere to plug
in, and the platform **discards everything in the CSR except the public key** —
an agent that could name itself in its request could name another cluster, which
would make certificate identity decorative. The OIDC and impersonation halves
remain designed and unbuilt.

**ADR-006 — Shared control plane with enforced tenant isolation, plus a
single-tenant option.**
*Decision:* tenant id on every row enforced in the data layer, per-tenant
encryption keys, and a supported single-tenant deployment. *Why:* shared is
economic for most customers; regulated customers will not accept it at any price.
*Consequence:* two deployment topologies to test; cheaper than retrofitting
isolation after the first government deal.

**ADR-007 — Investigation as a durable, resumable state machine.**
*Decision:* `created → queued → collecting → analysing → deepening → reasoning →
reporting → complete`, persisted at each transition, leased so exactly one
orchestrator advances it, resumable after a worker dies. *Why:* an investigation
spans many agents and minutes; in-memory state cannot survive a deploy.
*Consequence:* replaces the in-memory job store; the interface was designed for
this and does not change above the persistence layer.

---

## 12. Enterprise readiness — design assessment

Scores are for the **proposed design**, with today's implementation for contrast.

| Dimension | Today | Designed | Notes |
|---|---|---|---|
| Architecture | 7 | 9 | Clean seams; provider abstraction is cheap because reasoning is already decoupled |
| Security | 7 | 9 | mTLS, impersonation, agent-side policy, tenant isolation. Not 10 until third-party assessed |
| Scalability | 3 | 8 | Stateless services, queue dispatch, bounded collection. 8 not 9 until load-tested at 1,000 |
| Reliability | 4 | 8 | HA at M3; resumable investigations; graceful degradation already strong |
| Maintainability | 7 | 8 | Existing layering preserved; dual-language policy is the main new cost |
| Observability | 2 | 8 | Self-instrumentation is designed in rather than retrofitted |
| Developer experience | 6 | 8 | `ReplayProvider` and the eval corpus make the reasoning layer testable without a cluster |
| Enterprise adoption | 3 | 8 | SSO, tenancy, audit, fleet, single-tenant option. 8 not 9 pending SOC 2 |

No dimension is scored 10. Nothing untested in production earns a 10.

---

## 13. Roadmap

**v2.0 — Deployable at fleet scale** (M1–M5)
Provider abstraction · durable state and HA · cluster agent and gateway ·
collector parity · self-observability. *Definition of done:* a platform team runs
it multi-replica behind SSO, connects 50 clusters, and passes a security review.

**v3.0 — Enterprise platform** (M6–M9)
Multi-tenancy with single-tenant option · fleet management · knowledge graph ·
event-driven investigation · integrations · MCP · provider-agnostic LLM including
self-hosted. *Definition of done:* 500 clusters across multiple tenants;
alert-triggered investigations reaching a ticket without human involvement.

**v4.0 — Autonomous operations**
Continuous investigation from cluster state rather than human invocation ·
learning from outcomes (which remediations actually worked) to reweight
hypothesis priors · cross-cluster fleet correlation ("this regression is on 40
clusters, all on node image X") · policy-gated auto-remediation for low-risk,
high-confidence classes with full audit · a community marketplace of playbooks
and rules.

The v4.0 auto-remediation step is the one that must not be rushed. It requires
the outcome data v3.0 collects, and it inverts the platform's founding
constraint. Every control described here — read-only agents, deterministic
commands, grounding, audit — exists so that inversion can eventually be made
safely and deliberately, rather than by degrees.
