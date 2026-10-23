"""WebSocket event fan-out — spec v1.1 repair 3.

Replaces polling-only delivery of race-state/alert/agent updates with a
real push channel: `/v1/races/{race_id}/ws`. This module holds no race
logic of its own — it is purely "who is currently connected for this
race_id, and how do I get a `RaceEvent` to all of them" — everything that
decides WHEN an event fires still lives where it always did (the
simulator's tick loop, `WatchEvaluator.evaluate`, the strategy/agent REST
handlers). `app/main.py`'s lifespan wires those call sites to
`RaceEventBroadcaster.broadcast`.

Repair 3 also requires the REST polling loop to remain as a fallback
("poll race state every two seconds" when WebSocket is unavailable) —
that fallback already existed before this repair (Gate 0) and is
unchanged; this module only adds the push path alongside it. A client
that cannot reach the WebSocket endpoint at all continues to work exactly
as before.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from app.domain.models import RaceEvent

logger = logging.getLogger(__name__)


class RaceEventBroadcaster:
    """Tracks connected WebSocket clients per race_id and fans out
    `RaceEvent`s to them. Safe for concurrent use — all mutation of the
    per-race connection set happens under a single asyncio.Lock, since
    `connect`/`disconnect`/`broadcast` can all be invoked concurrently
    (a new client connecting while a broadcast to existing clients is in
    flight, for instance).

    AWS hackathon-final pass, P0-3: every call site in this codebase
    (app/main.py's `_on_state_committed`, and the strategy/agent REST
    handlers in app/api/routes.py) still just calls `broadcast(race_id,
    event)` exactly as before -- nothing about this class's public
    surface changed. What changed is what `broadcast` does internally:
    it now hands the event to an `app.services.event_broker.EventBroker`
    (see that module's docstring) instead of delivering straight to this
    process's own local connections. The broker is responsible for
    calling back into `deliver_local` -- once for the event as produced
    here, and (for `RedisEventBroker`) again for every event received
    from OTHER backend tasks over the shared pub/sub channel. A
    broadcaster with no broker attached (`attach_broker` never called --
    true only in a few narrow unit tests that construct this class
    directly) falls back to delivering locally only, matching this
    class's pre-P0-3 behavior exactly."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._broker = None
        # AWS hackathon-final pass, P0-7: a per-CONNECTION send lock
        # (not a global per-race lock, which would needlessly serialize
        # delivery to unrelated clients). This is the "server-side
        # snapshot/broadcast barrier": before this pass, the WS route
        # (api/routes.py) registered a new connection via `connect()`
        # and THEN, in a separate step, fetched and sent an initial
        # snapshot with a bare `websocket.send_json(...)` call -- fully
        # unsynchronized with `deliver_local`'s own send loop. A
        # concurrent broadcast landing in that window could interleave
        # its own `send_json` call on the SAME connection with the
        # route's initial-snapshot send (Starlette's WebSocket.send is
        # not safe for concurrent callers), and there was no guarantee
        # which one the client's transport would actually see first.
        # `send_direct` (used by the WS route for the initial snapshot)
        # and `deliver_local` (used for every live broadcast) now both
        # acquire the SAME per-connection lock before touching that
        # connection's socket, so the two can never interleave -- each
        # send to a given connection is now fully serialized end-to-end,
        # while sends to DIFFERENT connections remain fully concurrent.
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def attach_broker(self, broker) -> None:
        """Wires this broadcaster to an `EventBroker` so that `broadcast`
        fans out cross-instance instead of only to this process's own
        connections. Registers `deliver_local` as the broker's callback
        so events produced on ANY task (including this one) reach this
        task's locally-connected clients."""
        self._broker = broker
        await broker.start(lambda race_id, event: self.deliver_local(race_id, event))

    async def connect(self, race_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(race_id, set()).add(websocket)
            self._send_locks[websocket] = asyncio.Lock()

    async def disconnect(self, race_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(race_id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    del self._connections[race_id]
            self._send_locks.pop(websocket, None)

    async def send_direct(self, race_id: str, websocket: WebSocket, event: RaceEvent) -> bool:
        """Sends `event` to exactly ONE connection (used by the WS route
        for the initial snapshot — see this class's own docstring on
        `_send_locks` for why this must share a lock with
        `deliver_local`). Returns False (and drops the dead connection,
        same as `deliver_local`) if the send fails; True on success. A
        caller that races this against `connect`/`disconnect` for the
        SAME connection may find the send lock already gone (the
        connection was disconnected concurrently) — treated as an
        ordinary failed send, not an error."""
        send_lock = self._send_locks.get(websocket)
        if send_lock is None:
            return False
        async with send_lock:
            try:
                await websocket.send_json(event.model_dump(mode="json"))
                return True
            except Exception:  # noqa: BLE001 - a failed direct send means this connection is dead
                async with self._lock:
                    self._connections.get(race_id, set()).discard(websocket)
                    self._send_locks.pop(websocket, None)
                return False

    def connection_count(self, race_id: str) -> int:
        return len(self._connections.get(race_id, ()))

    async def broadcast(self, race_id: str, event: RaceEvent) -> None:
        """Announces `event` for `race_id`. If a broker is attached
        (the normal case — see app/main.py's lifespan), delivery is
        broker-mediated so every backend task with locally-connected
        clients for this race receives it, not just this process. With
        no broker attached, falls back to `deliver_local` directly —
        identical to this method's pre-P0-3 behavior, which is what
        existing unit tests that construct `RaceEventBroadcaster()` bare
        (no broker) still exercise."""
        if self._broker is not None:
            await self._broker.publish(race_id, event)
            return
        await self.deliver_local(race_id, event)

    async def deliver_local(self, race_id: str, event: RaceEvent) -> None:
        """Sends `event` to every currently-connected client for
        `race_id` ON THIS PROCESS ONLY. A client whose send fails
        (already disconnected, but the server hasn't processed the close
        yet) is dropped from the connection set rather than raising —
        one dead connection must never prevent delivery to the others,
        and must never crash the caller (the simulator's own commit
        path, watch evaluation, a REST handler, or an EventBroker
        delivering an event received from another task)."""
        async with self._lock:
            connections = list(self._connections.get(race_id, ()))
            send_locks = {ws: self._send_locks[ws] for ws in connections if ws in self._send_locks}
        if not connections:
            return

        payload = event.model_dump(mode="json")
        dead: list[WebSocket] = []
        for websocket in connections:
            # P0-7: acquire the SAME per-connection lock `send_direct`
            # uses, so a live broadcast can never interleave with the WS
            # route's initial-snapshot send for this connection (see this
            # class's own docstring). A connection with no lock entry was
            # already disconnected concurrently -- skip it rather than
            # send to a socket that's already gone.
            send_lock = send_locks.get(websocket)
            if send_lock is None:
                continue
            async with send_lock:
                try:
                    await websocket.send_json(payload)
                except Exception:  # noqa: BLE001 - any send failure means this connection is dead
                    dead.append(websocket)

        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections.get(race_id, set()).discard(websocket)
                    self._send_locks.pop(websocket, None)
