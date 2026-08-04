"""Whether this worker should be sent traffic, as distinct from whether it is alive.

`/health` answered one question for both, which is fine until the two answers
differ — and they differ at exactly the two moments that matter.

**Starting up.** The process is listening before migrations have run and before
the queue consumer is claiming. A load balancer that sees one endpoint and
finds it green sends requests into a worker with no store.

**Shutting down.** This is the one that costs users their work. Kubernetes
sends SIGTERM and removes the pod from Endpoints *concurrently*, so for the
propagation window — typically a second or two, longer under load — the pod is
still receiving new requests while it is trying to drain. Answering "alive" to a
liveness probe is correct there; answering "ready" is not, and if the same
endpoint serves both you cannot say one without the other.

So: **liveness is a property of the process, readiness is a property of the
dependencies plus the lifecycle.** Liveness must never consult Postgres or
Redis — a database blip would restart every worker in the fleet simultaneously,
turning a recoverable dependency failure into an outage. That asymmetry is the
whole point of splitting them, and it is the same shape as the rate limiter
failing open while authorisation fails closed.
"""

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class Readiness:
    """Lifecycle state for the readiness probe. One per process."""

    # False until startup has finished wiring the state backend. A worker that
    # is listening but not yet wired must not be sent work.
    started: bool = False
    # Set the instant shutdown begins, before anything is torn down. This is
    # what gets the pod out of the load balancer's rotation while in-flight
    # investigations finish.
    draining: bool = False
    checks: dict[str, Any] = field(default_factory=dict)

    def mark_started(self) -> None:
        self.started = True

    def begin_drain(self) -> None:
        if not self.draining:
            logger.info("Draining: readiness is now false, in-flight work continues")
        self.draining = True

    @property
    def ready(self) -> bool:
        return self.started and not self.draining

    def reason(self) -> str:
        if self.draining:
            return "draining"
        if not self.started:
            return "starting"
        return "ready"


_readiness = Readiness()


def get_readiness() -> Readiness:
    return _readiness


def reset_readiness() -> None:
    """Tests build and tear down the app repeatedly in one process."""
    global _readiness
    _readiness = Readiness()
