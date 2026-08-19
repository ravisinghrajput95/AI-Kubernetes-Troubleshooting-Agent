"""What the platform says about itself.

`PRODUCTION_READINESS.md` calls the absence of this "ironic for an
observability tool", and `docs/PERFORMANCE_ENVELOPE.md` made it concrete: that
document tells an operator to *size on throughput and alarm on queue depth*,
and until now the platform exposed neither. An envelope you cannot observe in
production is a laboratory result.

So the metric set is chosen from the envelope rather than from what happens to
be easy to instrument. Every number that document tells someone to act on has a
series here.

**The load-bearing decision is what is *not* a label.**

No metric here carries a cluster id, a tenant, a namespace, a user or an
investigation id, and that single rule is doing two jobs at once:

- **Cardinality.** One series per cluster across a 1,000-cluster fleet, times
  the handful of metrics below, is how a Prometheus falls over. The platform
  is built for exactly the fleet size that makes this fatal.
- **Disclosure.** `/metrics` is scraped by infrastructure, not by a
  tenant-authenticated caller. Labelling by cluster or tenant would publish the
  customer list, and their cluster names, to anyone who can reach the port —
  after M6 spent a milestone making one tenant's rows invisible to another.

Those two arguments point the same way, which is the useful kind of constraint.
When someone eventually wants per-cluster rates, the answer is the audit log or
an investigation query, not a label here.

Failing to record a metric must never fail the thing being measured, so every
public function here is total: it records or it does nothing.
"""

from collections.abc import Callable

from loguru import logger
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest

# A registry of our own rather than the process-global default.
#
# The default registry is shared with anything else that imports
# prometheus_client, and it auto-registers process and GC collectors on import.
# Owning the registry keeps `/metrics` to what this module deliberately
# publishes, and lets the tests build a clean one instead of asserting against
# whatever else the interpreter has loaded.
REGISTRY = CollectorRegistry()

# Buckets chosen from the measured envelope, not from the library default.
#
# The default (.005 … 10) puts almost every investigation in the last bucket:
# the measured p50 through an agent is ~0.5 s and a real cluster with an LLM
# call is tens of seconds. Buckets that all saturate produce a histogram that
# cannot answer the question it exists for.
INVESTIGATION_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, float("inf"))
COLLECTION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, float("inf"))

investigations_total = Counter(
    "k8sagent_investigations_total",
    "Investigations that reached a terminal state, by outcome.",
    ["outcome"],
    registry=REGISTRY,
)

investigations_submitted_total = Counter(
    "k8sagent_investigations_submitted_total",
    "Investigations accepted for execution.",
    registry=REGISTRY,
)

investigation_duration_seconds = Histogram(
    "k8sagent_investigation_duration_seconds",
    "Wall time from start to terminal state.",
    buckets=INVESTIGATION_BUCKETS,
    registry=REGISTRY,
)

investigations_running = Gauge(
    "k8sagent_investigations_running",
    "Investigations executing on this worker right now.",
    registry=REGISTRY,
)

worker_capacity = Gauge(
    "k8sagent_worker_capacity",
    "JOB_MAX_CONCURRENT for this worker. Saturation is running/capacity.",
    registry=REGISTRY,
)

queue_depth = Gauge(
    "k8sagent_queue_depth",
    "Investigations waiting to be claimed. The envelope's alarm signal.",
    ["queue"],
    registry=REGISTRY,
)

agents_connected = Gauge(
    "k8sagent_agents_connected",
    "Agent streams held by this worker.",
    registry=REGISTRY,
)

cluster_access_total = Counter(
    "k8sagent_cluster_access_total",
    "How investigations reached their cluster: through an agent or a kubeconfig.",
    ["provider"],
    registry=REGISTRY,
)

collection_duration_seconds = Histogram(
    "k8sagent_collection_duration_seconds",
    "Wall time for one collection wave.",
    buckets=COLLECTION_BUCKETS,
    registry=REGISTRY,
)

