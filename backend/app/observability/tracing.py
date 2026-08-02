"""Where an investigation's time actually goes.

The last open P1. `docs/PERFORMANCE_ENVELOPE.md` published a throughput ceiling
and listed, among the things it had *not* measured, "collection, report
rendering, Postgres writes and analysis all sit inside it and were not
isolated. Naming a bottleneck without measuring it is how the wrong thing gets
optimised." This is how that got measured — and measuring it showed the
published ceiling was the load harness rather than the platform.

**Phase timing, exported as histograms. OTLP trace export is deliberately not
built, and the reason is a hard constraint rather than a preference.**
`opentelemetry-proto` requires `protobuf<7.0`; this project pins
`protobuf==7.35.1` because protobuf 7 validates generated code against the
runtime, and the agent's wire bindings under `app/wire/gen/` are generated with
it. Taking the exporter means downgrading the toolchain that guarantees the
fleet transport's schema — the tail wagging the dog. Installing it in a
scratch environment silently downgraded protobuf to 6.33.6, which is exactly
the failure the pin exists to prevent.

What that costs, stated rather than glossed: no cross-service correlation. An
investigation submitted on one worker and run on another cannot be stitched
into one trace from here. What it does *not* cost is the question traces were
wanted for — `k8sagent_investigation_phase_seconds` answers "where did the time
go" from a scrape, in every deployment, with no collector to run.

To revisit: either `opentelemetry-proto` relaxes its protobuf ceiling, or the
wire bindings are regenerated against protobuf 6 deliberately rather than as a
side effect.

Phase names are a **closed set**, for the same reason no metric here carries a
cluster: a label whose values grow without bound is how a metrics store falls
over, and phases are a property of the pipeline rather than of the input.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Every phase an investigation passes through, in order. Closed, because it
# becomes a metric label; adding one means adding it here, which is where the
# reader is already looking.
PHASES: tuple[str, ...] = (
    "collect",
    "analyse",
    "report",
    # The durable write. Separate from `report` because they are different
    # costs in different deployments: `report` renders bytes, `persist` puts a
    # multi-megabyte jsonb into Postgres, and the first measurement of these
    # phases showed them accounting for a fraction of a worker's slot
    # occupancy — the missing time was here, outside every span.
    "persist",
    "notify",
)


@contextmanager
def span(phase: str, **attributes: Any) -> Iterator[None]:
    """Time one phase, and record it however this deployment can.

    Total by construction: the timing and the span are both best-effort, and an
    exception inside the block propagates unchanged. Instrumentation that can
    change the outcome of the thing it measures is worse than none.
    """
    from app.observability import metrics

    started = time.perf_counter()
    try:
        yield
    finally:
        # `finally`, so a phase that raised is still timed — a slow failure is
        # exactly the shape an operator needs to see, and dropping it would
        # make the histogram describe only the happy path.
        metrics.phase_finished(phase, time.perf_counter() - started)
