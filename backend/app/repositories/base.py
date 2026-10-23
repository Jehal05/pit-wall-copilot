"""Persistence boundary so domain/service code never depends on DynamoDB
(or any other storage technology) directly.

Active-run protection (spec v1.1 mandatory correction 1): each race has at
most one ACTIVE run_id at a time. Once a run is active, only
`begin_new_run` can change which run_id is active — `save_state` and
`save_control` (the ordinary, per-tick writes the simulator loop makes)
can NEVER switch a race to a different run_id, no matter what state_version
they carry. This is what makes reset() safe against a delayed write from
the run that was just superseded: that write's run_id no longer matches
the active run, so it is rejected regardless of its state_version — the
implementation must not fall back to comparing state_versions or UUIDs
across two different run_ids to decide which "wins".

The one exception is bootstrap: a race that has never had ANY run
established yet (a brand-new table/process) has no active run to compare
against, so the very first `save_state`/`save_control` call implicitly
establishes its run_id as active. This is deliberately narrow — it only
ever applies once, before any run has been established — and is what spec
v1.1's correction 1 means by "No active state exists" as the one
non-`begin_new_run` way a write may proceed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.domain.models import DemoControlState, RaceState, SimulatorStatus
from app.repositories.lease import LeaseProof


class StaleWriteError(Exception):
    """Raised when a write's state_version is not newer than the stored one
    within the same run, when the write's run_id is not the race's
    currently active run (whether that's an older, superseded run, or any
    run_id other than the active one that did not arrive through
    `begin_new_run`), or when a supplied `LeaseProof` no longer matches
    the live lease record — including (P0-2 repair) a proof whose own
    `race_id`/`lease_name` do not match the write's actual target race,
    which must be rejected before any live-lease record is even consulted
    (see `app/repositories/lease.py`'s module docstring for the
    stale-leader window this closes, and this module's own P0-2 note
    below for the cross-race exploit it also closes)."""


class InvalidControlTransitionError(StaleWriteError):
    """Raised by `transition_control_from_operator` when the CONTROL
    item's current stored `status` is not a member of the caller's
    `allowed_current_statuses` — e.g. an operator `/pause` racing a tick
    that just moved the demo to COMPLETED. A subclass of `StaleWriteError`
    (not a sibling exception) so any existing caller that only catches
    `StaleWriteError` still sees it, while `SimulatorService` can also
    catch this specific type when it wants to react differently (see
    `SimulatorService.start`/`pause`, which treat it as "someone else's
    write already resolved this — re-read and return the current state"
    rather than a hard failure)."""


class RaceStateRepository(ABC):
    """Persistence boundary for canonical race state and control state."""

    @abstractmethod
    async def get_state(self, race_id: str) -> Optional[RaceState]:
        """Return the latest known state for a race, or None if unknown."""

    @abstractmethod
    async def save_state_from_simulator(
        self, state: RaceState, *, lease_proof: Optional[LeaseProof] = None
    ) -> RaceState:
        """Persist a new state for the race's CURRENTLY ACTIVE run only.
        This is the leader-owned, per-tick write — called ONLY by the
        simulator tick loop (`SimulatorService._advance`), never by an
        operator command. (Renamed from `save_state` in the P0-1 repair
        pass specifically to make that boundary unmistakable — see
        `transition_control_from_operator` below for the operator-command
        counterpart, which never requires or accepts a `lease_proof`.)

        Must raise StaleWriteError when:
          - a run is already active for this race and state.run_id does not
            match it (an old/superseded run, or any run_id that did not
            arrive via begin_new_run — see this module's docstring); or
          - state.run_id matches the active run but state.state_version is
            not strictly greater than the currently stored version; or
          - `lease_proof` is provided and its own `race_id` does not equal
            this write's actual target `state.race_id`, or its
            `lease_name` does not equal
            `simulator_lease_name(state.race_id)` (P0-2 repair: these two
            checks are unconditional and evaluated in Python BEFORE any
            persistence round-trip — a proof legitimately acquired for a
            DIFFERENT race must never be accepted here just because its
            own embedded `race_id` happens to be self-consistent); or
          - `lease_proof` is provided and, evaluated atomically with the
            rest of this write (one DynamoDB transaction; one nested
            critical section for the in-memory implementation — see
            `app/repositories/lease.py`'s module docstring), does NOT
            exactly match the live lease record: the lease must exist,
            its `race_id` must match, its `owner_id` must equal
            `lease_proof.owner_id`, its `fencing_token` must EXACTLY equal
            `lease_proof.fencing_token` (not merely be less-than-or-equal
            — see lease.py for why an inequality check leaves a
            stale-leader window open), and it must not be expired as of
            the moment this write is evaluated. `lease_proof=None` (the
            default) skips this check entirely — single-process/local-dev
            callers that never wired a `LeaseRepository` never carry a
            proof and are unaffected. A concrete implementation MAY be
            constructed to require a non-None `lease_proof` on every call
            (fail closed) when it represents a genuinely multi-instance
            deployment target — see `DynamoDBRaceStateRepository`'s own
            `require_lease_proof` constructor flag.

        Must succeed, and implicitly adopt state.run_id as the active run,
        only when no run has EVER been established for this race yet
        (bootstrap — see module docstring). A write that arrives after
        begin_new_run has already established a run, but before that run's
        own first save_state/save_control call, must still be evaluated
        against that established active run_id, not treated as a second
        bootstrap.
        """

    @abstractmethod
    async def get_control(self, race_id: str) -> Optional[DemoControlState]:
        """Return the current simulator control/lifecycle state."""

    @abstractmethod
    async def save_control_from_simulator(
        self, control: DemoControlState, *, lease_proof: Optional[LeaseProof] = None
    ) -> DemoControlState:
        """Persist simulator control/lifecycle state for the race's
        CURRENTLY ACTIVE run only — the same active-run guard
        save_state_from_simulator enforces (spec v1.1 correction 1:
        "Protect the CONTROL record from old-run writes as well"). This is
        the leader-owned write — called ONLY by the simulator tick loop
        (`SimulatorService._advance`, for the COMPLETED transition and the
        per-tick control update), never by an operator command. (Renamed
        from `save_control` in the P0-1 repair pass — see
        `save_state_from_simulator`'s docstring above for why.)

        Unlike save_state_from_simulator, control writes are not required
        to strictly increase state_version on every call: the simulator
        loop legitimately writes a new control record that carries the
        SAME state_version as the most recent race-state tick (only
        `status` changed). So the version check here is non-regression,
        not strict-increase: reject only when control.state_version is
        LESS than the stored control's state_version for the same run_id.

        Must NOT modify `control_revision` at all — that field is owned
        exclusively by `transition_control_from_operator`'s atomic
        increment; this method must persist whatever `control_revision`
        value is already on the `control` object it is given (which, for
        the simulator loop's own call sites, is always carried forward
        unchanged from a prior `get_control()` read via `model_copy`).

        Production-fix pass — closes the "valid leader overwrites an
        operator pause" race: this method must ADDITIONALLY require that
        the CURRENTLY STORED `control_revision` still equals the
        `control_revision` carried on the `control` object being written
        (the revision the caller captured when it read the control record
        it is now building this write from), verified atomically with the
        rest of this write (the same DynamoDB transaction; the same
        in-memory lock's critical section). A mismatch means an operator
        command (`transition_control_from_operator`) has committed since
        this write's caller last read the control record — e.g. the
        simulator read RUNNING, an operator then paused (incrementing
        `control_revision`), and the simulator's in-flight write, built
        from its now-stale pre-pause read, must be rejected even though
        its lease proof is still perfectly valid (this is not a
        stale-LEADER problem — the leader is still legitimate — it is a
        stale-READ problem, which is exactly what `control_revision`
        fencing exists to close). Reject with `StaleWriteError` on a
        mismatch, exactly like every other fencing rejection this method
        already makes. Never reset or regress `control_revision`.

        `lease_proof` has the same live-lease-verification meaning as in
        `save_state_from_simulator` — state and control writes apply
        IDENTICAL fencing rules, checked against the same lease record,
        including the P0-2 cross-race `race_id`/`lease_name` checks.
        """

    @abstractmethod
    async def transition_control_from_operator(
        self,
        race_id: str,
        run_id: str,
        *,
        allowed_current_statuses: frozenset[SimulatorStatus],
        new_status: SimulatorStatus,
        current_lap: Optional[int] = None,
    ) -> DemoControlState:
        """Persist an OPERATOR-issued control transition (`/start`,
        `/pause`, or any future operator command) — added in the P0-1
        repair pass specifically so operator commands and leader-owned
        writes can never again be conflated behind one ambiguous method.

        Unlike `save_control_from_simulator`, this method:

          1. NEVER requires or accepts a `lease_proof` — that is the whole
             point. An operator command (a human or client hitting
             `/start`/`/pause`) must succeed regardless of which backend
             task, if any, currently holds the simulator leadership lease
             for this race. Simulator leadership governs who may ADVANCE
             the race each tick, not who may issue an operator command.
          2. Still enforces the same active-run-id guard every other
             write in this ABC enforces (via the same GENERATION-item
             mechanism): a call whose `run_id` is not the race's current
             active run must raise `StaleWriteError`.
          3. Enforces a valid status transition atomically: the write must
             be conditioned on the CONTROL item's CURRENT STORED `status`
             (a first-class attribute, not the `data` JSON blob) being a
             member of `allowed_current_statuses`. When it is not, raise
             `InvalidControlTransitionError` (a `StaleWriteError`
             subclass) rather than silently doing nothing or raising a
             generic error — see that exception's own docstring for how
             `SimulatorService` reacts to it.
          4. Uses its own atomic optimistic-concurrency counter,
             `control_revision`, entirely separate from `state_version`:
             every successful call here increments it by exactly 1 via a
             genuine compare-and-set — read the currently stored
             `control_revision` (a STRONGLY CONSISTENT read — see
             production-fix pass note below), compute
             `next_revision = expected_revision + 1` exactly once, and
             condition the write on the stored revision still equalling
             `expected_revision`. A losing concurrent call must retry
             against a freshly re-read revision rather than error out or
             return HTTP 500 — see point 6. `save_control_from_simulator`
             must never touch this field.

             Production-fix pass: an earlier version of this method
             computed `next_revision` via DynamoDB's atomic
             `if_not_exists(control_revision, 0) + 1` SET-clause
             expression for the TOP-LEVEL attribute only, while
             separately serializing a `data` JSON blob built from the
             CALLER'S OWN pre-write read of `control_revision` (i.e. the
             OLD value) — the two were never guaranteed to agree, and in
             practice always disagreed by exactly one
             (`get_control()`, which deserializes from `data`, would
             report the stale pre-increment revision even though the
             top-level attribute had already advanced). The fix: this
             method must compute `next_revision` explicitly in Python
             from a value it just read, write that SAME literal value to
             the top-level attribute, inside the serialized `data` blob,
             and as the `control_revision` on the `DemoControlState` it
             returns — one value, written to every representation in the
             same atomic write, never derived independently in two
             places.
          5. Updates `status` (and `current_lap`, when provided) as
             first-class stored attributes, kept in sync with the `data`
             JSON blob `get_control()` deserializes from.
          6. Retries its own compare-and-set on losing a race against a
             concurrent operator call (the CONTROL item's condition
             failed, but the GENERATION check still passed) — re-reading
             the current control record (strongly consistent), re-checking
             `allowed_current_statuses`, and re-attempting with a freshly
             computed `expected_revision`/`next_revision` pair — rather
             than raising or returning an error to the caller. This is
             what makes two genuinely concurrent `/start` (or `/pause`)
             calls both complete successfully (point: "idempotent" does
             not mean "skips writing" here — each call that legitimately
             reaches this method still performs its own real,
             revision-incrementing write; it is safe and expected for
             `control_revision` to advance by more than one across two
             concurrent operator calls that both succeed. What must never
             happen is two calls committing the SAME `next_revision`, or
             `control_revision` ever regressing.)

        `new_status` must be applied even when it equals the CURRENT
        stored status (i.e. `new_status` being a member of
        `allowed_current_statuses` is expected and required for
        idempotent operator retries — see `SimulatorService.start`, which
        includes RUNNING in its own allowed set specifically so two
        concurrent `/start` calls both succeed without error).

        `current_lap`, when None, must leave the stored `current_lap`
        unchanged.
        """

    @abstractmethod
    async def begin_new_run(self, race_id: str, state: RaceState, control: DemoControlState) -> None:
        """The ONLY way a race's active run_id may change (spec v1.1
        correction 1: "Switching from one run to another must happen only
        through an explicit repository reset/new-run operation"). Called
        once by SimulatorService.reset() (and by the very first
        initialization, so bootstrap and reset share one atomic path
        rather than reset relying on a delete-then-let-the-next-writer-
        claim-it sequence, which this method exists specifically to avoid).

        `state` and `control` must both carry the SAME new run_id, which
        must differ from the race's current active run_id (if any).

        Must be atomic, or safely equivalent to atomic under concurrency:
        once this call returns, no write bearing any run_id other than
        state.run_id can ever succeed again via save_state/save_control for
        this race — including a write that was already in flight (e.g.
        already past this repository's own admission check) when this was
        called racing to complete afterward. Implementations satisfy this
        by making the active-run switch and the new baseline's first
        write indivisible from any other writer's point of view (an
        `asyncio.Lock`-guarded critical section for the in-memory
        implementation; a single `TransactWriteItems` call for the
        DynamoDB implementation — see dynamodb.py's own module docstring).

        Unlike the pre-repair-pass design, this no longer needs to reset
        any fencing-related bookkeeping of its own: `lease_proof`
        verification (repair pass) checks a write's proof against the
        LIVE lease record directly, not against any per-race floor this
        method used to maintain — a reset changes which run_id is active
        (checked independently, as above) but does not by itself
        invalidate or require touching the simulator lease, which is
        keyed by race_id, not by run_id.
        """