evidence_records_total = Counter(
    "k8sagent_evidence_records_total",
    "Evidence records collected, by status. Degradation is visible here first.",
    ["status"],
    registry=REGISTRY,
)

llm_calls_total = Counter(
    "k8sagent_llm_calls_total",
    "Model calls, by outcome. `skipped` means no API key was configured.",
    ["outcome"],
    registry=REGISTRY,
)

diagnoses_total = Counter(
    "k8sagent_diagnoses_total",
    "Diagnoses by path: `grounded` accepted the model, `fallback` did not.",
    ["path"],
    registry=REGISTRY,
)

events_triggered_total = Counter(
    "k8sagent_events_triggered_total",
    "Investigations started by an inbound event.",
    registry=REGISTRY,
)

events_rejected_total = Counter(
    "k8sagent_events_rejected_total",
    "Inbound events not acted on, by reason. `duplicate` is the normal case.",
    ["reason"],
    registry=REGISTRY,
)

investigation_phase_seconds = Histogram(
    "k8sagent_investigation_phase_seconds",
    "Wall time inside one phase of the pipeline. Attributes the throughput ceiling.",
    ["phase"],
    buckets=COLLECTION_BUCKETS,
    registry=REGISTRY,
)

notifications_total = Counter(
    "k8sagent_notifications_total",
    "Outbound announcements, by outcome. `failed` means retries were exhausted.",
    ["outcome"],
    registry=REGISTRY,
)

rate_limited_total = Counter(
    "k8sagent_rate_limited_total",
    "Investigation submissions refused by a rate limit, by which bucket refused.",
    ["scope"],
    registry=REGISTRY,
)

grounding_rejections_total = Counter(
    "k8sagent_grounding_rejections_total",
    "Model responses discarded by grounding, by reason.",
    ["reason"],
    registry=REGISTRY,
)


# Every label this module allows is a **closed set**, which falls out of the
# no-unbounded-labels rule above — and that makes pre-initialisation possible.
#
# Prometheus does not create a labelled series until it is first observed, so
# `k8sagent_investigations_total{outcome="failed"}` reads "no data" rather than
# 0 until the first failure. An alert written against it is silent for exactly
# as long as the platform is healthy, and fires on the *second* failure. Seeding
# each combination at zero is what makes an alert correct from a cold start.
_KNOWN_LABELS: tuple[tuple[Counter, str, tuple[str, ...]], ...] = (
    (
        investigations_total,
        "outcome",
        ("succeeded", "failed", "cancelled", "worker_lost", "unreachable", "no_evidence"),
    ),
    (cluster_access_total, "provider", ("agent", "kubeconfig")),
    (rate_limited_total, "scope", ("subject", "tenant")),
    (notifications_total, "outcome", ("delivered", "rejected", "failed")),
    (
        events_rejected_total,
        "reason",
        ("signature", "malformed", "duplicate", "submit_failed"),
    ),
    (llm_calls_total, "outcome", ("succeeded", "failed")),
    (diagnoses_total, "path", ("grounded", "fallback")),
    (
        grounding_rejections_total,
        "reason",
        (
            "empty_root_cause",
            "fabricated_hypothesis",
            "bad_citations",
            "irrelevant_citations",
            "invented_resource",
            "contradiction",
            "unparseable",
            "other",
        ),
    ),
    (
        evidence_records_total,
        "status",
        ("ok", "empty", "unavailable", "forbidden", "timeout", "not_applicable", "failed"),
    ),
)


def _seed() -> None:
    from app.observability.tracing import PHASES

    for phase in PHASES:
        investigation_phase_seconds.labels(phase=phase)

    for metric, label, values in _KNOWN_LABELS:
        for value in values:
            metric.labels(**{label: value}).inc(0)
    for name in ("shared", "worker"):
        queue_depth.labels(queue=name).set(0)


_seed()


def _safe(action: Callable[[], None]) -> None:
    """Record, or do nothing.

    Instrumentation that can fail the thing it measures is worse than no
    instrumentation: it converts an observability bug into an outage. Every
    entry point below goes through here.
    """
    try:
        action()
    except Exception as exc:  # pragma: no cover - exercised by the fault test
        logger.debug("Metric not recorded: {error}", error=exc)


