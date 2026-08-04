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

    def ping(self) -> None:
        """Raise if Redis is not answering. For the readiness probe."""
        self._sync.ping()

    # --- key layout ---------------------------------------------------------

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def queue_key(self) -> str:
        return f"{self._prefix}:jobs:queue"

    def worker_queue_key(self, worker_id: str) -> str:
        """The queue for work only one worker can usefully do.

        A gRPC stream belongs to whichever worker holds the socket, so an
        investigation of an agent-connected cluster has exactly one worker that
        can collect it. The shared queue cannot express that; this can.

        It is a *hint*, never a second source of truth. The row stays `pending`
        and the claim stays the conditional UPDATE, so a job on the wrong
        worker's queue is a scheduling miss rather than a correctness failure.
        """
        return f"{self._prefix}:jobs:queue:{worker_id}"

    @property
    def control_channel(self) -> str:
        return f"{self._prefix}:jobs:control"

    def events_channel(self, job_id: str) -> str:
        return f"{self._prefix}:jobs:events:{job_id}"

    # --- presence -----------------------------------------------------------
    #
    # Expiring keys rather than a set with explicit removal: a worker that is
    # killed cannot remove its own entries, and a fleet index that accumulates
    # phantom agents is worse than one that is briefly a few seconds stale.

    def set_expiring(self, key: str, value: str, ttl_seconds: int) -> None:
        self._sync.set(key, value, ex=ttl_seconds)

    def get(self, key: str) -> str | None:
        """One key, for the routing lookup on the submit path.

        Deliberately not expressed as a one-element `scan_values`: routing asks
        about a single named cluster, and a scan would make the fleet's size the
        cost of starting any investigation.
        """
        return self._sync.get(key)

    def delete(self, key: str) -> None:
        self._sync.delete(key)

    def set_if_absent(self, key: str, ttl_seconds: int) -> bool:
        """Claim a key, or report that someone already holds it.

        `SET NX EX` in one command: exactly one caller creates it, and it
        expires on its own, so no cleanup path has to be correct on the unhappy
        path. Used for alert deduplication, where two workers receiving the
        same webhook must not both start an investigation.
        """
        return bool(self._sync.set(key, "1", nx=True, ex=ttl_seconds))

    def increment_in_window(self, key: str, ttl_seconds: int) -> int:
        """Count one use, and return the running total for this window.

        `INCR` returns the post-increment value, so the caller that gets 1 is
        the one that created the key and is therefore the one that sets its
        expiry. That ordering is what makes this safe without a read-then-write:
        two workers cannot both believe they are first, and a key can never be
        left without a TTL.
        """
        pipeline = self._sync.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, ttl_seconds, nx=True)
        used, _ = pipeline.execute()
        return int(used)

    def scan_values(self, pattern: str) -> list[str]:
        """Values of every key matching `pattern`.

        `scan_iter` rather than `keys`, because `keys` blocks the server for
        the length of the keyspace and this runs on a page load.
        """
        keys = list(self._sync.scan_iter(match=pattern, count=100))
        if not keys:
            return []
        return [value for value in self._sync.mget(keys) if value]

    # --- queue --------------------------------------------------------------

    def enqueue(self, job_id: str, worker_id: str = "") -> None:
        """Offer a job to one worker, or to whoever is free.

        `worker_id` is set when the cluster is reachable only through an agent
        whose stream that worker holds. Everything else goes to the shared
        queue, which is still the common case: a kubeconfig cluster can be
        collected anywhere.
        """
        key = self.worker_queue_key(worker_id) if worker_id else self.queue_key
        self._sync.rpush(key, job_id)

    async def dequeue(
        self, timeout: float = QUEUE_BLOCK_SECONDS, worker_id: str = ""
    ) -> str | None:
        """Block for a job id, or return None so the caller can do other work.

        `BLPOP` takes a key list and returns from the first non-empty one, so
        naming this worker's own queue first gives affinity its priority in the
        same round trip — no second poll, no fairness knob.

        An idle queue is the normal case, and redis-py surfaces the expiry of a
        blocking read as an exception. Letting that escape turns "no work right
        now" into a crashed consumer loop every few seconds, so it is caught
        here and reported as what it is: nothing to do.
        """
        import redis.exceptions

        keys = [self.worker_queue_key(worker_id), self.queue_key] if worker_id else [self.queue_key]
        try:
            item = await self._async.blpop(keys, timeout=timeout)
        except (redis.exceptions.TimeoutError, TimeoutError):
            return None
        return item[1] if item else None

    def queue_depth(self, worker_id: str = "") -> int:
        """How many ids are waiting. For the load harness and for operators."""
        key = self.worker_queue_key(worker_id) if worker_id else self.queue_key
        return int(self._sync.llen(key))

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
