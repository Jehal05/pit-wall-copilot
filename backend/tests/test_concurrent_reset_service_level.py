"""Service-level concurrent-reset test (spec v1.1 pass 3, correction 9).

`tests/test_active_run_protection.py::test_concurrent_reset_and_old_run_advance_cannot_resurrect_old_run`
already proves the underlying invariant at the REPOSITORY layer (direct
`repo.begin_new_run`/`repo.save_state` calls, 5-way raced). This file adds
the service-level counterpart the correction explicitly asks for: two
concurrent `SimulatorService.reset()` calls, deliberately reordered, with
assertions phrased in terms of what a real caller (the API layer, a
connected WebSocket client) actually observes — not just what the
repository's own internal dict ends up holding.

Per the correction's explicit instruction, this test does NOT decide a
"winner" by comparing UUIDs — the winner is decided purely by which
`reset()` call is the last one to acquire `SimulatorService._reset_lock`
(see that lock's doc-comment in `app/services/simulator.py`), and this
test verifies that outcome is what's actually observed everywhere.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.models import RaceState
from app.repositories.base import StaleWriteError
from app.repositories.memory import InMemoryRaceStateRepository
from app.services.simulator import SimulatorService
from tests.conftest import TEST_TICK_SECONDS


class _DelayableRepository(InMemoryRaceStateRepository):
    """Wraps the real in-memory repository so a specific `begin_new_run`
    call can be held open past the point it would otherwise return —
    used here to prove the OUTCOME is decided by `_reset_lock`
    acquisition order, not by which repository write happens to land
    first (before this correction, it was the other way around, which is
    exactly the bug)."""

    def __init__(self) -> None:
        super().__init__()
        self.delay_next_begin_new_run: asyncio.Event = asyncio.Event()
        self.delay_next_begin_new_run.set()  # no delay by default
        self.begin_new_run_calls: list[str] = []

    async def begin_new_run(self, race_id, state, control) -> None:  # type: ignore[override]
        self.begin_new_run_calls.append(state.run_id)
        await self.delay_next_begin_new_run.wait()
        await super().begin_new_run(race_id, state, control)


@pytest.mark.asyncio
async def test_two_concurrent_resets_leave_the_service_and_repository_agreeing_on_one_final_run(scenario):
    """The core correction 9 assertion: after two `reset()` calls race,
    there is exactly one final active run, and EVERY observable surface
    (repository state, repository control, the service's own
    `_active_run_id`, and the sequence of on_state_committed
    notifications a caller would have received as RACE_RESET events)
    agrees on which one it is."""
    repository = _DelayableRepository()
    committed_states: list[RaceState] = []

    async def _record(state: RaceState) -> None:
        committed_states.append(state)

    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        on_state_committed=_record,
    )
    await service.ensure_initialized()
    committed_states.clear()  # only care about the two resets below

    # Deliberately reorder: hold the FIRST reset's repository write open
    # until the SECOND reset has had a chance to be scheduled, so a
    # naive (unlocked) implementation would let the second's repository
    # write land first while the first's in-process bookkeeping still
    # "wins" locally — the exact divergence this correction closes.
    repository.delay_next_begin_new_run.clear()

    async def _delayed_release() -> None:
        await asyncio.sleep(TEST_TICK_SECONDS)
        repository.delay_next_begin_new_run.set()

    reset_a = asyncio.create_task(service.reset(service.race_id))
    reset_b = asyncio.create_task(service.reset(service.race_id))
    release_task = asyncio.create_task(_delayed_release())

    state_a, state_b = await asyncio.gather(reset_a, reset_b)
    await release_task

    assert state_a.run_id != state_b.run_id, "two resets must establish two DIFFERENT run_ids"

    # Because reset() holds _reset_lock for its entire body (including the
    # repository await), the two calls cannot interleave — whichever
    # acquired the lock SECOND is unambiguously the final run, and that is
    # true everywhere.
    final_state = await repository.get_state(service.race_id)
    final_control = await repository.get_control(service.race_id)

    assert final_state.run_id == final_control.run_id, (
        "repository state and control must share the same active run_id "
        "after concurrent resets settle"
    )
    assert service._active_run_id == final_state.run_id, (  # noqa: SLF001
        "the simulator's own internal active-run tracking must match "
        "stored state after settling — a caller relying on the service "
        "directly (e.g. the tick loop) must never disagree with what's "
        "actually persisted"
    )

    # The LAST on_state_committed notification (the service's equivalent
    # of a RACE_RESET broadcast) must correspond to the run that actually
    # ended up active — a client that received an EARLIER reset's
    # notification and then never received a later correcting one would
    # be showing a superseded run as if it were still current, which is
    # exactly what must not happen.
    assert committed_states, "expected at least one on_state_committed notification"
    assert committed_states[-1].run_id == final_state.run_id, (
        "the final RACE_RESET-equivalent notification must describe the "
        "run that is actually active, not a superseded one"
    )
    # Every notified run_id is one of the two contenders — no phantom run.
    assert {s.run_id for s in committed_states} <= {state_a.run_id, state_b.run_id}


@pytest.mark.asyncio
async def test_a_delayed_write_from_the_superseded_run_is_rejected_after_concurrent_resets(scenario):
    """After two resets race, a write for the run that LOST (the one that
    is no longer active) must still be rejected outright — not compared
    by state_version or UUID ordering against the winner — exactly the
    same guarantee test_active_run_protection.py proves at the repository
    layer, re-verified here as a consequence of a SERVICE-level race."""
    repository = _DelayableRepository()
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
    )
    await service.ensure_initialized()

    state_a, state_b = await asyncio.gather(
        service.reset(service.race_id),
        service.reset(service.race_id),
    )

    final_state = await repository.get_state(service.race_id)
    superseded_state = state_a if state_a.run_id != final_state.run_id else state_b
    assert superseded_state.run_id != final_state.run_id

    # A straggler write for the superseded run, arriving after the dust
    # has settled, must be rejected — regardless of how "new" its
    # state_version looks in isolation.
    stale_write = superseded_state.model_copy(update={"state_version": 999})
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(stale_write)


@pytest.mark.asyncio
async def test_starting_after_concurrent_resets_advances_the_final_active_run_only(scenario):
    """Once the dust settles, start()/the tick loop must operate on the
    run that actually won — not silently keep advancing whichever run a
    caller happened to hold a local reference to."""
    repository = _DelayableRepository()
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
    )
    await service.ensure_initialized()

    state_a, state_b = await asyncio.gather(
        service.reset(service.race_id),
        service.reset(service.race_id),
    )
    final_state = await repository.get_state(service.race_id)

    control = await service.start(service.race_id)
    assert control.run_id == final_state.run_id

    await asyncio.sleep(TEST_TICK_SECONDS * 2.5)
    advanced_state = await service.get_state(service.race_id)
    assert advanced_state.run_id == final_state.run_id
    assert advanced_state.lap >= final_state.lap

    await service._cancel_active_loop()  # noqa: SLF001 test cleanup