def investigation_submitted() -> None:
    _safe(investigations_submitted_total.inc)


def investigation_finished(outcome: str, duration_seconds: float | None = None) -> None:
    _safe(lambda: investigations_total.labels(outcome=outcome).inc())
    if duration_seconds is not None and duration_seconds >= 0:
        _safe(lambda: investigation_duration_seconds.observe(duration_seconds))


def running(count: int) -> None:
    _safe(lambda: investigations_running.set(count))


def capacity(limit: int) -> None:
    _safe(lambda: worker_capacity.set(limit))


def queue(name: str, depth: int) -> None:
    """Queue depth by *role*, not by worker id.

    `shared` and `worker` rather than the worker's hostname: a worker id is
    unbounded over a deployment's life, so labelling by it grows the series
    count with every restart and rollout.
    """
    _safe(lambda: queue_depth.labels(queue=name).set(depth))


def agents(count: int) -> None:
    _safe(lambda: agents_connected.set(count))


def cluster_access(provider: str) -> None:
    _safe(lambda: cluster_access_total.labels(provider=provider).inc())


def collection_finished(duration_seconds: float, statuses: dict[str, int]) -> None:
    _safe(lambda: collection_duration_seconds.observe(duration_seconds))
    for status, count in statuses.items():
        _safe(
            lambda status=status, count=count: evidence_records_total.labels(status=status).inc(
                count
            )
        )


def llm_call(outcome: str) -> None:
    _safe(lambda: llm_calls_total.labels(outcome=outcome).inc())


def diagnosis(path: str) -> None:
    _safe(lambda: diagnoses_total.labels(path=path).inc())


def grounding_rejected(reason: str) -> None:
    """`reason` is a fixed category, never the model's prose.

    Grounding messages quote the response they rejected, which is attacker-
    influenced text from a cluster. Using one as a label would hand an
    unbounded, hostile-controlled string to the metrics store — the same
    injection surface `app/ai` closes at the prompt boundary.
    """
    _safe(lambda: grounding_rejections_total.labels(reason=reason).inc())


def event_triggered() -> None:
    _safe(events_triggered_total.inc)


def event_rejected(reason: str) -> None:
    """`reason` is a fixed category, never anything from the payload."""
    _safe(lambda: events_rejected_total.labels(reason=reason).inc())


def phase_finished(phase: str, seconds: float) -> None:
    """`phase` is one of `tracing.PHASES` — a closed set, like every label here."""
    _safe(lambda: investigation_phase_seconds.labels(phase=phase).observe(seconds))


def notification(outcome: str) -> None:
    """`outcome` is a fixed category, never the receiver's response body."""
    _safe(lambda: notifications_total.labels(outcome=outcome).inc())


def rate_limited(scope: str) -> None:
    """`scope` is `subject` or `tenant` — never the caller's identity."""
    _safe(lambda: rate_limited_total.labels(scope=scope).inc())


def render() -> tuple[bytes, str]:
    """The exposition payload and its content type.

    **Both come from the same module, and that is the whole of this function.**
    The generator and the content type were imported from *different* ones —
    the Prometheus text-format `generate_latest` paired with the OpenMetrics
    content type — so every response advertised OpenMetrics and carried a body
    that is not. OpenMetrics requires a terminating `# EOF`; text format has
    none, so Prometheus selected its OpenMetrics parser on the strength of the
    header and rejected the whole scrape with `data does not end with # EOF`.

    Nothing local could see it. `curl /metrics` returned 200 and 16 KB of
    correct exposition, every series was present and correctly labelled, and
    the test asserted the *header* said openmetrics — which it did. Only a real
    Prometheus, parsing what the header promised, disagreed: both targets
    `down`, no series stored, and all 17 rules in `deploy/alerts/` evaluating
    against nothing forever. Import both names from one module so the pair
    cannot drift again.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
