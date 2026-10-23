"""Tests for the cross-instance event broker (P0-3).

`TestTwoInstanceFanOut` is the test explicitly required by this pass: two
`RaceEventBroadcaster` instances, each standing in for a separate ECS
task, sharing one `fakeredis` server as their "ElastiCache" -- proving
that an event produced through instance B actually reaches a client
connected only to instance A, without duplication, and that ordering is
preserved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.models import RaceEvent, RaceEventType
from app.services.event_broadcaster import RaceEventBroadcaster
from app.services.event_broker import (
    InMemoryEventBroker,
    RedisEventBroker,
    build_event_broker,
)


_FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(state_version: int, event_type: RaceEventType = RaceEventType.RACE_STATE) -> RaceEvent:
    # A fixed generated_at (rather than datetime.now()) so two calls with
    # the same state_version compare equal -- these tests assert on event
    # identity/ordering, not on wall-clock timing.
    return RaceEvent(
        type=event_type,
        race_id="race-1",
        run_id="run-1",
        state_version=state_version,
        generated_at=_FIXED_TIMESTAMP,
        payload={"state_version": state_version},
    )


class _FakeWebSocket:
    """Minimal stand-in for fastapi.WebSocket: records every JSON payload
    sent to it, in order, without needing a real ASGI connection."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.received.append(payload)


class TestInMemoryEventBroker:
    async def test_publish_before_start_is_degraded_not_a_crash(self):
        broker = InMemoryEventBroker()
        await broker.publish("race-1", _event(1))  # start() never called
        assert broker.metrics.degraded_events == 1
        assert broker.metrics.published == 1

    async def test_publish_after_start_reaches_the_handler(self):
        broker = InMemoryEventBroker()
        received = []

        async def handler(race_id, event):
            received.append((race_id, event))

        await broker.start(handler)
        await broker.publish("race-1", _event(1))
        assert received == [("race-1", _event(1))]
        assert broker.metrics.published == 1
        assert broker.metrics.received == 1
        assert broker.metrics.degraded_events == 0


class TestBuildEventBroker:
    def test_memory_is_the_default(self):
        class _Settings:
            event_broker = "memory"
            redis_url = None

        broker = build_event_broker(_Settings())
        assert isinstance(broker, InMemoryEventBroker)

    def test_redis_without_a_url_fails_loudly_at_startup(self):
        class _Settings:
            event_broker = "redis"
            redis_url = None

        with pytest.raises(RuntimeError, match="REDIS_URL"):
            build_event_broker(_Settings())

    def test_unknown_broker_setting_fails_loudly_at_startup(self):
        class _Settings:
            event_broker = "kafka"
            redis_url = None

        with pytest.raises(RuntimeError, match="Unknown EVENT_BROKER"):
            build_event_broker(_Settings())


class TestTwoInstanceFanOut:
    """The exact test this pass's P0-3 requirement calls for: two
    processes ("instance A" and "instance B"), a client connected only to
    A, an event produced only through B, proving delivery, no
    duplication, and preserved ordering."""

    def _make_broker_pair(self):
        """Two RedisEventBrokers sharing one in-memory fakeredis server —
        this is the hermetic stand-in for two ECS tasks both pointed at
        the same ElastiCache Serverless endpoint."""
        import fakeredis.aioredis as fakeredis_aioredis

        shared_server = fakeredis_aioredis.FakeServer()

        def factory_a():
            return fakeredis_aioredis.FakeRedis(server=shared_server, decode_responses=True)

        def factory_b():
            return fakeredis_aioredis.FakeRedis(server=shared_server, decode_responses=True)

        broker_a = RedisEventBroker("redis://fake", redis_client_factory=factory_a)
        broker_b = RedisEventBroker("redis://fake", redis_client_factory=factory_b)
        return broker_a, broker_b

    @pytest.mark.asyncio
    async def test_event_from_instance_b_reaches_a_client_connected_only_to_instance_a(self):
        broker_a, broker_b = self._make_broker_pair()
        instance_a = RaceEventBroadcaster()
        instance_b = RaceEventBroadcaster()
        await instance_a.attach_broker(broker_a)
        await instance_b.attach_broker(broker_b)

        # Give each broker's background subscriber loop a moment to
        # actually finish (re)subscribing before we publish, matching how
        # a real deployment's tasks are up and subscribed well before
        # traffic arrives.
        await broker_a._connected.wait()  # noqa: SLF001 - test-only introspection of connection readiness
        await broker_b._connected.wait()  # noqa: SLF001

        client_on_a = _FakeWebSocket()
        await instance_a.connect("race-1", client_on_a)

        try:
            # The event is produced entirely through instance B -- the
            # client is connected to A and never touches B directly.
            await instance_b.broadcast("race-1", _event(1))

            # Cross-process delivery goes over the fake network, so poll
            # briefly rather than assuming synchronous delivery.
            for _ in range(50):
                if client_on_a.received:
                    break
                import asyncio

                await asyncio.sleep(0.02)

            assert len(client_on_a.received) == 1
            assert client_on_a.received[0]["state_version"] == 1

            # No duplication: the event must not also have been delivered
            # a second time to the same connection (e.g. via both the
            # direct local path AND a self-subscribed loop-back).
            await instance_b.broadcast("race-1", _event(2))
            for _ in range(50):
                if len(client_on_a.received) >= 2:
                    break
                import asyncio

                await asyncio.sleep(0.02)
            assert len(client_on_a.received) == 2

            # Ordering preserved: state_version 1 then 2, not reordered.
            assert [msg["state_version"] for msg in client_on_a.received] == [1, 2]
        finally:
            await instance_a.disconnect("race-1", client_on_a)
            await broker_a.stop()
            await broker_b.stop()

    @pytest.mark.asyncio
    async def test_event_ids_are_not_duplicated_when_the_producing_instance_also_has_a_local_client(self):
        # Instance B has its OWN locally-connected client too. When B
        # produces an event, its local client must receive it exactly
        # once (direct local delivery), not once locally + once more via
        # the round trip through the broker.
        broker_a, broker_b = self._make_broker_pair()
        instance_a = RaceEventBroadcaster()
        instance_b = RaceEventBroadcaster()
        await instance_a.attach_broker(broker_a)
        await instance_b.attach_broker(broker_b)
        await broker_a._connected.wait()  # noqa: SLF001
        await broker_b._connected.wait()  # noqa: SLF001

        client_on_b = _FakeWebSocket()
        await instance_b.connect("race-1", client_on_b)

        try:
            await instance_b.broadcast("race-1", _event(1))

            import asyncio

            await asyncio.sleep(0.1)  # let any (incorrect) round-trip delivery have time to arrive

            assert len(client_on_b.received) == 1
        finally:
            await instance_b.disconnect("race-1", client_on_b)
            await broker_a.stop()
            await broker_b.stop()


class TestRedisEventBrokerDegradation:
    @pytest.mark.asyncio
    async def test_publish_with_no_connected_client_still_delivers_locally_and_reports_degraded(self):
        broker = RedisEventBroker("redis://unreachable-host-for-this-test:1")
        received = []

        async def handler(race_id, event):
            received.append((race_id, event))

        broker._handler = handler  # noqa: SLF001 - simulate start() having registered a handler without a live connection

        await broker.publish("race-1", _event(1))

        # Local delivery still happens even though no broker connection exists.
        assert received == [("race-1", _event(1))]
        assert broker.metrics.degraded_events == 1
        assert broker.metrics.publish_failures == 0  # never attempted a network call at all
