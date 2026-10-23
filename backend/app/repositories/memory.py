"""In-memory repository implementation.

Used for local development and all automated tests so that no AWS
credentials or network access are required. State does not survive process
restart, which is acceptable for Gate 0's local/test scope.

Active-run protection (spec v1.1 correction 1): `self._active_run_ids`
tracks, per race, which run_id is currently allowed to write — this is
tracked SEPARATELY from `self._states`/`self._controls` so that an active
run_id, once established, is never lost even for an instant (unlike the
pre-correction design, which inferred "the active run" purely from
whatever run_id the STATE item currently happened to hold — a design that
has no representation for "a run is active but its baseline hasn't been
written yet" and therefore has no way to close the resurrection window
`begin_new_run` closes). See base.py's module docstring for the full
contract this implements.

Lease-proof fencing (repair pass — see `app/repositories/lease.py`'s
module docstring for the stale-leader window this closes): this
repository optionally takes a reference to the SAME `InMemoryLeaseRepository`
a test/deployment wired into its `SimulatorService`(s), and verifies a
supplied `LeaseProof` by nesting into THAT repository's own lock before
committing a write — see `_verify_lease_proof_locked` below. This is the
in-memory equivalent of a single DynamoDB `TransactWriteItems` spanning
both the LEASE item and the STATE/CONTROL item: no concurrent
`try_acquire_or_renew` call can be interleaved between the check and the
commit, because both operations require the SAME lock. Passing no
`lease_repository` (the default) preserves plain, unfenced local-dev
behavior — the one case the governing instruction asks to keep unfenced:
explicitly-configured single-process/in-memory mode with no lease
repository wired in at all.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.domain.models import DemoControlState, RaceState, SimulatorStatus, utc_now
from app.repositories.base import InvalidControlTransitionError, RaceStateRepository, StaleWriteError
from app.repositories.lease import InMemoryLeaseRepository, LeaseProof, simulator_lease_name


class InMemoryRaceStateRepository(RaceStateRepository):
    def __init__(self, lease_repository: Optional[InMemoryLeaseRepository] = None) -> None:
        self._states: dict[str, RaceState] = {}
        self._controls: dict[str, DemoControlState] = {}
        self._active_run_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # Repair pass: when set, every `lease_proof` this repository is
        # given is verified against THIS repository's live lease record
        # (see module docstring). None (the default) is the plain
        # unfenced local-dev/test-double mode every pre-existing caller
        # that never passes a `lease_repository` continues to get,
        # unchanged.
        self._lease_repository = lease_repository

    async def get_state(self, race_id: str) -> Optional[RaceState]:
        async with self._lock:
            return self._states.get(race_id)

    async def _verify_lease_proof_locked(self, race_id: str, lease_proof: Optional[LeaseProof]) -> None:
        """Must be called while already holding `self._lock`. `race_id` is
        this write's ACTUAL target race — not anything read off
        `lease_proof` itself. Raises StaleWriteError when `lease_proof` is
        supplied and:

          - (P0-2 repair) its own `race_id` does not equal this write's
            actual target `race_id`, or its `lease_name` does not equal
            `simulator_lease_name(race_id)` — checked FIRST, unconditionally,
            before any live lease record is even consulted. This is what
            closes the cross-race exploit: a proof legitimately acquired
            for a different race must never be accepted here merely
            because it is internally self-consistent (its own race_id
            matches its own lease_name) — it must match THIS write's
            target, which the proof alone can never be trusted to assert
            about itself.
          - it does not exactly match the CURRENT lease record — see
            `InMemoryLeaseRepository.matches_locked`'s own docstring for
            why nesting into that repository's lock (rather than an
            independent `get_lease` read) is what makes this atomic. Note
            this check now always compares against the ACTUAL target
            `race_id` (defense-in-depth; guaranteed equal to
            `lease_proof.race_id` by the check above by the time we reach
            here).

        A no-op when `lease_proof` is None (unfenced callers)."""
        if lease_proof is None:
            return
        if self._lease_repository is None:
            raise StaleWriteError(
                f"lease_proof supplied for race {race_id!r} but this repository has no "
                "lease_repository configured to verify it against — construct "
                "InMemoryRaceStateRepository(lease_repository=...) to enable fencing"
            )
        if lease_proof.race_id != race_id:
            raise StaleWriteError(
                f"lease_proof race_id {lease_proof.race_id!r} does not match this write's actual "
                f"target race {race_id!r} — a lease proof acquired for one race can never "
                "authorize a write to another race"
            )
        expected_lease_name = simulator_lease_name(race_id)
        if lease_proof.lease_name != expected_lease_name:
            raise StaleWriteError(
                f"lease_proof lease_name {lease_proof.lease_name!r} does not match the expected "
                f"convention {expected_lease_name!r} for race {race_id!r}"
            )
        async with self._lease_repository.lock:
            if not self._lease_repository.matches_locked(
                lease_proof.lease_name, race_id, lease_proof.owner_id, lease_proof.fencing_token
            ):
                raise StaleWriteError(
                    f"lease_proof (owner={lease_proof.owner_id!r}, "
                    f"fencing_token={lease_proof.fencing_token}) for race {race_id!r} no longer "
                    "matches the live lease record (superseded, expired, or never acquired) — "
                    "a stale leader may not write"
                )

    async def save_state_from_simulator(
        self, state: RaceState, *, lease_proof: Optional[LeaseProof] = None
    ) -> RaceState:
        async with self._lock:
            active_run_id = self._active_run_ids.get(state.race_id)
            if active_run_id is not None and active_run_id != state.run_id:
                raise StaleWriteError(
                    f"run_id {state.run_id!r} is not the active run "
                    f"({active_run_id!r}) for race {state.race_id!r} — only "
                    f"begin_new_run may change the active run"
                )
            existing = self._states.get(state.race_id)
            if existing is not None and existing.run_id == state.run_id and state.state_version <= existing.state_version:
                raise StaleWriteError(
                    f"state_version {state.state_version} is not newer than "
                    f"stored version {existing.state_version} for run {state.run_id}"
                )
            await self._verify_lease_proof_locked(state.race_id, lease_proof)
            if active_run_id is None:
                self._active_run_ids[state.race_id] = state.run_id  # bootstrap — see base.py docstring
            self._states[state.race_id] = state
            return state

    async def get_control(self, race_id: str) -> Optional[DemoControlState]:
        async with self._lock:
            return self._controls.get(race_id)

    async def save_control_from_simulator(
        self, control: DemoControlState, *, lease_proof: Optional[LeaseProof] = None
    ) -> DemoControlState:
        async with self._lock:
            active_run_id = self._active_run_ids.get(control.race_id)
            if active_run_id is not None and active_run_id != control.run_id:
                raise StaleWriteError(
                    f"run_id {control.run_id!r} is not the active run "
                    f"({active_run_id!r}) for race {control.race_id!r} — only "
                    f"begin_new_run may change the active run"
                )
            existing = self._controls.get(control.race_id)
            if existing is not None and existing.run_id == control.run_id and control.state_version < existing.state_version:
                raise StaleWriteError(
                    f"control state_version {control.state_version} regresses stored "
                    f"version {existing.state_version} for run {control.run_id}"
                )
            # Production-fix pass: fence this write against the CURRENTLY
            # stored control_revision — closes the "valid leader overwrites
            # an operator pause" race. Checked under the SAME lock as the
            # rest of this method, so it is atomic with respect to a
            # concurrent transition_control_from_operator call (which also
            # requires this same lock). A mismatch means an operator
            # command has committed since `control` was built from an
            # earlier get_control() read — a stale READ, not a stale
            # LEASE, and is rejected exactly like every other fencing
            # check here.
            if existing is not None and existing.run_id == control.run_id and existing.control_revision != control.control_revision:
                raise StaleWriteError(
                    f"control_revision {control.control_revision} for race {control.race_id!r} "
                    f"does not match the currently stored revision {existing.control_revision} — "
                    "an operator command has committed since this write was built; a valid "
                    "simulator lease does not override an operator's control_revision"
                )
            await self._verify_lease_proof_locked(control.race_id, lease_proof)
            if active_run_id is None:
                self._active_run_ids[control.race_id] = control.run_id  # bootstrap
            # `control_revision` is intentionally left exactly as carried on
            # `control` — this method never touches it (see base.py's
            # docstring); callers (the simulator tick loop) always build
            # `control` via `existing.model_copy(update={...})`, which
            # naturally preserves whatever value was already stored.
            self._controls[control.race_id] = control
            return control

    async def transition_control_from_operator(
        self,
        race_id: str,
        run_id: str,
        *,
        allowed_current_statuses: frozenset[SimulatorStatus],
        new_status: SimulatorStatus,
        current_lap: Optional[int] = None,
    ) -> DemoControlState:
        async with self._lock:
            active_run_id = self._active_run_ids.get(race_id)
            if active_run_id is None or active_run_id != run_id:
                raise StaleWriteError(
                    f"run_id {run_id!r} is not the active run ({active_run_id!r}) for race "
                    f"{race_id!r} — operator transition rejected"
                )
            existing = self._controls.get(race_id)
            if existing is None or existing.run_id != run_id:
                raise StaleWriteError(
                    f"no control record exists for the active run of race {race_id!r} — cannot "
                    "apply an operator transition"
                )
            if existing.status not in allowed_current_statuses:
                raise InvalidControlTransitionError(
                    f"cannot transition race {race_id!r} to {new_status.value!r}: current status "
                    f"{existing.status.value!r} is not one of "
                    f"{sorted(s.value for s in allowed_current_statuses)}"
                )
            updated = existing.model_copy(
                update={
                    "status": new_status,
                    "current_lap": current_lap if current_lap is not None else existing.current_lap,
                    "updated_at": utc_now(),
                    # The one and only place control_revision is ever
                    # incremented — atomically, under this same lock, so
                    # two concurrent operator calls can never both compute
                    # the same "next" value.
                    "control_revision": existing.control_revision + 1,
                }
            )
            self._controls[race_id] = updated
            return updated

    async def begin_new_run(self, race_id: str, state: RaceState, control: DemoControlState) -> None:
        async with self._lock:
            active_run_id = self._active_run_ids.get(race_id)
            if active_run_id == state.run_id:
                raise ValueError(
                    f"begin_new_run must establish a NEW run_id for race {race_id!r}, "
                    f"got the already-active run_id {state.run_id!r}"
                )
            # Holding self._lock for the whole switch is what makes this
            # atomic with respect to concurrent save_state/save_control
            # calls: any such call already blocked on the lock re-checks
            # active_run_id against the NEW value once it acquires the
            # lock, so a delayed old-run write started before this call
            # (or racing against it) can never land after this point.
            # Repair pass: no lease bookkeeping to reset here — see
            # base.py's `begin_new_run` docstring for why a reset does
            # not need to touch the (race-scoped, not run-scoped) lease.
            self._active_run_ids[race_id] = state.run_id
            self._states[race_id] = state
            self._controls[race_id] = control
