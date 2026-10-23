from __future__ import annotations

import asyncio

import pytest

from app.domain.models import SimulatorStatus
from app.repositories.base import StaleWriteError
from tests.conftest import TEST_TICK_SECONDS

HALF_TICK = TEST_TICK_SECONDS / 2
ONE_TICK = TEST_TICK_SECONDS + 0.01
FULL_RUN = TEST_TICK_SECONDS * 6 + 0.2  # enough real time for all 4 remaining laps


async def _wait_until_lap(simulator, target_lap: int, timeout_seconds: float = FULL_RUN) -> None:
    elapsed = 0.0
    step = 0.01
    while elapsed < timeout_seconds:
        state = await simulator.get_state(simulator.race_id)
        if state.lap >= target_lap:
            return
        await asyncio.sleep(step)
        elapsed += step


# 1. Initial state is lap 24 and state version zero.
async def test_initial_state_is_lap_24_version_zero(simulator):
    state = await simulator.get_state(simulator.race_id)
    assert state.lap == 24
    assert state.state_version == 0
    assert state.synthetic is True
    assert len(state.drivers) == 4


# 2. Start advances the scenario in the correct order.
async def test_start_advances_scenario_in_order(simulator):
    laps_seen = [(await simulator.get_state(simulator.race_id)).lap]
    await simulator.start(simulator.race_id)

    for _ in range(6):
        await asyncio.sleep(ONE_TICK)
        laps_seen.append((await simulator.get_state(simulator.race_id)).lap)

    assert laps_seen == sorted(laps_seen)
    assert laps_seen[-1] == 28
    observed_unique = sorted(set(laps_seen))
    assert observed_unique == [24, 25, 26, 27, 28]


# 3. Repeated start does not create duplicate loops.
async def test_repeated_start_does_not_duplicate_loop(simulator):
    control_a = await simulator.start(simulator.race_id)
    task_a = simulator._task  # noqa: SLF001

    control_b = await simulator.start(simulator.race_id)
    task_b = simulator._task  # noqa: SLF001

    assert control_a.status == SimulatorStatus.RUNNING
    assert control_b.status in (SimulatorStatus.RUNNING, SimulatorStatus.COMPLETED)
    assert task_a is task_b  # identical underlying loop — no second loop spawned


# 4. Pause prevents advancement.
async def test_pause_prevents_advancement(simulator):
    await simulator.start(simulator.race_id)
    await asyncio.sleep(HALF_TICK)  # let the loop begin, before the first tick fires
    await simulator.pause(simulator.race_id)
    state_after_pause = await simulator.get_state(simulator.race_id)

    await asyncio.sleep(TEST_TICK_SECONDS * 3)
    state_later = await simulator.get_state(simulator.race_id)

    assert state_later.lap == state_after_pause.lap
    assert state_later.state_version == state_after_pause.state_version


# 5. Reset generates a new run ID.
async def test_reset_generates_new_run_id(simulator):
    initial_state = await simulator.get_state(simulator.race_id)
    await simulator.start(simulator.race_id)
    await asyncio.sleep(ONE_TICK)

    reset_state = await simulator.reset(simulator.race_id)
    assert reset_state.run_id != initial_state.run_id


# 6. Reset returns the state to lap 24.
async def test_reset_returns_to_lap_24(simulator):
    await simulator.start(simulator.race_id)
    await _wait_until_lap(simulator, 25)
    mid_state = await simulator.get_state(simulator.race_id)
    assert mid_state.lap > 24  # sanity: race actually progressed

    reset_state = await simulator.reset(simulator.race_id)
    assert reset_state.lap == 24
    assert reset_state.state_version == 0


# 7. An old run cannot update the new run after reset.
async def test_old_run_cannot_update_after_reset(simulator):
    old_state = await simulator.get_state(simulator.race_id)
    old_run_id = old_state.run_id

    new_state = await simulator.reset(simulator.race_id)
    assert new_state.run_id != old_run_id

    stale_next = old_state.model_copy(update={"state_version": old_state.state_version + 1})
    with pytest.raises(StaleWriteError):
        await simulator._repository.save_state_from_simulator(stale_next)  # noqa: SLF001


# 8. State version increases monotonically.
async def test_state_version_increases_monotonically(simulator):
    versions = [(await simulator.get_state(simulator.race_id)).state_version]
    await simulator.start(simulator.race_id)
    for _ in range(5):
        await asyncio.sleep(ONE_TICK)
        versions.append((await simulator.get_state(simulator.race_id)).state_version)

    for earlier, later in zip(versions, versions[1:]):
        assert later >= earlier
    assert versions[-1] > versions[0]


