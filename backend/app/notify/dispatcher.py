"""Delivering the announcement, without letting it matter to the investigation.

The rule this module exists to enforce: **a notification can never fail an
investigation.** The work is finished, the report is stored, and the diagnosis
is durable before anything here runs. A ticketing system being down, slow, or
returning nonsense is that system's problem — turning it into a failed
investigation would make the platform's reliability the worst of its
integrations'.

So every path here is total. Delivery runs as a detached task, exceptions are
recorded and dropped, and the caller is never given anything to await.
"""

import asyncio
from typing import Any

import httpx
from loguru import logger

from app.notify.destinations import Destination, encode
from app.observability import metrics

# Two retries, quickly. A receiver that is down stays down for longer than any
# retry policy worth having, and the investigation is already durable — so the
# job here is to survive a blip, not to guarantee delivery. Guaranteed delivery
# would need a queue, a dead-letter, and a story for ordering, which is a
# different feature from "tell Slack".
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 2.0)
TIMEOUT_SECONDS = 10.0


async def deliver(destination: Destination, summary: dict[str, Any]) -> bool:
    """POST once, with bounded retries. Never raises."""
    body = encode(summary)
    headers = {"Content-Type": "application/json"}
    if destination.secret:
        headers["X-K8sagent-Signature"] = destination.signature(body)

    for attempt in range(MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(destination.url, content=body, headers=headers)
            if response.status_code < 400:
                metrics.notification("delivered")
                return True

            # 4xx is not retried: a receiver rejecting the body will reject it
            # again, and retrying turns our bug into their rate limit.
            if response.status_code < 500:
                metrics.notification("rejected")
                logger.warning(
                    "Destination {name} rejected the notification ({status})",
                    name=destination.name,
                    status=response.status_code,
                )
                return False

            logger.debug(
                "Destination {name} returned {status}",
                name=destination.name,
                status=response.status_code,
            )
        except Exception as exc:
            logger.debug(
                "Destination {name} unreachable: {error}", name=destination.name, error=exc
            )

        if attempt < len(BACKOFF_SECONDS):
            await asyncio.sleep(BACKOFF_SECONDS[attempt])

    metrics.notification("failed")
    logger.warning(
        "Gave up announcing to {name} after {attempts} attempts",
        name=destination.name,
        attempts=MAX_ATTEMPTS,
    )
    return False


def announce(
    investigation_id: str,
    outcome: str,
    investigation: dict[str, Any] | None,
    diagnosis: dict[str, Any] | None,
) -> None:
    """Fire the announcement and return immediately.

    Synchronous by signature and asynchronous in effect, because every caller
    is on the investigation's terminal path and none of them should be given
    something to await. There is no return value on purpose: a caller that
    could observe delivery would eventually be written to depend on it.
    """
    try:
        _announce(investigation_id, outcome, investigation, diagnosis)
    except Exception as exc:  # pragma: no cover - exercised by the fault test
        logger.warning(
            "Could not announce investigation {id}: {error}", id=investigation_id, error=exc
        )


def _announce(
    investigation_id: str,
    outcome: str,
    investigation: dict[str, Any] | None,
    diagnosis: dict[str, Any] | None,
) -> None:
    from app.core.config import settings
    from app.notify import get_destinations
    from app.notify.destinations import build_summary
    from app.tenancy import current_tenant

    destinations = get_destinations()
    if not destinations:
        return

    tenant = current_tenant()
    summary = build_summary(
        investigation_id, outcome, investigation, diagnosis, settings.console_url
    )
    severity = str(summary.get("severity") or "info")

    for destination in destinations:
        # The tenant check is first and is not a filter on the summary — it
        # decides whether this destination hears about the investigation at
        # all. Announcing one customer's incident into another's channel is
        # M6's failure committed on the way out.
        if destination.tenant != tenant:
            continue
        if not destination.accepts(outcome, severity):
            continue

        _spawn(_timed(destination, summary))


async def _timed(destination, summary) -> bool:
    """Delivery is a phase too: a slow receiver is a plausible answer to
    "where did the time go", and one nobody would think to look for."""
    from app.observability.tracing import span

    with span("notify", destination=destination.name):
        return await deliver(destination, summary)


def _spawn(coroutine) -> None:
    """Run a delivery detached from whatever is on the stack.

    A reference is kept only so the loop does not garbage-collect the task
    mid-flight; nothing ever awaits it. Outside a running loop — a synchronous
    `/investigate` call in a worker thread — there is nothing to attach to, and
    a short-lived loop is the honest answer rather than silently not sending.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coroutine)
        except Exception as exc:  # pragma: no cover
            logger.debug("Notification failed: {error}", error=exc)
        return

    task = loop.create_task(coroutine)
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)


_IN_FLIGHT: set[asyncio.Task] = set()
