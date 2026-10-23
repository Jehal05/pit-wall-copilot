"""Repository abstractions for the Strategy Watch Engine — Gate 2.

Mirrors the pattern established in repositories/base.py for race state: an
abstract interface so domain/service code never depends on a specific
storage technology, plus a documented conditional-write guarantee for
idempotency.

Two implementations exist: the in-memory one (repositories/watch_memory.py,
the default, used by local dev and most of the test suite) and a
DynamoDB-backed one (repositories/watch_dynamodb.py, selected the same way
as race state via `STATE_REPOSITORY=dynamodb` — see
repositories/factory.py and watch_dynamodb.py's own module docstring for
the table/sk-prefix design). This fixes the previously-documented Gate 2
limitation that only race state had a DynamoDB path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.domain.watch_models import Alert, Watch


class WatchUpdateConflict(Exception):
    """Raised by `update_watch` when `require_last_evaluated_state_version_below`
    was given and the STORED watch's `last_evaluated_state_version` is
    already >= that value — i.e. a concurrent/duplicate/out-of-order
    evaluation lost the race to advance this watch (spec: "Handle
    conditional conflicts safely. Prevent an older evaluation from
    overwriting a newer watch."). Callers (WatchEvaluator) treat this as
    "someone else already processed this or a newer state_version" and
    silently skip the update rather than raising further — the caller's
    own alert, if any, was already recorded idempotently before this call,
    so nothing is lost, only a redundant watch-status write is dropped."""


class WatchRepository(ABC):
    """Persistence boundary for Strategy Watches."""

    @abstractmethod
    async def create_watch(self, watch: Watch) -> Watch:
        """Persist a newly created watch."""

    @abstractmethod
    async def get_watch(self, race_id: str, watch_id: str) -> Optional[Watch]:
        """Return a watch by id, or None if it does not exist for this race."""

    @abstractmethod
    async def update_watch(
        self, watch: Watch, *, require_last_evaluated_state_version_below: Optional[int] = None
    ) -> Watch:
        """Persist an updated watch (status/probability/evaluated version).

        `require_last_evaluated_state_version_below`, when given, makes
        this a conditional/optimistic-concurrency write: the update is
        applied only if the CURRENTLY STORED watch's
        `last_evaluated_state_version` is strictly less than this value
        (or the watch doesn't exist yet, e.g. a first evaluation racing a
        concurrent one). If the stored value is already >= this value —
        a concurrent evaluation for the same or a newer state_version won
        the race — raises `WatchUpdateConflict` instead of overwriting it
        (spec repair 6: "Prevent an older evaluation from overwriting a
        newer watch... Handle conditional conflicts safely"). Omitted
        (None, the default), the write is unconditional, as before."""

    @abstractmethod
    async def delete_watch(self, race_id: str, watch_id: str) -> bool:
        """Delete a watch. Returns True if it existed and was deleted."""

    @abstractmethod
    async def list_watches(self, race_id: str) -> tuple[Watch, ...]:
        """Return all watches ever created for a race, across all runs."""

    @abstractmethod
    async def list_active_watches(self, race_id: str, run_id: str) -> tuple[Watch, ...]:
        """Return only the watches created under the given (current) run_id.

        A watch created under a previous run is never returned here, even
        if it still exists in storage — this is what "invalidates" it
        without requiring an explicit delete (spec 48)."""


class AlertRepository(ABC):
    """Persistence boundary for Alerts, with mandatory idempotent writes."""

    @abstractmethod
    async def record_transition(self, idempotency_key: str, alert: Alert) -> tuple[Alert, bool]:
        """Persist an alert transition exactly once.

        If `idempotency_key` (spec 49: `run_id:watch_id:state_version:new_status`)
        has already been recorded, returns the EXISTING alert and False —
        the caller must not publish another alert for it. Otherwise
        persists `alert` and returns (alert, True).

        NOTE: this method deliberately says nothing about the WATCH's own
        update — see `TransitionWriter.write_transition` below, which is
        what `WatchEvaluator` actually calls for a status-changing
        evaluation (spec v1.1 correction 9). `record_transition` on its own
        remains here because it is still useful in isolation (e.g. tests
        that exercise idempotency directly, or a future caller that only
        needs the alert half), but it is NOT what closes the
        alert-committed/watch-not-yet-updated crash window that motivated
        `TransitionWriter`.
        """

    @abstractmethod
    async def get_alert(self, race_id: str, alert_id: str) -> Optional[Alert]:
        """Return one alert by id."""

    @abstractmethod
    async def list_alerts(self, race_id: str, watch_id: Optional[str] = None) -> tuple[Alert, ...]:
        """Return alerts for a race, optionally filtered by watch, newest first."""


class TransitionWriter(ABC):
    """Spec v1.1 correction 9: atomically commits an alert transition
    together with the watch update it corresponds to, closing a real
    crash-recovery gap that a plain two-step
    `AlertRepository.record_transition()` + `WatchRepository.update_watch()`
    sequence leaves open against a DURABLE backend (DynamoDB):

    If a process crashes (Lambda timeout, container restart, ...) AFTER
    the alert half commits but BEFORE the watch half does, the watch is
    left durably showing its OLD status/last_evaluated_state_version. The
    alert itself is safely idempotent against an exact retry of the SAME
    state_version (the idempotency_key is stable, so a retry is correctly
    a no-op) — but it is NOT safe against the NEXT, genuinely newer
    state_version that arrives after the crash: evaluating that newer
    state against the stale, un-updated watch can recompute the SAME
    logical CLOSED->OPEN (or OPEN->CLOSED) transition a second time, with
    a DIFFERENT idempotency_key (it embeds state_version), producing a
    real duplicate alert for a transition that already fired. This is a
    genuine bug, not a theoretical one — it survives a single-process
    in-memory restart just as easily discovers it does NOT apply there
    (memory is wiped on crash, so there is nothing stale left to read),
    which is why `InMemoryTransitionWriter` below does not need DynamoDB's
    `TransactWriteItems`-based fix; only a durable backend can leave a
    stale-but-persisted watch behind a crash.

    `DynamoDBTransitionWriter` closes this by writing the idempotency
    marker, the alert item, and the watch item together in ONE
    `TransactWriteItems` call: either all three commit, or none do — there
    is no possible crash window between "alert committed" and "watch
    committed" because there is no longer a second, separate write.
    """

    @abstractmethod
    async def write_transition(
        self,
        idempotency_key: str,
        alert: Alert,
        updated_watch: Watch,
        *,
        require_last_evaluated_state_version_below: Optional[int] = None,
    ) -> tuple[Optional[Alert], bool]:
        """Atomically commit `alert` (idempotently, by `idempotency_key`)
        together with `updated_watch` (conditionally, exactly like
        `WatchRepository.update_watch`'s own
        `require_last_evaluated_state_version_below` contract).

        Returns (published_alert, created):
          - (existing_alert, False): `idempotency_key` was already
            recorded by an earlier, FULLY COMMITTED call (both alert and
            watch went through together that time) — the caller must not
            publish again, and must not touch the watch (it was already
            updated, atomically, by whichever call actually won).
          - (None, False): a genuinely new `idempotency_key`, but the
            watch's conditional check failed — a concurrent or crash-
            recovered newer evaluation already advanced the watch past
            `updated_watch`'s baseline. The WHOLE write was rolled back,
            including the alert half: nothing was persisted. The caller
            treats this exactly like the old `WatchUpdateConflict`
            behavior — skip silently, publish nothing.
          - (alert, True): both halves committed together. The caller
            publishes `alert`.
        """