# 9. The simulator completes at lap 28.
async def test_simulator_completes_at_lap_28(simulator):
    await simulator.start(simulator.race_id)
    await _wait_until_lap(simulator, 28)

    control = await simulator.get_control(simulator.race_id)
    state = await simulator.get_state(simulator.race_id)
    assert state.lap == 28
    assert control.status == SimulatorStatus.COMPLETED


async def test_advance_once_helper_moves_exactly_one_lap(simulator):
    before = await simulator.get_state(simulator.race_id)
    after = await simulator.advance_once_for_testing(simulator.race_id)
    assert after.lap == before.lap + 1
    assert after.state_version == before.state_version + 1


# Local-demo-fixed repair, issue 4: the scenario intentionally authors only
# laps 24-28 (5 demo laps out of the fictional 58-lap race) and stopping
# there is correct completion, not a frozen/stuck simulator — but the
# tick loop and control status must make that unambiguous rather than
# leaving the demo looking like it silently died. These tests pin down
# the exact completion contract the demo script (and any UI) relies on.


async def test_status_is_running_while_progressing_and_completed_once_lap_28_is_reached(simulator):
    """The status endpoint's backing control state must clearly read
    RUNNING while the demo is still advancing, and only flip to COMPLETED
    once lap 28 (the authored end of this demo segment) is actually
    reached — never before, and never silently staying RUNNING forever
    after."""
    control_before_start = await simulator.get_control(simulator.race_id)
    assert control_before_start.status == SimulatorStatus.STOPPED

    await simulator.start(simulator.race_id)
    await asyncio.sleep(HALF_TICK)
    mid_control = await simulator.get_control(simulator.race_id)
    assert mid_control.status == SimulatorStatus.RUNNING
    assert mid_control.current_lap < 28

    await _wait_until_lap(simulator, 28)
    final_control = await simulator.get_control(simulator.race_id)
    assert final_control.status == SimulatorStatus.COMPLETED
    assert final_control.current_lap == 28


async def test_no_further_state_version_is_written_after_completion(simulator):
    """Once the demo reaches lap 28/COMPLETED, the background tick loop
    must stop advancing entirely - no further state_version bump, no
    further lap change - rather than continuing to tick harmlessly (or
    worse, erroring) forever in the background. This is what makes a
    polling client's own loop safe to stop the moment it observes
    COMPLETED: there is nothing more coming after it."""
    await simulator.start(simulator.race_id)
    await _wait_until_lap(simulator, 28)

    completed_state = await simulator.get_state(simulator.race_id)
    completed_control = await simulator.get_control(simulator.race_id)
    assert completed_state.lap == 28
    assert completed_control.status == SimulatorStatus.COMPLETED

    # Give the (already-finished) tick loop several more full intervals to
    # prove it does not keep writing after completion.
    await asyncio.sleep(TEST_TICK_SECONDS * 3)

    still_state = await simulator.get_state(simulator.race_id)
    still_control = await simulator.get_control(simulator.race_id)
    assert still_state.lap == 28
    assert still_state.state_version == completed_state.state_version
    assert still_control.status == SimulatorStatus.COMPLETED

    # The background loop task itself should have exited (returned False
    # from `_advance`), not be sitting there still ticking indefinitely.
    assert simulator._task is not None  # noqa: SLF001
    assert simulator._task.done()  # noqa: SLF001


async def test_restart_after_completion_is_a_no_op_and_reset_starts_a_fresh_run(simulator):
    """Calling `/start` again after COMPLETED must not error and must not
    resurrect the finished run (spec: "COMPLETED; caller must reset to run
    again") - it is reported as-is. A subsequent `reset()` is the one
    documented way back to a fresh, running demo, and that fresh run must
    genuinely restart from lap 24, not continue from wherever the
    completed run left off."""
    await simulator.start(simulator.race_id)
    await _wait_until_lap(simulator, 28)
    completed_control = await simulator.get_control(simulator.race_id)
    assert completed_control.status == SimulatorStatus.COMPLETED

    # Restarting a COMPLETED run is a no-op that reports COMPLETED as-is.
    restart_attempt = await simulator.start(simulator.race_id)
    assert restart_attempt.status == SimulatorStatus.COMPLETED
    still_state = await simulator.get_state(simulator.race_id)
    assert still_state.lap == 28

    # Reset is the documented way back to a fresh, running demo.
    reset_state = await simulator.reset(simulator.race_id)
    assert reset_state.lap == 24
    assert reset_state.state_version == 0
    reset_control = await simulator.get_control(simulator.race_id)
    assert reset_control.status == SimulatorStatus.STOPPED
    assert reset_control.run_id != completed_control.run_id

    # And the fresh run can actually run again through to completion.
    await simulator.start(simulator.race_id)
    await _wait_until_lap(simulator, 28)
    final_control = await simulator.get_control(simulator.race_id)
    assert final_control.status == SimulatorStatus.COMPLETED
