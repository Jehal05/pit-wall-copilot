"""Cross-instance event distribution — AWS hackathon-final pass, P0-3.

`RaceEventBroadcaster` (event_broadcaster.py) only ever tracked WebSocket
connections held by its own process. That is correct and sufficient for a
single backend process, but this application is deployed to ECS/Fargate
where `BackendService.DesiredCount` can (and, for a real production
deployment, should) be greater than 1 — see infrastructure/template.yaml's
own comment on `BackendService`. With more than one task running, an event
produced on task A (the simulator tick, a strategy/undercut calculation, a
watch firing) was simply never seen by a client connected to task B: no
error was raised, delivery just silently failed for roughly `(N-1)/N` of
connected clients. Sticky sessions at the load balancer would only paper
over this for as long as no task is ever replaced, scaled, or rebalanced —
explicitly rejected as "a complete fix" by this pass's own requirements.

This module is the fix: an `EventBroker` abstraction with two
implementations.

  - `InMemoryEventBroker` — a single-process, in-memory implementation
    used for local development and the existing single-instance test
    suite. Semantically identical to calling the same in-process
    broadcaster directly; this is what `event_broker=memory` (the
    default) wires up, so nothing about existing behavior/tests changes
    unless a deployment explicitly opts into `event_broker=redis`.

  - `RedisEventBroker` — publishes every event to a Redis/Valkey pub/sub
    channel (Amazon ElastiCache Serverless in the deployed stack) and
    subscribes to that same channel, so every backend task — regardless
    of which task originally produced the event — receives it and can
    deliver it to whichever of ITS OWN locally-connected WebSocket
    clients are watching that race. `RaceEventBroadcaster` is unchanged
    in how it holds and pushes to local connections; only how a task
    learns "an event happened" is now broker-mediated rather than always
    being its own in-process call.

Both implementations report structured delivery metrics
(`EventBroker.metrics()`) and never claim a publish "succeeded" silently
when it did not — see `RedisEventBroker.publish`'s degraded-delivery
handling. A broker's failure to publish/subscribe must never crash the
caller (the simulator's commit path, a REST handler, a watch evaluation);
it is logged loudly instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from app.domain.models import RaceEvent
from app.metrics import emit_event_broker_degraded
from app.observability import audit_log

logger = logging.getLogger(__name__)

# A subscriber callback: invoked with (race_id, event) for every event this
# process learns about, whether it originated locally or on another task.
EventHandler = Callable[[str, RaceEvent], Awaitable[None]]


@dataclass
class EventBrokerMetrics:
    """Structured delivery metrics — surfaced on /v1/system/health (or an
    equivalent diagnostics endpoint) and exported to CloudWatch via the
    structured JSON audit log, per this pass's Phase 4 observability
    requirements. Never silently pretend delivery succeeded: `degraded`
    increments any time a publish/subscribe operation could not be
    completed as designed, even if the broker chose to continue serving
    locally-originated events afterward."""

    published: int = 0
    received: int = 0
    publish_failures: int = 0
    degraded_events: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "published": self.published,
            "received": self.received,
            "publish_failures": self.publish_failures,
            "degraded_events": self.degraded_events,
        }


class EventBroker(Protocol):
    """Minimal pub/sub contract every implementation must satisfy."""

    metrics: EventBrokerMetrics

    @property
    def is_connected(self) -> bool:
        """AWS hackathon-final pass, phase 4/5: true iff this broker is
        currently able to fan events out across instances. Surfaced on
        `GET /health` (see routes.py) so ECS's own health check —
        already what drives `BackendService`'s `DeploymentCircuitBreaker`
        and target-group health — can detect "this task cannot currently
        do cross-instance fan-out" as a real readiness failure, not just
        a background warning log. `InMemoryEventBroker` is always
        trivially connected (there is no network dependency to lose);
        `RedisEventBroker` reflects its actual subscriber-connection
        state."""
        ...

    async def start(self, on_event: EventHandler) -> None:
        """Begin listening for events produced by ANY process (including
        this one, for `InMemoryEventBroker`; only other processes, for
        `RedisEventBroker`, which delivers locally-originated events
        directly rather than round-tripping them through the network)."""
        ...

    async def publish(self, race_id: str, event: RaceEvent) -> None:
        """Announce that `event` happened for `race_id`. Must be safe to
        call even if `start()` was never called or the broker is
        currently degraded — publishing never raises."""
        ...

    async def stop(self) -> None:
        """Release any background listener task / network connection."""
        ...


class InMemoryEventBroker:
    """Single-process broker: `publish` invokes the registered handler
    directly. This is deliberately trivial — it exists so
    `RaceEventBroadcaster` can be written once, against `EventBroker`,
    with no special-casing for "am I clustered or not," and so the
    existing single-process test suite and local `docker-compose` dev
    loop need no external dependency at all."""

    def __init__(self) -> None:
        self.metrics = EventBrokerMetrics()
        self._handler: EventHandler | None = None

    @property
    def is_connected(self) -> bool:
        # Trivially always "connected" -- there is no network dependency
        # this implementation could lose. `start()` not yet having been
        # called is a distinct, real caller bug (see publish()'s own
        # handling below), not a "disconnected" state.
        return True

    async def start(self, on_event: EventHandler) -> None:
        self._handler = on_event

    async def publish(self, race_id: str, event: RaceEvent) -> None:
        self.metrics.published += 1
        if self._handler is None:
            # Nobody has called start() yet (e.g. broker constructed but
            # app startup hasn't wired it in) -- this is a caller bug in
            # local single-process mode, but per this module's own
            # contract it must never raise, only report degradation.
            self.metrics.degraded_events += 1
            logger.warning(
                "InMemoryEventBroker.publish called before start(); event dropped for race_id=%s",
                race_id,
            )
            return
        self.metrics.received += 1
        await self._handler(race_id, event)

    async def stop(self) -> None:
        self._handler = None


class RedisEventBroker:
    """Cross-instance broker backed by Redis/Valkey pub/sub (Amazon
    ElastiCache Serverless in the deployed stack). One channel per
    race_id (`pitwall:events:{race_id}`) keeps fan-out scoped -- a task
    with no clients watching a given race never has to deserialize
    events for it.

    Reconnect behavior: the background subscriber loop retries a dropped
    connection with capped exponential backoff. While disconnected, this
    process:
      - can still deliver events for LOCALLY-connected clients when they
        originate from ITS OWN publish() calls (best-effort: dispatched
        directly, not only through Redis -- see publish() below), so a
        transient Redis outage degrades cross-instance delivery, not
        single-instance delivery;
      - increments `metrics.degraded_events` and logs a loud warning for
        every publish issued while disconnected, rather than silently
        reporting success.
    This is the "graceful degradation with visible warnings, never
    silently pretend delivery succeeded" requirement from this pass.
    """

    RECONNECT_MIN_DELAY = 0.5
    RECONNECT_MAX_DELAY = 10.0

    def __init__(self, redis_url: str, *, redis_client_factory=None) -> None:
        self._redis_url = redis_url
        self._redis_client_factory = redis_client_factory
        self.metrics = EventBrokerMetrics()
        self._handler: EventHandler | None = None
        self._client = None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._stopped = False
        # Redis pub/sub delivers a published message back to every
        # subscriber on that channel, INCLUDING the publisher itself if
        # it is also subscribed (which every RedisEventBroker always is,
        # via _listen_forever's psubscribe). Without this tag,
        # `publish()`'s direct local-handler call above would be followed
        # a moment later by a second, indistinguishable delivery of the
        # exact same event via the subscriber loop -- a real duplicate-
        # delivery bug the two-instance test (tests/test_event_broker.py)
        # caught. Every outgoing message carries this instance's own id;
        # `_handle_wire_message` drops anything whose origin matches.
        self._instance_id = uuid.uuid4().hex

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _make_client(self):
        if self._redis_client_factory is not None:
            return self._redis_client_factory()
        import redis.asyncio as redis_asyncio  # local import: optional dependency, only needed for event_broker=redis

        return redis_asyncio.from_url(self._redis_url, decode_responses=True)

    async def start(self, on_event: EventHandler) -> None:
        self._handler = on_event
        self._stopped = False
        self._listener_task = asyncio.create_task(self._listen_forever())

    async def _listen_forever(self) -> None:
        delay = self.RECONNECT_MIN_DELAY
        while not self._stopped:
            try:
                self._client = self._make_client()
                self._pubsub = self._client.pubsub()
                await self._pubsub.psubscribe("pitwall:events:*")
                self._connected.set()
                delay = self.RECONNECT_MIN_DELAY
                async for message in self._pubsub.listen():
                    if self._stopped:
                        break
                    if message is None or message.get("type") != "pmessage":
                        continue
                    await self._handle_wire_message(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - any connection/protocol failure means "reconnect", not "crash the app"
                self._connected.clear()
                logger.warning(
                    "RedisEventBroker lost its subscriber connection; reconnecting in %.1fs",
                    delay,
                    exc_info=True,
                )
                audit_log(
                    "event_broker_degraded",
                    reason="subscriber_connection_lost",
                    retry_in_seconds=delay,
                )
                emit_event_broker_degraded(reason="subscriber_connection_lost")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

    async def _handle_wire_message(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
            origin_instance_id = envelope["origin_instance_id"]
            race_id = envelope["race_id"]
            event = RaceEvent.model_validate(envelope["event"])
        except Exception:  # noqa: BLE001 - a malformed message must never crash the listener loop
            logger.warning("RedisEventBroker discarded a malformed pub/sub message", exc_info=True)
            return
        if origin_instance_id == self._instance_id:
            # Our own publish() already delivered this locally -- this is
            # the pub/sub echo of that same message, not a new event from
            # another task. See __init__'s comment on _instance_id.
            return
        self.metrics.received += 1
        if self._handler is not None:
            await self._handler(race_id, event)

    async def publish(self, race_id: str, event: RaceEvent) -> None:
        self.metrics.published += 1
        # Always deliver to this process's own locally-connected clients
        # directly -- a Redis outage must not also break single-instance
        # delivery for the task that actually produced the event.
        if self._handler is not None:
            await self._handler(race_id, event)

        if self._client is None or not self._connected.is_set():
            self.metrics.degraded_events += 1
            logger.warning(
                "RedisEventBroker.publish: no connected Redis client -- event for race_id=%s was "
                "delivered locally only, NOT fanned out to other instances",
                race_id,
            )
            audit_log(
                "event_broker_degraded",
                reason="publish_without_connected_broker",
                race_id=race_id,
            )
            emit_event_broker_degraded(reason="publish_without_connected_broker")
            return

        try:
            payload = json.dumps(
                {
                    "origin_instance_id": self._instance_id,
                    "race_id": race_id,
                    "event": event.model_dump(mode="json"),
                }
            )
            await self._client.publish(f"pitwall:events:{race_id}", payload)
        except Exception:  # noqa: BLE001 - publish failures degrade cross-instance delivery, they never crash the caller
            self.metrics.publish_failures += 1
            self.metrics.degraded_events += 1
            logger.warning("RedisEventBroker failed to publish event for race_id=%s", race_id, exc_info=True)
            audit_log("event_broker_degraded", reason="publish_failed", race_id=race_id)
            emit_event_broker_degraded(reason="publish_failed")

    async def stop(self) -> None:
        # Both `except ... pass` blocks below are best-effort TEARDOWN of
        # a Redis connection this process is shutting down anyway --
        # swallowing a close/disconnect error here is deliberate (an
        # already-broken connection failing to close cleanly must never
        # prevent the rest of shutdown from proceeding), not a hidden
        # failure mode a caller could be surprised by. Suppressed narrowly
        # (bandit B110) on each `except` line itself below, not repo-wide.
        self._stopped = True
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001 # nosec B110
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 # nosec B110
                pass


def build_event_broker(settings) -> EventBroker:
    """Factory mirroring `app/repositories/factory.py`'s pattern: one
    explicit setting (`event_broker`) decides the implementation, no
    inference from other settings, and an unrecognized value fails loudly
    at startup rather than silently falling back to a weaker mode."""
    if settings.event_broker == "memory":
        return InMemoryEventBroker()
    if settings.event_broker == "redis":
        if not settings.redis_url:
            raise RuntimeError("EVENT_BROKER=redis requires REDIS_URL to be set")
        return RedisEventBroker(settings.redis_url)
    raise RuntimeError(f"Unknown EVENT_BROKER setting: {settings.event_broker!r} (expected 'memory' or 'redis')")
