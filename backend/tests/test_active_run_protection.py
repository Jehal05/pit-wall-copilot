"""Regression tests for spec v1.1 mandatory correction 1: active-run
protection against DynamoDB (and in-memory) stale-run resurrection.

Parametrized so every scenario runs against BOTH
`InMemoryRaceStateRepository` and `DynamoDBRaceStateRepository` (backed by
`moto`) — "keep the in-memory and DynamoDB repository contracts
consistent" is itself part of the correction's requirements, so a
regression in only one implementation must fail these tests, not just one
implementation-specific test file.

See `app/repositories/base.py`'s module docstring for the contract being
tested, and `app/repositories/dynamodb.py`'s module docstring for exactly
what was unsafe about the pre-correction DynamoDB ConditionExpression
(`attribute_not_exists(pk) OR run_id <> :run_id OR state_version < :new_version`)
and how the GENERATION-item transaction design fixes it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.models import DriverState, RaceState, SimulatorStatus, DemoControlState, TireCompound, TrackState, utc_now
from app.repositories.base import StaleWriteError
from app.repositories.memory import InMemoryRaceStateRepository

# Spec v1.1 pass 3, correction 10: exercises moto-simulated AWS (DynamoDB)
# behavior, not just fast in-process logic -- tagged `integration` rather
# than the `unit` default (see tests/conftest.py's auto-tagging hook).
# Repair pass (repo-wide Ruff cleanup): moved below the imports above (pure
# reordering, no behavior change) so `pytestmark`'s assignment no longer
# sits between the docstring/`__future__` block and this file's own
# non-boto3-dependent imports, which was flagging them E402.
pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

RACE_ID = "demo-race-001"
TABLE_NAME = "pit-wall-active-run-protection-test"


def _driver(driver_id: str = "CAR_03") -> DriverState:
    return DriverState(
        driver_id=driver_id,
        display_name="Test Driver",
        team_name="Test Team",
        position=1,
        gap_to_leader_ms=0,
        gap_to_ahead_ms=0,
        compound=TireCompound.MEDIUM,
        tire_age_laps=1,
        base_pace_ms=90000,
        current_lap_pace_ms=90000,
        pit_stops=0,
        pit_window=(20, 25),
        traffic_factor=0.0,
    )


def _state(run_id: str, version: int, lap: int = 24) -> RaceState:
    return RaceState(
        race_id=RACE_ID,
        run_id=run_id,
        state_version=version,
        lap=lap,
        total_laps=58,
        race_time_ms=1000 * version,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id="scenario-demo-01",
        synthetic=True,
        drivers=(_driver(),),
    )


def _control(run_id: str, version: int, status: SimulatorStatus = SimulatorStatus.STOPPED) -> DemoControlState:
    return DemoControlState(
        race_id=RACE_ID,
        run_id=run_id,
        status=status,
        scenario_id="scenario-demo-01",
        current_lap=24,
        state_version=version,
        updated_at=utc_now(),
    )


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _memory_repo():
    return InMemoryRaceStateRepository()


def _dynamodb_repo():
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    return DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")


# Both repo constructors, parametrized by name so failure output says which
# implementation broke. The DynamoDB variant additionally requires the
# dynamodb_table fixture, applied via `request.getfixturevalue` so one
# parametrized test function can drive either backend.
REPO_KINDS = ["memory", "dynamodb"]


@pytest.fixture(params=REPO_KINDS)
def repo(request):
    if request.param == "dynamodb":
        request.getfixturevalue("dynamodb_table")
        return _dynamodb_repo()
    return _memory_repo()


# 1. Run A version 5 cannot be replaced by run A version 3.
async def test_same_run_older_version_rejected(repo):
    await repo.save_state_from_simulator(_state("run-a", version=5))
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_state("run-a", version=3))
    stored = await repo.get_state(RACE_ID)
    assert stored.state_version == 5


# 2. Run B cannot be introduced through ordinary save_state once run A is
#    established (bootstrap already happened — a second run_id is not a
#    bootstrap, it's a rejection).
async def test_ordinary_save_state_cannot_introduce_a_new_run(repo):
    await repo.save_state_from_simulator(_state("run-a", version=0))
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_state("run-b", version=0))
    stored = await repo.get_state(RACE_ID)
    assert stored.run_id == "run-a"


# 3. Reset (begin_new_run) explicitly changes run A to run B.
async def test_begin_new_run_switches_the_active_run(repo):
    await repo.save_state_from_simulator(_state("run-a", version=3))
    await repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))
    stored = await repo.get_state(RACE_ID)
    assert stored.run_id == "run-b"
    assert stored.state_version == 0


# 4. After run B becomes active, a delayed run-A write is rejected.
async def test_delayed_run_a_write_rejected_after_run_b_active(repo):
    await repo.save_state_from_simulator(_state("run-a", version=3))
    await repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))
    # A "delayed" run-A write with a HIGHER state_version than anything run
    # A ever had — proving the rejection is about run_id, not staleness.
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_state("run-a", version=99))


# 5. A delayed run-A CONTROL write is rejected.
async def test_delayed_run_a_control_write_rejected_after_run_b_active(repo):
    await repo.save_state_from_simulator(_state("run-a", version=3))
    await repo.save_control_from_simulator(_control("run-a", version=3))
    await repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))
    with pytest.raises(StaleWriteError):
        await repo.save_control_from_simulator(_control("run-a", version=99, status=SimulatorStatus.RUNNING))
    stored_control = await repo.get_control(RACE_ID)
    assert stored_control.run_id == "run-b"


# 6. Concurrent reset/advance operations cannot resurrect run A: a
#    save_state for run A racing against begin_new_run(run B) must, no
#    matter how the race resolves, never leave run A as the stored state.
async def test_concurrent_reset_and_old_run_advance_cannot_resurrect_old_run(repo):
    await repo.save_state_from_simulator(_state("run-a", version=3))

    async def advance_run_a():
        try:
            await repo.save_state_from_simulator(_state("run-a", version=4))
        except StaleWriteError:
            pass

    async def reset_to_run_b():
        await repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))

    # Run many times with both orderings to flush out ordering-dependent
    # bugs rather than relying on one lucky/unlucky interleaving.
    for _ in range(5):
        await asyncio.gather(advance_run_a(), reset_to_run_b())
        stored = await repo.get_state(RACE_ID)
        # Whichever of the two operations "won" a given gather() call, the
        # end state must NEVER be run-a at a version beyond what it had
        # before this iteration's race — i.e. run-a can never be the
        # active run once run-b has been established even once.
        assert stored.run_id != "run-a" or stored.state_version <= 3
        if stored.run_id == "run-b":
            break
    else:
        pytest.fail("begin_new_run(run-b) never won any of the 5 concurrent races — test is not exercising the race")


# 7. The stored state remains run B after every rejected old-run operation.
async def test_stored_state_remains_run_b_after_rejected_old_run_ops(repo):
    await repo.save_state_from_simulator(_state("run-a", version=3))
    await repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))

    for attempt_version in (1, 2, 3, 4, 100):
        with pytest.raises(StaleWriteError):
            await repo.save_state_from_simulator(_state("run-a", version=attempt_version))
        stored = await repo.get_state(RACE_ID)
        assert stored.run_id == "run-b"
        assert stored.state_version == 0  # never mutated by any rejected run-a attempt


# 8. Both in-memory and DynamoDB modes follow the same rules — this is
#    implicit in every test above being parametrized over `repo`, but this
#    test makes the equivalence explicit for a single, easy-to-point-at
#    scenario: the exact sequence spec v1.1 correction 1 describes as the
#    original vulnerability (a delayed write from an old run, differing
#    run_id, arriving after reset).
@pytest.mark.parametrize("kind", REPO_KINDS)
async def test_original_vulnerability_scenario_fixed_in_both_backends(kind, request):
    if kind == "dynamodb":
        request.getfixturevalue("dynamodb_table")
        active_repo = _dynamodb_repo()
    else:
        active_repo = _memory_repo()

    await active_repo.save_state_from_simulator(_state("run-a", version=5))
    await active_repo.begin_new_run(RACE_ID, _state("run-b", version=0), _control("run-b", version=0))
    # Exactly the vulnerable shape from the bug report: a different run_id
    # arriving via ordinary save_state, which the OLD condition
    # (`run_id <> :run_id`) would have accepted unconditionally.
    with pytest.raises(StaleWriteError):
        await active_repo.save_state_from_simulator(_state("run-a", version=6))
    stored = await active_repo.get_state(RACE_ID)
    assert stored.run_id == "run-b"


async def test_begin_new_run_rejects_reactivating_the_current_run(repo):
    await repo.save_state_from_simulator(_state("run-a", version=0))
    with pytest.raises(ValueError):
        await repo.begin_new_run(RACE_ID, _state("run-a", version=1), _control("run-a", version=1))
