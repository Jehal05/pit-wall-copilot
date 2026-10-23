"""In-memory implementations of the watch/alert repositories — Gate 2.

Used for local development, the demo, and all automated tests, matching
the same rationale as repositories/memory.py: no AWS credentials are
required and state does not survive a process restart, which is
acceptable at this stage.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.domain.watch_models import Alert, Watch
from app.repositories.watch_base import AlertRepository, TransitionWriter, WatchRepository, WatchUpdateConflict


class InMemoryWatchRepository(WatchRepository):
    def __init__(self) -> None:
        # keyed by (race_id, watch_id)
        self._watches: dict[tuple[str, str], Watch] = {}
        self._lock = asyncio.Lock()

    async def create_watch(self, watch: Watch) -> Watch:
        async with self._lock:
            self._watches[(watch.race_id, watch.watch_id)] = watch
            return watch

    async def get_watch(self, race_id: str, watch_id: str) -> Optional[Watch]:
        async with self._lock:
            return self._watches.get((race_id, watch_id))

    async def update_watch(
        self, watch: Watch, *, require_last_evaluated_state_version_below: Optional[int] = None
    ) -> Watch:
        # The check-then-write below is safe without a finer-grained lock
        # because asyncio is single-threaded/cooperative and there is no
        # `await` between the check and the write — nothing else can run
        # in between. See watch_base.py's docstring for the semantics.
        async with self._lock:
            if require_last_evaluated_state_version_below is not None:
                stored = self._watches.get((watch.race_id, watch.watch_id))
                if stored is not None and stored.last_evaluated_state_version >= require_last_evaluated_state_version_below:
                    raise WatchUpdateConflict(
                        f"watch {watch.watch_id}: stored last_evaluated_state_version "
                        f"{stored.last_evaluated_state_version} >= {require_last_evaluated_state_version_below}"
                    )
            self._watches[(watch.race_id, watch.watch_id)] = watch
            return watch

    async def delete_watch(self, race_id: str, watch_id: str) -> bool:
        async with self._lock:
            return self._watches.pop((race_id, watch_id), None) is not None

    async def list_watches(self, race_id: str) -> tuple[Watch, ...]:
        async with self._lock:
            return tuple(w for (rid, _wid), w in self._watches.items() if rid == race_id)

    async def list_active_watches(self, race_id: str, run_id: str) -> tuple[Watch, ...]:
        async with self._lock:
            return tuple(
                w
                for (rid, _wid), w in self._watches.items()
                if rid == race_id and w.run_id == run_id
            )


class InMemoryAlertRepository(AlertRepository):
    def __init__(self) -> None:
        # keyed by idempotency_key -> Alert (the conditional-write guard)
        self._by_idempotency_key: dict[str, Alert] = {}
        # keyed by (race_id, alert_id) -> Alert (fast point lookup)
        self._by_id: dict[tuple[str, str], Alert] = {}
        self._lock = asyncio.Lock()

    async def record_transition(self, idempotency_key: str, alert: Alert) -> tuple[Alert, bool]:
        async with self._lock:
            existing = self._by_idempotency_key.get(idempotency_key)
            if existing is not None:
                return existing, False
            self._by_idempotency_key[idempotency_key] = alert
            self._by_id[(alert.race_id, alert.alert_id)] = alert
            return alert, True

    async def get_alert(self, race_id: str, alert_id: str) -> Optional[Alert]:
        async with self._lock:
            return self._by_id.get((race_id, alert_id))

    async def list_alerts(self, race_id: str, watch_id: Optional[str] = None) -> tuple[Alert, ...]:
        async with self._lock:
            alerts = [a for (rid, _aid), a in self._by_id.items() if rid == race_id]
            if watch_id is not None:
                alerts = [a for a in alerts if a.watch_id == watch_id]
            alerts.sort(key=lambda a: (a.state_version, a.created_at), reverse=True)
            return tuple(alerts)


class InMemoryTransitionWriter(TransitionWriter):
    """Spec v1.1 correction 9. In-memory storage does not need DynamoDB's
    `TransactWriteItems` fix — the crash window `TransitionWriter`'s class
    docstring describes only matters for a backend whose state SURVIVES a
    process crash while the process itself does not. A crashed process
    here loses the alert, the watch, and everything else together, so
    there is never a stale-but-persisted watch left behind for a later
    evaluation to misread. This class exists only so `WatchEvaluator` can
    use the same `TransitionWriter` interface for both backends; it still
    makes the two writes atomic with respect to CONCURRENT coroutines
    within this one process (not just crash recovery) by taking both
    repositories' locks in a fixed order (watches, then alerts) for the
    duration of both writes."""

    def __init__(self, watch_repository: InMemoryWatchRepository, alert_repository: InMemoryAlertRepository) -> None:
        self._watches = watch_repository
        self._alerts = alert_repository

    async def write_transition(
        self,
        idempotency_key: str,
        alert: Alert,
        updated_watch: Watch,
        *,
        require_last_evaluated_state_version_below: Optional[int] = None,
    ) -> tuple[Optional[Alert], bool]:
        # Fixed lock order (watches, then alerts) avoids deadlocking against
        # any other code path that might one day need both locks at once.
        async with self._watches._lock:  # noqa: SLF001 — intentional, see class docstring
            async with self._alerts._lock:  # noqa: SLF001
                existing = self._alerts._by_idempotency_key.get(idempotency_key)  # noqa: SLF001
                if existing is not None:
                    return existing, False

                if require_last_evaluated_state_version_below is not None:
                    stored_watch = self._watches._watches.get(  # noqa: SLF001
                        (updated_watch.race_id, updated_watch.watch_id)
                    )
                    if (
                        stored_watch is not None
                        and stored_watch.last_evaluated_state_version >= require_last_evaluated_state_version_below
                    ):
                        # Whole write rolled back — matches
                        # DynamoDBTransitionWriter's TransactWriteItems
                        # cancellation behavior exactly: neither the alert
                        # nor the watch is touched.
                        return None, False

                self._alerts._by_idempotency_key[idempotency_key] = alert  # noqa: SLF001
                self._alerts._by_id[(alert.race_id, alert.alert_id)] = alert  # noqa: SLF001
                self._watches._watches[(updated_watch.race_id, updated_watch.watch_id)] = updated_watch  # noqa: SLF001
                return alert, True
