"""Redis: the work queue, the control channel, and progress fan-out.

Redis is the latency layer, never the source of truth. Every message here has a
durable Postgres fact behind it:

- a queued id is a `pending` row, so a lost message costs seconds of latency
  when the reaper re-enqueues it, not a lost investigation;
- a cancel message is a `cancel_requested` column already committed;
- a progress event is a row in `investigation_events` already inserted, with
  the sequence number that the message carries.

If Redis drops everything, the system is slower. It is never wrong.

Two clients, deliberately. `publish` is called from collector worker threads, so
it needs the synchronous client; subscription runs on the event loop, so it
needs the asyncio one. Both come from the same `redis` package.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

# How long the queue consumer parks on an empty queue before looping.
QUEUE_BLOCK_SECONDS = 5.0

# Headroom between that and the client's own read deadline. redis-py defaults
# `socket_timeout` to 5s, which is exactly how long the consumer blocks — so
# the client aborts the read at the same instant the server answers, and every
# idle cycle raises. Giving the socket a longer deadline than the command is
# what makes an idle consumer quiet instead of a source of errors.
SOCKET_TIMEOUT_HEADROOM_SECONDS = 10.0


class RedisBus:
    def __init__(self, url: str, prefix: str = "k8sagent") -> None:
        import redis
        import redis.asyncio

        self._prefix = prefix
        socket_timeout = QUEUE_BLOCK_SECONDS + SOCKET_TIMEOUT_HEADROOM_SECONDS
        self._sync = redis.Redis.from_url(url, decode_responses=True)
        self._async = redis.asyncio.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
        )
        # Fails fast at startup rather than on the first investigation.
        self._sync.ping()
        logger.info("Redis bus ready (prefix {prefix})", prefix=prefix)

    # --- key layout ---------------------------------------------------------

    @property
    def queue_key(self) -> str:
        return f"{self._prefix}:jobs:queue"

    @property
    def control_channel(self) -> str:
        return f"{self._prefix}:jobs:control"

    def events_channel(self, job_id: str) -> str:
        return f"{self._prefix}:jobs:events:{job_id}"

    # --- queue --------------------------------------------------------------

    def enqueue(self, job_id: str) -> None:
        self._sync.rpush(self.queue_key, job_id)

    async def dequeue(self, timeout: float = QUEUE_BLOCK_SECONDS) -> str | None:
        """Block for a job id, or return None so the caller can do other work.

        An idle queue is the normal case, and redis-py surfaces the expiry of a
        blocking read as an exception. Letting that escape turns "no work right
        now" into a crashed consumer loop every few seconds, so it is caught
        here and reported as what it is: nothing to do.
        """
        import redis.exceptions

        try:
            item = await self._async.blpop([self.queue_key], timeout=timeout)
        except (redis.exceptions.TimeoutError, TimeoutError):
            return None
        return item[1] if item else None

    # --- control ------------------------------------------------------------

    def request_cancel(self, job_id: str) -> None:
        self._sync.publish(self.control_channel, json.dumps({"op": "cancel", "id": job_id}))

    async def watch_control(self) -> AsyncIterator[dict[str, Any]]:
        """Yield control messages until cancelled. One connection per process."""
        pubsub = self._async.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self.control_channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    yield json.loads(message["data"])
                except (ValueError, TypeError):
                    logger.warning("Ignoring malformed control message")
        finally:
            await pubsub.aclose()

    # --- progress fan-out ---------------------------------------------------

    def publish_event(self, job_id: str, payload: dict[str, Any]) -> None:
        self._sync.publish(self.events_channel(job_id), json.dumps(payload))

    async def subscribe_events(self, job_id: str):
        """A subscription handle that is live from the moment it is created.

        Returned rather than iterated so the caller can subscribe *before*
        reading the backlog. That ordering is what makes it impossible to lose
        an event published while the backlog query is in flight.
        """
        pubsub = self._async.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self.events_channel(job_id))
        return _EventSubscription(pubsub)

    async def close(self) -> None:
        try:
            await self._async.aclose()
        finally:
            self._sync.close()


class _EventSubscription:
    """A live Redis subscription to one investigation's events."""

    def __init__(self, pubsub) -> None:
        self._pubsub = pubsub

    async def next_event(self, timeout: float) -> dict[str, Any] | None:
        """The next event payload, or None if `timeout` elapsed first."""
        message = await self._pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=timeout,
        )
        if message is None or message.get("type") != "message":
            return None
        try:
            return json.loads(message["data"])
        except (ValueError, TypeError):
            logger.warning("Ignoring malformed event message")
            return None

    async def drain(self) -> list[dict[str, Any]]:
        """Everything buffered so far, without waiting."""
        events: list[dict[str, Any]] = []
        while True:
            event = await self.next_event(timeout=0.0)
            if event is None:
                return events
            events.append(event)

    async def close(self) -> None:
        await self._pubsub.aclose()
