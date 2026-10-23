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
