"""Unit tests for RaceEventBroadcaster — spec v1.1 repair 3.

Uses a small fake WebSocket double (no real ASGI/network involved) so
connect/disconnect/broadcast/dead-connection-cleanup are tested fast and
deterministically. Real-transport WebSocket coverage (an actual client
connecting over the app's WS route) lives in
tests/test_websocket_events.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.domain.models import RaceEvent, RaceEventType
from app.services.event_broadcaster import RaceEventBroadcaster


class _FakeWebSocket:
    def __init__(self, *, fail: bool = False):
        self.accepted = False
        self.sent: list[dict] = []
        self._fail = fail

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        if self._fail:
            raise RuntimeError("connection is dead")
        self.sent.append(payload)


def _event(race_id: str = "demo-race-001", state_version: int = 1) -> RaceEvent:
    return RaceEvent(
        type=RaceEventType.RACE_STATE, race_id=race_id, run_id="run-a",
        state_version=state_version, generated_at=datetime.now(timezone.utc), payload={"lap": 24},
    )


async def test_connect_accepts_and_registers():
    broadcaster = RaceEventBroadcaster()
    ws = _FakeWebSocket()
    await broadcaster.connect("demo-race-001", ws)
    assert ws.accepted is True
    assert broadcaster.connection_count("demo-race-001") == 1


async def test_broadcast_delivers_to_all_connected_clients_for_that_race():
    broadcaster = RaceEventBroadcaster()
    ws_a, ws_b = _FakeWebSocket(), _FakeWebSocket()
    await broadcaster.connect("demo-race-001", ws_a)
    await broadcaster.connect("demo-race-001", ws_b)

    await broadcaster.broadcast("demo-race-001", _event())

    assert len(ws_a.sent) == 1
    assert len(ws_b.sent) == 1
    assert ws_a.sent[0]["type"] == "RACE_STATE"


async def test_broadcast_does_not_leak_across_different_races():
    broadcaster = RaceEventBroadcaster()
    ws_race_a = _FakeWebSocket()
    ws_race_b = _FakeWebSocket()
    await broadcaster.connect("race-a", ws_race_a)
    await broadcaster.connect("race-b", ws_race_b)

    await broadcaster.broadcast("race-a", _event(race_id="race-a"))

    assert len(ws_race_a.sent) == 1
    assert len(ws_race_b.sent) == 0


async def test_disconnect_removes_the_client():
    broadcaster = RaceEventBroadcaster()
    ws = _FakeWebSocket()
    await broadcaster.connect("demo-race-001", ws)
    await broadcaster.disconnect("demo-race-001", ws)
    assert broadcaster.connection_count("demo-race-001") == 0

    await broadcaster.broadcast("demo-race-001", _event())
    assert ws.sent == []


async def test_a_dead_connection_is_dropped_without_blocking_delivery_to_others():
    broadcaster = RaceEventBroadcaster()
    dead = _FakeWebSocket(fail=True)
    alive = _FakeWebSocket()
    await broadcaster.connect("demo-race-001", dead)
    await broadcaster.connect("demo-race-001", alive)

    await broadcaster.broadcast("demo-race-001", _event())  # must not raise

    assert len(alive.sent) == 1
    assert broadcaster.connection_count("demo-race-001") == 1  # the dead one was pruned


async def test_broadcast_to_a_race_with_no_connections_is_a_silent_noop():
    broadcaster = RaceEventBroadcaster()
    await broadcaster.broadcast("nobody-connected", _event(race_id="nobody-connected"))  # must not raise


# --- P0-7: server-side snapshot/broadcast barrier -------------------------


class _RecordingWebSocket:
    """Like `_FakeWebSocket`, but every `send_json` call appends to a
    SHARED, ordered log (not just this instance's own `sent` list) with
    an artificial await point in the middle of the send -- so two
    concurrent send attempts to the SAME connection can be forced to
    interleave if nothing prevents it. Used to prove `send_direct` and
    `deliver_local` cannot race each other on one connection (P0-7)."""

    def __init__(self, shared_log: list[str], *, delay_event: asyncio.Event | None = None):
        self.accepted = False
        self.sent: list[dict] = []
        self._shared_log = shared_log
        self._delay_event = delay_event

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self._shared_log.append(f"start:{payload.get('state_version')}")
        if self._delay_event is not None:
            # Yield control mid-send -- if the two send paths shared no
            # lock, the other send would run to completion right here,
            # interleaving its own start/end markers with this one's.
            await self._delay_event.wait()
        self.sent.append(payload)
        self._shared_log.append(f"end:{payload.get('state_version')}")


async def test_send_direct_and_a_concurrent_broadcast_never_interleave_on_the_same_connection():
    broadcaster = RaceEventBroadcaster()
    shared_log: list[str] = []
    delay_event = asyncio.Event()
    ws = _RecordingWebSocket(shared_log, delay_event=delay_event)
    await broadcaster.connect("demo-race-001", ws)

    # Start a `send_direct` (standing in for the WS route's initial
    # snapshot) but have it block mid-send (simulating a slow/contended
    # network write) until `delay_event` is set below.
    snapshot_task = asyncio.create_task(
        broadcaster.send_direct("demo-race-001", ws, _event(state_version=5))
    )
    await asyncio.sleep(0)  # let snapshot_task actually start and reach the mid-send await

    # A live broadcast fires WHILE the snapshot send is still in flight.
    # Without the shared per-connection lock, this would interleave its
    # own send_json call with the still-in-progress snapshot send.
    broadcast_task = asyncio.create_task(broadcaster.broadcast("demo-race-001", _event(state_version=6)))
    await asyncio.sleep(0)  # give it a chance to (incorrectly) start sending if unsynchronized

    # The broadcast must NOT have started its own send yet -- it should
    # be blocked waiting for the same per-connection lock the snapshot
    # send is still holding.
    assert shared_log == ["start:5"]

    delay_event.set()  # let the snapshot send complete
    await snapshot_task
    await broadcast_task

    # The snapshot's send must have fully completed (start AND end) BEFORE
    # the broadcast's send started -- never interleaved.
    assert shared_log == ["start:5", "end:5", "start:6", "end:6"]
    assert [item["state_version"] for item in ws.sent] == [5, 6]


async def test_send_direct_delivers_to_exactly_the_targeted_connection():
    broadcaster = RaceEventBroadcaster()
    shared_log_a: list[str] = []
    shared_log_b: list[str] = []
    ws_a = _RecordingWebSocket(shared_log_a)
    ws_b = _RecordingWebSocket(shared_log_b)
    await broadcaster.connect("demo-race-001", ws_a)
    await broadcaster.connect("demo-race-001", ws_b)

    ok = await broadcaster.send_direct("demo-race-001", ws_a, _event(state_version=1))

    assert ok is True
    assert len(ws_a.sent) == 1
    assert len(ws_b.sent) == 0


async def test_send_direct_to_a_dead_connection_drops_it_and_returns_false():
    broadcaster = RaceEventBroadcaster()
    ws = _FakeWebSocket(fail=True)
    await broadcaster.connect("demo-race-001", ws)

    ok = await broadcaster.send_direct("demo-race-001", ws, _event())

    assert ok is False
    assert broadcaster.connection_count("demo-race-001") == 0


async def test_send_direct_after_disconnect_fails_safely_without_raising():
    broadcaster = RaceEventBroadcaster()
    ws = _FakeWebSocket()
    await broadcaster.connect("demo-race-001", ws)
    await broadcaster.disconnect("demo-race-001", ws)

    ok = await broadcaster.send_direct("demo-race-001", ws, _event())  # must not raise

    assert ok is False
