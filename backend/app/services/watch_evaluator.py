"""Strategy Watch Engine — Gate 2.

Implements spec v1.1 section 18 ("Strategy Watch Engine") and section 49
("Watch Evaluation, Alert Snapshot and Idempotency").

In a full AWS deployment this evaluator is invoked by a Lambda subscribed
to DynamoDB Streams on the race-state table ("DynamoDB Streams SHALL
invoke the Watch Evaluator whenever race state changes" — spec 18). Gate 2
runs entirely in-process/in-memory (see repositories/watch_memory.py), so
the equivalent trigger is a direct callback invoked by SimulatorService
immediately after it commits a new accepted state — see
services/simulator.py's `on_state_committed` hook and app/main.py's
lifespan wiring. The evaluator's own logic (steps 1-6 below) is identical
either way; only how it gets invoked differs.
"""

from __future__ import annotations

import uuid
from typing import Optional

from app.domain.models import RaceState, is_state_stale, utc_now
from app.domain.watch_models import Alert, Watch, WatchStatus, WatchType
from app.repositories.watch_base import AlertRepository, TransitionWriter, WatchRepository, WatchUpdateConflict
from app.services.strategy_engine import StrategyEngine, StrategyValidationError


class WatchEvaluator:
    """Stateless coordinator: holds only repository/engine references, no
    race state of its own. Safe to invoke concurrently for different races
    (the underlying stores serialize their own writes)."""

    def __init__(
        self,
        watch_repository: WatchRepository,
        alert_repository: AlertRepository,
        strategy_engine: StrategyEngine,
        transition_writer: Optional[TransitionWriter] = None,
    ) -> None:
        self._watches = watch_repository
        self._alerts = alert_repository
        self._engine = strategy_engine
        # Spec v1.1 correction 9: when given, a status-CHANGING evaluation
        # (the branch that both records an alert AND updates the watch)
        # goes through this single atomic write instead of the old
        # record_transition()-then-update_watch() two-step — see
        # watch_dynamodb.py's module docstring and TransitionWriter's own
        # class docstring (watch_base.py) for exactly which crash-recovery
        # gap this closes. Optional (defaults to None, falling back to the
        # old two-step) so existing callers/tests that construct a
        # WatchEvaluator directly against bare watch_repository/
        # alert_repository pairs (with no atomic writer available, e.g. a
        # hand-rolled test double) keep working unchanged; real wiring
        # (app/main.py, lambda_handlers/watch_evaluator_stream.py) always
        # passes one via repositories/factory.py's build_transition_writer.
        self._transition_writer = transition_writer

    async def create_watch(
        self,
        race_state: RaceState,
        candidate: str,
        target: str,
        watch_type: WatchType = WatchType.UNDERCUT,
    ) -> Watch:
        """create_strategy_watch (spec 22): establishes a new watch and its
        initial status from the CURRENT race state. This is a baseline
        establishment, not a transition, so no alert is recorded here —
        alerts only represent a change relative to a previously
        established status (spec 18 step 3: "compares new status with
        previous status")."""
        if watch_type != WatchType.UNDERCUT:
            raise StrategyValidationError(f"Unsupported watch_type: {watch_type}")

        thresholds = self._engine.watch_thresholds()
        result = self._engine.calculate_undercut(race_state, candidate, target)
        initial_status = (
            WatchStatus.OPEN
            if result.confidence.probability_ahead >= thresholds.open_probability
            else WatchStatus.CLOSED
        )

        watch = Watch(
            watch_id=f"watch-{uuid.uuid4()}",
            race_id=race_state.race_id,
            run_id=race_state.run_id,
            type=watch_type,
            candidate=candidate,
            target=target,
            open_threshold=thresholds.open_probability,
            close_threshold=thresholds.close_probability,
            status=initial_status,
            last_probability_ahead=result.confidence.probability_ahead,
            last_evaluated_state_version=race_state.state_version,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        return await self._watches.create_watch(watch)

    async def evaluate(self, race_state: RaceState) -> tuple[Alert, ...]:
        """Watch Evaluator (spec 18): invoked on every accepted race-state
        change. Returns only the alerts newly published by THIS call — a
        duplicate/replayed event that was already processed returns an
        empty tuple, never a second alert for the same transition (spec 49).
        """
        # Spec v1.1 Section 29 "Race data stale" / repair 14: "Suppress new
        # proactive strategy alerts" when the snapshot being evaluated is
        # already stale (current_time - generated_at > 5s). This is a
        # pure no-op, exactly like the run_id/state_version guard above it
        # (spec 49/repair 6) — no calculation, no alert creation, no watch
        # mutation — so a stale evaluation can never itself manufacture a
        # transition the engineer would then see fire late and out of
        # context.
        if is_state_stale(race_state.generated_at):
            return ()

        active_watches = await self._watches.list_active_watches(race_state.race_id, race_state.run_id)
        published: list[Alert] = []

        for watch in active_watches:
            # Repair 6 guard (spec): "Before evaluating a watch, require
            # race_state.run_id == watch.run_id AND race_state.state_version
            # > watch.last_evaluated_state_version. If either check fails,
            # return without calculation, alert creation or watch update."
            # `list_active_watches` already filters by run_id, so that half
            # is normally already satisfied by the time a watch reaches
            # this loop — the check is still made explicit and re-verified
            # here so this method stays correct even if a future caller
            # passes it a watch list from somewhere else. The state_version
            # half is the one that actually needs enforcing here: a
            # duplicate delivery of the SAME state_version, or an
            # out-of-order delivery of an OLDER one arriving after a newer
            # one was already evaluated, must be a pure no-op — no
            # recalculation, no possible (even if idempotent) alert write,
            # no watch mutation. This is what makes at-least-once event
            # delivery (a real DynamoDB Streams Lambda can be invoked more
            # than once for the same change, or invocations can be
            # reordered) safe: replaying or reordering evaluate() calls
            # never changes the outcome.
            if race_state.run_id != watch.run_id or race_state.state_version <= watch.last_evaluated_state_version:
                continue

            # Step 1 (loaded above) + step 2: invoke the deterministic model.
            try:
                result = self._engine.calculate_undercut(race_state, watch.candidate, watch.target)
            except StrategyValidationError:
                # A watch can become temporarily unevaluable (e.g. the
                # candidate has overtaken the target). Skip it this tick
                # rather than raising — the demo must keep running.
                continue

            new_status = self._apply_hysteresis(watch, result.confidence.probability_ahead)

            updated_watch = watch.model_copy(
                update={
                    "status": new_status,
                    "last_probability_ahead": result.confidence.probability_ahead,
                    "last_evaluated_state_version": race_state.state_version,
                    "updated_at": utc_now(),
                }
            )

            # Step 3: compare new status with previous status.
            if new_status != watch.status:
                idempotency_key = f"{race_state.run_id}:{watch.watch_id}:{race_state.state_version}:{new_status.value}"
                alert = Alert(
                    alert_id=f"alert-{uuid.uuid4()}",
                    evaluation_id=f"eval-{uuid.uuid4()}",
                    watch_id=watch.watch_id,
                    race_id=race_state.race_id,
                    run_id=race_state.run_id,
                    state_version=race_state.state_version,
                    previous_status=watch.status,
                    new_status=new_status,
                    triggering_snapshot=race_state,
                    deterministic_result=result,
                    assumptions=result.assumptions,
                    model_version=result.model_version,
                    idempotency_key=idempotency_key,
                    created_at=utc_now(),
                )
                if self._transition_writer is not None:
                    # Spec v1.1 correction 9: commit the alert AND the
                    # watch update together, atomically — see
                    # TransitionWriter's class docstring (watch_base.py)
                    # for exactly which crash-recovery gap this closes
                    # relative to the old two-step sequence below.
                    stored_alert, created = await self._transition_writer.write_transition(
                        idempotency_key,
                        alert,
                        updated_watch,
                        require_last_evaluated_state_version_below=race_state.state_version,
                    )
                    # Step 4: publish only state transitions — and only the
                    # ones THIS call actually created (never a replay, and
                    # never a call whose watch half lost a conflicting
                    # race — write_transition rolled the whole thing back).
                    if created:
                        published.append(stored_alert)
                        # Step 6: "pushes alert to connected Fire TV
                        # clients." Gate 2 exposes this via polling (GET
                        # .../alerts), consistent with Gate 0's polling
                        # architecture — see the README's Gate 2 scope
                        # note on why WebSocket push is deferred rather
                        # than added here.
                    # The atomic write already applied (or correctly
                    # skipped) the watch update — nothing left to do for
                    # this watch this tick.
                    continue

                # Fallback path (no transition_writer configured): the
                # original two-step sequence. Still correct for a single
                # in-memory process (see TransitionWriter's class
                # docstring for why the crash window this leaves open is
                # DynamoDB-specific), and kept so a caller/test that
                # constructs a bare WatchEvaluator without wiring an
                # atomic writer keeps working exactly as before.
                # Step 5 (persist) via the idempotent conditional write.
                stored_alert, created = await self._alerts.record_transition(idempotency_key, alert)
                # Step 4: publish only state transitions — and only the
                # ones THIS call actually created (never a replay).
                if created:
                    published.append(stored_alert)
                    # Step 6: "pushes alert to connected Fire TV clients."
                    # Gate 2 exposes this via polling (GET .../alerts),
                    # consistent with Gate 0's polling architecture — see
                    # the README's Gate 2 scope note on why WebSocket push
                    # is deferred rather than added here.

            # Second line of defense beyond the in-process guard above: a
            # concurrent invocation of this same method (e.g. two DynamoDB
            # Streams Lambda invocations processing overlapping state
            # changes for the same watch) could both pass the in-memory
            # check above before either has written back, since that check
            # reads a `watch` object fetched at the START of this call, not
            # at write time. `require_last_evaluated_state_version_below`
            # makes the actual write conditional on what's stored AT WRITE
            # TIME, so the loser of that race safely no-ops instead of
            # clobbering the winner's (newer) watch state. If our own alert
            # was already recorded above, it stays recorded — alert
            # recording is independently idempotent by idempotency_key, so
            # losing this race costs nothing but a redundant watch write.
            # (Only reached for a NON-transitioning evaluation now — a
            # transitioning one either `continue`d above via the atomic
            # writer, or falls through from the two-step fallback path,
            # which still needs this same write.)
            try:
                await self._watches.update_watch(
                    updated_watch,
                    require_last_evaluated_state_version_below=race_state.state_version,
                )
            except WatchUpdateConflict:
                continue

        return tuple(published)

    @staticmethod
    def _apply_hysteresis(watch: Watch, probability_ahead: float) -> WatchStatus:
        """OPEN >= open_threshold, CLOSE <= close_threshold; the band
        between the two thresholds holds the current status unchanged.
        This is what "prevents repeated OPEN/CLOSED oscillation" (spec 18)."""
        if watch.status == WatchStatus.CLOSED and probability_ahead >= watch.open_threshold:
            return WatchStatus.OPEN
        if watch.status == WatchStatus.OPEN and probability_ahead <= watch.close_threshold:
            return WatchStatus.CLOSED
        return watch.status
