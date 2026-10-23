"""Deterministic race simulator.

Advances the canonical race state lap-by-lap using only the authored
scenario file. No randomness, no LLM involvement, no per-car business
logic — the simulator is purely a data-driven player of scenario laps.

Concurrency / safety guarantees required by the spec:
  * Only one advancing loop may exist per race at a time (idempotent start).
  * A reset generates a new run_id and safely cancels/orphans the previous
    loop so it can never write into the new run (an in-flight old loop
    checks its captured run_id against the *current* active run before
    every write and stops silently if it no longer matches).
  * state_version increases by exactly one on every accepted state update.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.domain.models import (
    DemoControlState,
    DriverState,
    RaceState,
    SimulatorStatus,
    utc_now,
)
from app.domain.scenario import Scenario, ScenarioLap
from app.metrics import emit_simulator_lease_ownership_changed, emit_stale_leader_write_rejected
from app.repositories.base import InvalidControlTransitionError, RaceStateRepository, StaleWriteError
from app.repositories.lease import LeaseProof, LeaseRepository, simulator_lease_name

SleepFn = Callable[[float], Awaitable[None]]


class UnknownRaceError(Exception):
    """Raised when an operation targets a race_id the simulator does not manage."""


def _gap_to_ahead(scenario: Scenario, lap: ScenarioLap, driver_id: str) -> int:
    """Derive gap-to-car-ahead for the four modeled cars.

    Gate 0 only models four of the field's cars, so "car ahead" is not
    always one of them. CAR_04's gap to CAR_03 is the scenario's canonical
    narrative value and is used directly. For the remaining cars we use the
    difference in gap-to-leader between adjacent modeled positions as a
    documented demo simplification (these are synthetic values regardless).
    """
    if driver_id == "CAR_04" and "CAR_03" in lap.drivers:
        return lap.gap_car04_to_car03_ms

    ordered = sorted(lap.drivers.items(), key=lambda item: item[1].position)
    ids_in_order = [car_id for car_id, _ in ordered]
    idx = ids_in_order.index(driver_id)
    if idx == 0:
        return lap.drivers[driver_id].gap_to_leader_ms
    prev_id = ids_in_order[idx - 1]
    return max(0, lap.drivers[driver_id].gap_to_leader_ms - lap.drivers[prev_id].gap_to_leader_ms)


def _build_race_state(
    scenario: Scenario,
    scenario_lap: ScenarioLap,
    race_id: str,
    run_id: str,
    state_version: int,
    generated_at: datetime,
) -> RaceState:
    drivers: list[DriverState] = []
    for driver_id, car_config in scenario.cars.items():
        lap_data = scenario_lap.drivers[driver_id]
        drivers.append(
            DriverState(
                driver_id=driver_id,
                display_name=car_config.display_name,
                team_name=car_config.team_name,
                position=lap_data.position,
                gap_to_leader_ms=lap_data.gap_to_leader_ms,
                gap_to_ahead_ms=_gap_to_ahead(scenario, scenario_lap, driver_id),
                compound=car_config.compound,
                tire_age_laps=lap_data.tire_age_laps,
                base_pace_ms=car_config.base_pace_ms,
                current_lap_pace_ms=lap_data.current_lap_pace_ms,
                pit_stops=car_config.pit_stops,
                pit_window=car_config.pit_window,
                traffic_factor=lap_data.traffic_factor,
            )
        )
    drivers.sort(key=lambda d: d.position)

    return RaceState(
        race_id=race_id,
        run_id=run_id,
        state_version=state_version,
        lap=scenario_lap.lap,
        total_laps=scenario.total_laps,
        race_time_ms=scenario_lap.race_time_ms,
        track_state=scenario.track_state,
        generated_at=generated_at,
        scenario_id=scenario.scenario_id,
        synthetic=True,
        drivers=tuple(drivers),
    )


StateCommittedHook = Callable[[RaceState], Awaitable[None]]


class SimulatorService:
    def __init__(
        self,
        repository: RaceStateRepository,
        scenario: Scenario,
        sleep_fn: Optional[SleepFn] = None,
        tick_interval_seconds: Optional[float] = None,
        on_state_committed: Optional[StateCommittedHook] = None,
        lease_repository: Optional[LeaseRepository] = None,
        lease_ttl_seconds: float = 10.0,
    ) -> None:
        self._repository = repository
        self._scenario = scenario
        self._sleep_fn = sleep_fn or asyncio.sleep
        self._tick_interval_seconds = (
            tick_interval_seconds if tick_interval_seconds is not None else scenario.update_interval_seconds
        )
        self._active_run_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        # AWS hackathon-final pass, P0-4: distributed leadership. Every
        # SimulatorService instance (one per backend PROCESS -- see
        # module docstring) gets its own random instance_id, used as the
        # lease "owner_id". `lease_repository` is None by default (single-
        # process local dev, and every pre-existing test that constructs
        # this class directly) -- in that mode `_run_loop` ticks exactly
        # as it always has, with no lease check at all, so nothing about
        # this pass changes behavior unless a lease repository is
        # explicitly wired in (app/main.py's lifespan does this whenever
        # STATE_REPOSITORY=dynamodb, mirroring how pairing/watch storage
        # already key off the same setting).
        self._instance_id = str(uuid.uuid4())
        self._lease_repository = lease_repository
        self._lease_ttl_seconds = lease_ttl_seconds
        # Phase 5 observability: tracks whether THIS instance held the
        # simulator lease as of the last tick, purely so
        # `emit_simulator_lease_ownership_changed` fires only on a real
        # takeover, not on every routine renewal — see `_run_loop`'s own
        # comment at the call site.
        self._was_lease_holder = False
        # Spec v1.1 pass 3, correction 9: serializes every call that may
        # establish a new active run (`_begin_new_run`, via either
        # `ensure_initialized` or `reset`). Without this, two concurrent
        # `reset()` calls can interleave: each sets `self._active_run_id`
        # synchronously (in call order) before awaiting
        # `repository.begin_new_run` (which can complete in EITHER order),
        # so the in-process `self._active_run_id` this service uses to
        # decide which run its own tick loop/cancellation logic considers
        # "current" can end up disagreeing with whichever run the
        # repository's own atomic `begin_new_run` call actually committed
        # last. Holding this lock for the FULL duration of
        # `_begin_new_run` (including its `await` on the repository) means
        # the two operations can never interleave — the second caller to
        # acquire the lock always establishes the final run, in BOTH
        # places, deterministically. This is a dedicated reset lock, not a
        # UUID-ordering scheme (the module docstring/spec are explicit
        # that UUIDs must never be compared to decide a "winner").
        self._reset_lock = asyncio.Lock()
        # Gate 2: invoked after every accepted state write, standing in for
        # a DynamoDB-Streams-triggered Watch Evaluator Lambda in a real AWS
        # deployment (see services/watch_evaluator.py's module docstring).
        self._on_state_committed = on_state_committed

    @property
    def race_id(self) -> str:
        return self._scenario.race_id

    def _check_race(self, race_id: str) -> None:
        if race_id != self._scenario.race_id:
            raise UnknownRaceError(f"Unknown race_id: {race_id}")

    async def get_state(self, race_id: str) -> Optional[RaceState]:
        self._check_race(race_id)
        return await self._repository.get_state(race_id)

    async def get_control(self, race_id: str) -> Optional[DemoControlState]:
        self._check_race(race_id)
        return await self._repository.get_control(race_id)

    async def ensure_initialized(self) -> RaceState:
        """Create the initial lap-24, state_version-0 state if none exists yet."""
        existing = await self._repository.get_state(self._scenario.race_id)
        if existing is not None:
            self._active_run_id = existing.run_id
            return existing
        return await self._begin_new_run()

    async def _begin_new_run(self) -> RaceState:
        """Establishes a brand-new run's lap-24, state_version-0 baseline
        via `repository.begin_new_run` — the one atomic operation that may
        change which run_id is active (spec v1.1 correction 1; see
        repositories/base.py's own docstring). Used both for the very
        first-ever initialization of a race (via `ensure_initialized`) and
        for every subsequent `reset()` — unifying those two paths onto the
        same atomic call is itself part of the fix: the pre-correction
        design instead called `repository.reset()` (delete) followed by
        separate `save_state_from_simulator`/`save_control_from_simulator` calls, leaving a window where
        a delayed write from the JUST-SUPERSEDED run could resurrect it
        before the new baseline landed.

        Serialized by `self._reset_lock` (held for this entire method,
        including the repository await) — see that lock's own doc-comment
        in `__init__` for why two concurrent callers must never interleave
        here."""
        async with self._reset_lock:
            run_id = str(uuid.uuid4())
            first_lap = self._scenario.laps[0]
            state = _build_race_state(
                self._scenario,
                first_lap,
                race_id=self._scenario.race_id,
                run_id=run_id,
                state_version=0,
                generated_at=utc_now(),
            )
            control = DemoControlState(
                race_id=self._scenario.race_id,
                run_id=run_id,
                status=SimulatorStatus.STOPPED,
                scenario_id=self._scenario.scenario_id,
                current_lap=state.lap,
                state_version=state.state_version,
                updated_at=utc_now(),
            )
            await self._repository.begin_new_run(self._scenario.race_id, state, control)
            # Set only AFTER the repository call has actually committed,
            # and still inside the lock — so `self._active_run_id` always
            # reflects the run that most recently WON the repository race,
            # never merely the run that was most recently ATTEMPTED.
            self._active_run_id = run_id
        await self._notify_state_committed(state)
        return state

    async def reset(self, race_id: str) -> RaceState:
        """Reset SHALL invalidate the previous run and restore lap 24 (spec 48)."""
        self._check_race(race_id)
        await self._cancel_active_loop()
        return await self._begin_new_run()

    async def start(self, race_id: str) -> DemoControlState:
        """Idempotent start: calling it while RUNNING must not spawn another
        loop. Deliberately calls `transition_control_from_operator` — NOT
        `save_control_from_simulator` — so this operator command works
        even when THIS backend task/instance does not hold (and never
        acquires) the simulator leadership lease for this race (P0-1
        repair pass): no `lease_proof` is ever needed for an operator
        command, only for the leader-owned tick loop's own writes.
        """
        self._check_race(race_id)
        async with self._start_lock:
            control = await self._repository.get_control(race_id)
            if control is None:
                await self.ensure_initialized()
                control = await self._repository.get_control(race_id)

            if control.status == SimulatorStatus.RUNNING and self._task is not None and not self._task.done():
                return control  # already running locally — idempotent no-op, no repository write needed

            if control.status == SimulatorStatus.COMPLETED:
                return control  # demo finished; caller must reset to run again

            # RUNNING is deliberately included in `allowed_current_statuses`
            # so that a concurrent/repeated `/start` — including one that
            # lands on a DIFFERENT SimulatorService instance than the one
            # that already has a live local loop — is a legal, idempotent
            # transition rather than an error.
            try:
                new_control = await self._repository.transition_control_from_operator(
                    race_id,
                    control.run_id,
                    allowed_current_statuses=frozenset(
                        {SimulatorStatus.STOPPED, SimulatorStatus.PAUSED, SimulatorStatus.RUNNING}
                    ),
                    new_status=SimulatorStatus.RUNNING,
                    current_lap=control.current_lap,
                )
            except InvalidControlTransitionError:
                # Rare race: control moved to a status outside the allowed
                # set (e.g. COMPLETED) between our read above and this
                # call, most plausibly the tick loop finishing the demo at
                # the exact moment an operator called /start. Treat this
                # the same way the COMPLETED early-return above does:
                # report the current, already-resolved state rather than
                # raising a 500 for what is really "someone else's write
                # already decided this."
                refreshed = await self._repository.get_control(race_id)
                return refreshed if refreshed is not None else control

            # Ensure this service instance recognizes the stored run as the
            # active one, even if it did not itself create it (e.g. it was
            # constructed after an earlier instance already initialized the
            # race in the shared repository).
            self._active_run_id = control.run_id

            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run_loop(race_id, control.run_id))
            return new_control

    async def ensure_loop_running_if_active(self, race_id: str) -> None:
        """AWS hackathon-final pass, phase 2: the distributed simulator
        supervisor's own tick — called periodically (see `run_supervisor`
        below) by EVERY backend process, independently of whether that
        process ever received an HTTP `/start` call.

        Closes the gap the pre-phase-2 design had: `start()` above is the
        ONLY thing that ever spawned `_run_loop`, so if the one task whose
        loop was leading crashed, no OTHER task would ever spin up a loop
        of its own — there would be no new `/start` call to trigger it,
        and the crashed task's lease would simply expire with nobody left
        contending for it. This method makes "there is a local loop
        whenever shared control state says RUNNING" a standing invariant
        of every process, not a one-time consequence of whichever process
        happened to handle the `/start` request. It is intentionally NOT
        the thing that decides who gets to actually advance the race each
        tick — that remains the lease bid inside `_run_loop` itself, which
        this newly-spawned loop goes through exactly like any other.

        Idempotent and safe to call on a fixed interval indefinitely: a
        no-op whenever a live loop already exists locally, whenever no run
        has been initialized yet, or whenever control state is not
        RUNNING (PAUSED/STOPPED/COMPLETED all correctly leave no loop
        running).
        """
        self._check_race(race_id)
        async with self._start_lock:
            control = await self._repository.get_control(race_id)
            if control is None or control.status != SimulatorStatus.RUNNING:
                return
            if self._task is not None and not self._task.done():
                return  # a local loop is already live — nothing to do
            self._active_run_id = control.run_id
            self._task = asyncio.create_task(self._run_loop(race_id, control.run_id))

    async def run_supervisor(self, race_id: str, poll_interval_seconds: float = 2.0) -> None:
        """Long-running background coroutine, one per backend process (see
        app/main.py's lifespan) — the actual "every process runs a
        lightweight supervisor" half of phase 2. Cancellation-safe: the
        caller cancels this task on shutdown exactly like it already
        cancels `self._task` (the simulator loop itself).

        This loop keeps running through any transient repository error
        (a DynamoDB throttle, a brief network blip) rather than letting
        one failed poll take the supervisor down for the rest of the
        process's lifetime — the next poll_interval_seconds simply tries
        again, which is strictly safer than a supervisor that silently
        stops supervising after its first hiccup.
        """
        try:
            while True:
                try:
                    await self.ensure_loop_running_if_active(race_id)
                except UnknownRaceError:
                    raise
                except Exception:  # noqa: BLE001 # nosec B110
                    pass
                await self._sleep_fn(poll_interval_seconds)
        except asyncio.CancelledError:
            return

    async def pause(self, race_id: str) -> DemoControlState:
        """Deliberately calls `transition_control_from_operator` — NOT
        `save_control_from_simulator` — for the same reason `start` does
        (see its docstring): pause must succeed even when this backend
        task is not, and never becomes, the simulator leader for this
        race."""
        self._check_race(race_id)
        control = await self._repository.get_control(race_id)
        if control is None:
            raise UnknownRaceError(f"Demo for race {race_id} has not been initialized")
        if control.status not in (SimulatorStatus.RUNNING, SimulatorStatus.PAUSED):
            return control  # STOPPED/COMPLETED: nothing to pause, unchanged (unchanged from before)
        try:
            new_control = await self._repository.transition_control_from_operator(
                race_id,
                control.run_id,
                # PAUSED is included so a concurrent/repeated /pause is a
                # legal, idempotent no-op transition, exactly mirroring
                # RUNNING's inclusion in start()'s own allowed set.
                allowed_current_statuses=frozenset({SimulatorStatus.RUNNING, SimulatorStatus.PAUSED}),
                new_status=SimulatorStatus.PAUSED,
                current_lap=control.current_lap,
            )
        except InvalidControlTransitionError:
            # Rare race: e.g. the tick loop completed the demo between our
            # read above and this call. Report the current, already-
            # resolved state rather than raising.
            refreshed = await self._repository.get_control(race_id)
            return refreshed if refreshed is not None else control
        return new_control

    async def _cancel_active_loop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(self, race_id: str, run_id: str) -> None:
        try:
            while True:
                await self._sleep_fn(self._tick_interval_seconds)

                # Safety: an old loop from a prior run must never advance the
                # new run's state, even if cancellation raced with a reset.
                if self._active_run_id != run_id:
                    return

                control = await self._repository.get_control(race_id)
                if control is None or control.run_id != run_id:
                    return
                if control.status == SimulatorStatus.PAUSED:
                    continue
                if control.status != SimulatorStatus.RUNNING:
                    return

                # P0-4: only the current lease holder may actually
                # advance this tick. Losing the lease bid (another task
                # already holds it, or holds it and has just renewed) is
                # an ordinary, expected outcome under multi-task
                # deployment -- NOT an error, and NOT a reason to stop
                # this loop: it keeps ticking and keeps re-bidding for
                # the lease every interval, which is exactly how a
                # crashed leader gets automatically replaced (its lease
                # simply expires and the next bidder wins it) without any
                # of the other tasks needing to notice the crash
                # explicitly.
                lease_proof: Optional[LeaseProof] = None
                if self._lease_repository is not None:
                    lease_name = self._lease_name(race_id)
                    grant = await self._lease_repository.try_acquire_or_renew(
                        lease_name, race_id, self._instance_id, self._lease_ttl_seconds
                    )
                    if grant is None:
                        self._was_lease_holder = False
                        continue  # another task currently holds the lease; try again next tick
                    # Repair pass: build a structured LeaseProof directly
                    # from the grant just received, and thread THAT (not
                    # a bare int) to the DynamoDB write boundary (see
                    # repositories/base.py, dynamodb.py, lease.py) —
                    # winning THIS tick's lease bid is not by itself
                    # sufficient proof that this write will be accepted;
                    # the persistence layer independently re-verifies the
                    # proof against the LIVE lease record (exact token
                    # match, not merely "higher than a historical floor"
                    # — see lease.py's module docstring for the
                    # stale-leader window that distinction closes), so a
                    # leader that wins a bid but has actually already
                    # been superseded by a newer leader's own acquisition
                    # still gets rejected even if it hasn't written yet.
                    lease_proof = LeaseProof.from_grant(lease_name, grant)
                    # Phase 5 observability: emit only on a real
                    # TAKEOVER (this instance did not hold the lease last
                    # tick and now does), not on every routine renewal --
                    # every non-None grant here always names THIS
                    # instance as owner (try_acquire_or_renew only ever
                    # grants to the owner_id it was called with), so
                    # "held last tick" vs "holds now" is the only signal
                    # that distinguishes a takeover from a renewal.
                    if not self._was_lease_holder:
                        emit_simulator_lease_ownership_changed(race_id=race_id)
                    self._was_lease_holder = True

                # `_advance` itself catches StaleWriteError (ordinary
                # version contention, and a persistence-level fencing
                # rejection alike) and returns False; either way this
                # loop simply stops advancing under a now-invalid
                # leadership rather than raising. The next tick's lease
                # bid fails closed until/unless this instance wins
                # leadership again with a fresh, current proof.
                advanced = await self._advance(race_id, run_id, lease_proof=lease_proof)
                if not advanced:
                    return
        except asyncio.CancelledError:
            return

    def _lease_name(self, race_id: str) -> str:
        return simulator_lease_name(race_id)

    async def _advance(
        self, race_id: str, run_id: str, *, lease_proof: Optional[LeaseProof] = None
    ) -> bool:
        """Advance exactly one lap. Returns False when the demo is complete.

        `lease_proof` (repair pass): propagated into every save_state_from_simulator/
        save_control_from_simulator call this method makes, so a stale/superseded leader
        (see `_run_loop`) is rejected at the persistence layer — against
        the LIVE lease record, not a historical floor — even if it
        otherwise still looks like a valid writer. `advance_once_for_testing`
        and any other unfenced caller pass None, exactly preserving this
        method's original unfenced behavior.
        """
        current = await self._repository.get_state(race_id)
        if current is None or current.run_id != run_id:
            return False

        next_lap_number = current.lap + 1
        next_scenario_lap = self._scenario.lap_by_number(next_lap_number)

        if next_scenario_lap is None:
            control = await self._repository.get_control(race_id)
            if control is not None and control.run_id == run_id:
                completed = control.model_copy(
                    update={"status": SimulatorStatus.COMPLETED, "updated_at": utc_now()}
                )
                await self._repository.save_control_from_simulator(completed, lease_proof=lease_proof)
            return False

        new_state = _build_race_state(
            self._scenario,
            next_scenario_lap,
            race_id=race_id,
            run_id=run_id,
            state_version=current.state_version + 1,
            generated_at=utc_now(),
        )
        try:
            await self._repository.save_state_from_simulator(new_state, lease_proof=lease_proof)
        except StaleWriteError:
            # Another writer already advanced this run past us (or it was
            # reset), OR this instance's lease proof has been superseded
            # by a newer leader's own acquisition. Either way: stop this
            # loop rather than fight over state. Phase 5 observability: a
            # fenced (lease_proof is not None) rejection is the specific
            # "stale leader" signal phase 5 asks to be alarmable — an
            # unfenced StaleWriteError (lease_proof is None, e.g. no
            # LeaseRepository configured at all) is ordinary
            # single-process contention, not a stale-leader event, so it
            # is deliberately NOT counted here.
            if lease_proof is not None:
                emit_stale_leader_write_rejected(race_id=race_id)
            self._was_lease_holder = False
            return False

        # Production-fix pass: re-read control AFTER the state write has
        # already committed, and — critically — RESPECT whatever
        # lifecycle status is actually stored there rather than
        # unconditionally forcing it to RUNNING. An earlier version of
        # this method always overwrote `status` to RUNNING (or COMPLETED
        # on the final lap) regardless of what this fresh read returned,
        # which meant a still-valid, still-current leader (not a stale
        # one — its lease proof is perfectly good) could stomp an
        # operator's concurrent PAUSED transition straight back to
        # RUNNING: the leader read RUNNING at the top of this tick, an
        # operator paused while the state write was in flight, and this
        # code then wrote RUNNING back because it never looked at the
        # status it just re-read. The fix: only ever advance status
        # RUNNING->RUNNING or RUNNING->COMPLETED (on the final lap); any
        # other currently-stored status (PAUSED, STOPPED, or already
        # COMPLETED) is preserved exactly as-is. Lap/state_version are
        # still brought forward regardless, since the state write itself
        # already committed and the control record's bookkeeping fields
        # should stay in sync with it.
        control = await self._repository.get_control(race_id)
        is_completed = False
        if control is not None and control.run_id == run_id:
            reached_final_lap = next_scenario_lap.lap >= self._scenario.end_lap
            if control.status == SimulatorStatus.RUNNING:
                new_status = SimulatorStatus.COMPLETED if reached_final_lap else SimulatorStatus.RUNNING
            else:
                # PAUSED/STOPPED/COMPLETED: an operator has already moved
                # this race to a terminal-for-this-tick lifecycle state.
                # Never regress it back to RUNNING just because a tick
                # happened to be in flight when that transition landed.
                new_status = control.status
            is_completed = new_status == SimulatorStatus.COMPLETED
            try:
                await self._repository.save_control_from_simulator(
                    control.model_copy(
                        update={
                            "status": new_status,
                            "current_lap": new_state.lap,
                            "state_version": new_state.state_version,
                            "updated_at": utc_now(),
                        }
                    ),
                    lease_proof=lease_proof,
                )
            except StaleWriteError:
                # Production-fix pass: a control_revision mismatch here
                # means an operator command committed between the
                # get_control() read two lines above and this write — an
                # entirely expected, benign outcome of real concurrency
                # (the operator's own transition already reflects the
                # authoritative status; this tick's lap/state_version
                # bookkeeping update simply lost the race and does not
                # need to be retried). This is NOT an unhandled
                # background-task crash: the race STATE write above
                # already succeeded and already committed, so the demo's
                # actual progress is unaffected — only this control
                # record's cosmetic lap/state_version mirroring was
                # skipped for this one tick. Treat exactly like the
                # existing StaleWriteError handling around the state
                # write: log nothing alarming, don't mark this a
                # stale-leader event (this leader is not stale — see
                # save_control_from_simulator's own docstring), and
                # continue the loop normally rather than propagating.
                pass

        # Notify exactly once per accepted state write, regardless of the
        # control-state outcome above (spec 18: "DynamoDB Streams SHALL
        # invoke the Watch Evaluator whenever race state changes").
        await self._notify_state_committed(new_state)
        return not is_completed

    async def _notify_state_committed(self, state: RaceState) -> None:
        if self._on_state_committed is not None:
            await self._on_state_committed(state)

    async def advance_once_for_testing(self, race_id: str) -> RaceState:
        """Test-only helper: advance exactly one lap synchronously without
        waiting for the tick interval. Not used by the API layer."""
        self._check_race(race_id)
        control = await self._repository.get_control(race_id)
        if control is None:
            raise UnknownRaceError("Demo not initialized")
        await self._advance(race_id, control.run_id)
        state = await self._repository.get_state(race_id)
        # Test-only helper (see docstring above), never called from the API
        # layer/production request path; a stripped assert here at worst
        # returns None to a test, not to a real caller.
        assert state is not None  # nosec B101
        return state
