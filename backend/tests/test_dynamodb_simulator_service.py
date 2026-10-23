"""P0-1 repair pass: service-level tests proving `/start`/`/pause`/the tick
loop all behave correctly against the ACTUAL production DynamoDB wiring
(`DynamoDBRaceStateRepository(require_lease_proof=True)` +
`DynamoDBLeaseRepository`, moto-backed, driven through a real
`SimulatorService` — never memory mode).

Root cause this repairs: `factory.py` constructs
`DynamoDBRaceStateRepository(require_lease_proof=True)` unconditionally for
`STATE_REPOSITORY=dynamodb`, which made every `save_control` call WITHOUT a
`lease_proof` raise `StaleWriteError` — but `SimulatorService.start`/`pause`
are operator commands that must work even when the calling backend task
does not hold (and never acquires) the simulator leadership lease. See
`app/repositories/base.py`'s docstring for `transition_control_from_operator`
— the fix — and `app/services/simulator.py`'s `start`/`pause` for how they
now use it instead of the leader-owned `save_control_from_simulator`.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.domain.models import (  # noqa: E402
    DemoControlState,
    DriverState,
    RaceState,
    SimulatorStatus,
    TireCompound,
    TrackState,
    utc_now,
)
from app.repositories.base import StaleWriteError  # noqa: E402
from app.repositories.lease import LeaseProof, simulator_lease_name  # noqa: E402
from app.services.simulator import SimulatorService  # noqa: E402
from tests.conftest import TEST_TICK_SECONDS  # noqa: E402

TABLE_NAME = "pit-wall-dynamodb-simulator-service-test"


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


def _build_repository():
    """Mirrors exactly what `factory.build_repository` constructs for
    `STATE_REPOSITORY=dynamodb`: `require_lease_proof=True`. This is the
    real production shape, not a relaxed test stand-in."""
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    return DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1", require_lease_proof=True)


def _build_lease_repository():
    from app.repositories.lease import DynamoDBLeaseRepository

    return DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")


def _build_service(scenario, repository, lease_repository=None, on_state_committed=None):
    return SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 6,
        on_state_committed=on_state_committed,
    )


def _driver() -> DriverState:
    return DriverState(
        driver_id="CAR_03",
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


def _make_state(scenario, *, run_id: str, state_version: int) -> RaceState:
    return RaceState(
        race_id=scenario.race_id,
        run_id=run_id,
        state_version=state_version,
        lap=24 + state_version,
        total_laps=scenario.total_laps,
        race_time_ms=1000 * state_version,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id=scenario.scenario_id,
        synthetic=True,
        drivers=(_driver(),),
    )


def _make_control(scenario, *, run_id: str, state_version: int, status: SimulatorStatus = SimulatorStatus.RUNNING) -> DemoControlState:
    return DemoControlState(
        race_id=scenario.race_id,
        run_id=run_id,
        status=status,
        scenario_id=scenario.scenario_id,
        current_lap=24,
        state_version=state_version,
        updated_at=utc_now(),
    )


# 1. Initialize -> start succeeds.
async def test_initialize_then_start_succeeds(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()

    control = await service.start(service.race_id)
    assert control.status == SimulatorStatus.RUNNING

    await service._cancel_active_loop()  # noqa: SLF001 test cleanup


# 2. Start -> pause succeeds.
async def test_start_then_pause_succeeds(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()

    await service.start(service.race_id)
    paused = await service.pause(service.race_id)
    assert paused.status == SimulatorStatus.PAUSED

    await service._cancel_active_loop()  # noqa: SLF001


# 3. Pause -> resume (start again) succeeds.
async def test_pause_then_resume_succeeds(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()

    await service.start(service.race_id)
    await service.pause(service.race_id)
    resumed = await service.start(service.race_id)
    assert resumed.status == SimulatorStatus.RUNNING

    await service._cancel_active_loop()  # noqa: SLF001


# 4. A start request lands on an instance that is not, and does not
#    become, the current simulator leader — still succeeds.
async def test_start_succeeds_without_holding_simulator_leadership(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    instance_a = _build_service(scenario, repository, lease_repository)
    instance_b = _build_service(scenario, repository, lease_repository)
    await instance_a.ensure_initialized()

    # Instance A genuinely holds the simulator lease; instance B never
    # acquires it at all before calling start().
    lease_name = simulator_lease_name(instance_a.race_id)
    grant = await lease_repository.try_acquire_or_renew(
        lease_name, instance_a.race_id, instance_a._instance_id, ttl_seconds=30.0  # noqa: SLF001
    )
    assert grant is not None

    control = await instance_b.start(instance_b.race_id)
    assert control.status == SimulatorStatus.RUNNING

    await instance_a._cancel_active_loop()  # noqa: SLF001
    await instance_b._cancel_active_loop()  # noqa: SLF001


# 5. A pause request lands on a non-leader instance — still succeeds.
async def test_pause_succeeds_on_non_leader_instance(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    instance_a = _build_service(scenario, repository, lease_repository)
    instance_b = _build_service(scenario, repository, lease_repository)
    await instance_a.ensure_initialized()
    await instance_a.start(instance_a.race_id)

    lease_name = simulator_lease_name(instance_a.race_id)
    grant = await lease_repository.try_acquire_or_renew(
        lease_name, instance_a.race_id, instance_a._instance_id, ttl_seconds=30.0  # noqa: SLF001
    )
    assert grant is not None

    paused = await instance_b.pause(instance_b.race_id)
    assert paused.status == SimulatorStatus.PAUSED

    await instance_a._cancel_active_loop()  # noqa: SLF001
    await instance_b._cancel_active_loop()  # noqa: SLF001


# 6. The running simulator advances only after obtaining a lease: it
#    advances while holding the lease, and stalls (no further advance)
#    once a different owner holds the lease instead.
async def test_running_simulator_advances_only_after_obtaining_lease(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()
    await service.start(service.race_id)

    advanced = False
    # Repair pass (clean-env full-suite flake found and fixed): this test
    # polls a REAL background tick loop (a genuine timer-driven loop, not
    # something a direct-state-manipulation trick can substitute for --
    # unlike the DynamoDB lease-expiry test's fix in test_dynamodb_
    # repository.py, what's actually being proven here is that the
    # service's own tick loop advances the race, which requires it to
    # really run). The prior budget (80 iterations * TEST_TICK_SECONDS ==
    # 2.4s of real wall-clock time) was observed to occasionally fail
    # under a CPU-contended full-suite run, where the event loop can be
    # starved long enough that the tick loop never gets scheduled within
    # that window -- not a defect in the simulator itself (isolated runs
    # of this exact test pass reliably and quickly, well under 2.4s).
    # Local-demo-fixed repair pass: even the previous 400-iteration (12s)
    # budget was still observed to occasionally fail under this pass's
    # heavier full-suite load (more tests now collected overall than when
    # that budget was set). Raised further to 1000 iterations (30s
    # ceiling) for the same reason as before -- purely to absorb
    # scheduler/event-loop contention from everything else running
    # concurrently in a full-suite run, never to change what is being
    # proven. This branch still returns as soon as `advanced` becomes
    # true, so a healthy run (the overwhelming majority) is unaffected in
    # wall-clock time; only a starved run gets the extra headroom. The
    # assertion itself, and the mechanism being tested (the service must
    # genuinely advance on its own via its real tick loop while holding
    # the lease), are completely unchanged.
    for _ in range(1000):
        await asyncio.sleep(TEST_TICK_SECONDS)
        state = await repository.get_state(service.race_id)
        if state.lap > 24:
            advanced = True
            break
    assert advanced, "simulator never advanced despite holding the lease"
    await service._cancel_active_loop()  # noqa: SLF001

    # A different owner now holds the lease. A fresh service instance's
    # loop must not be able to advance the race further. `service`'s own
    # lease may not have expired the instant its loop was cancelled, so
    # retry the acquisition until "someone-else" genuinely holds it.
    lease_name = simulator_lease_name(service.race_id)
    other_owner_grant = None
    for _ in range(100):
        other_owner_grant = await lease_repository.try_acquire_or_renew(
            lease_name, service.race_id, "someone-else", ttl_seconds=30.0
        )
        if other_owner_grant is not None:
            break
        await asyncio.sleep(TEST_TICK_SECONDS)
    assert other_owner_grant is not None, "never managed to take the lease away as a different owner"
    stalled_before = await repository.get_state(service.race_id)

    other_service = _build_service(scenario, repository, lease_repository)
    await other_service.start(other_service.race_id)
    await asyncio.sleep(TEST_TICK_SECONDS * 5)
    stalled_after = await repository.get_state(service.race_id)
    assert stalled_after.lap == stalled_before.lap
    await other_service._cancel_active_loop()  # noqa: SLF001


# 7. A proofless simulator state write is rejected.
async def test_proofless_simulator_state_write_rejected(scenario, dynamodb_table):
    repository = _build_repository()
    run_id = "run-proofless-state"
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id=run_id, state_version=0),
        _make_control(scenario, run_id=run_id, state_version=0),
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(
            _make_state(scenario, run_id=run_id, state_version=1), lease_proof=None
        )


# 8. A proofless simulator control/completion write is rejected.
async def test_proofless_simulator_control_write_rejected(scenario, dynamodb_table):
    repository = _build_repository()
    run_id = "run-proofless-control"
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id=run_id, state_version=0),
        _make_control(scenario, run_id=run_id, state_version=0),
    )
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(
            _make_control(scenario, run_id=run_id, state_version=1, status=SimulatorStatus.COMPLETED),
            lease_proof=None,
        )


# 9. An authorized operator transition succeeds without simulator
#    leadership — no lease_repository involvement at all (not even
#    constructed).
async def test_operator_transition_succeeds_with_no_lease_repository_at_all(scenario, dynamodb_table):
    repository = _build_repository()
    service = SimulatorService(
        repository=repository, scenario=scenario, sleep_fn=asyncio.sleep, tick_interval_seconds=TEST_TICK_SECONDS
    )  # no lease_repository at all

    control = await service.start(service.race_id)
    assert control.status == SimulatorStatus.RUNNING

    paused = await service.pause(service.race_id)
    assert paused.status == SimulatorStatus.PAUSED

    await service._cancel_active_loop()  # noqa: SLF001


# 10. Repeated start is idempotent.
async def test_repeated_start_is_idempotent(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()

    first = await service.start(service.race_id)
    second = await service.start(service.race_id)
    assert first.status == SimulatorStatus.RUNNING
    assert second.status == SimulatorStatus.RUNNING

    await service._cancel_active_loop()  # noqa: SLF001


# 11. Concurrent start calls (two instances) do not raise and both report
#     RUNNING.
async def test_concurrent_start_calls_across_instances_do_not_raise(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    instance_a = _build_service(scenario, repository, lease_repository)
    instance_b = _build_service(scenario, repository, lease_repository)
    await instance_a.ensure_initialized()

    results = await asyncio.gather(
        instance_a.start(instance_a.race_id), instance_b.start(instance_b.race_id)
    )
    assert all(control.status == SimulatorStatus.RUNNING for control in results)

    final_control = await repository.get_control(instance_a.race_id)
    assert final_control.status == SimulatorStatus.RUNNING

    await instance_a._cancel_active_loop()  # noqa: SLF001
    await instance_b._cancel_active_loop()  # noqa: SLF001


# 12. A paused state cannot be overwritten by an in-flight stale tick: a
#     leader that has been superseded, then attempts a control write with
#     its now-stale proof AFTER an operator has paused, must be rejected —
#     PAUSED must remain stored.
async def test_paused_state_not_overwritten_by_stale_in_flight_tick(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    run_id = "run-pause-fence"
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id=run_id, state_version=0),
        _make_control(scenario, run_id=run_id, state_version=0),
    )

    lease_name = simulator_lease_name(scenario.race_id)
    grant_a = await lease_repository.try_acquire_or_renew(lease_name, scenario.race_id, "leader-a", ttl_seconds=0.3)
    proof_a = LeaseProof.from_grant(lease_name, grant_a)
    await repository.save_state_from_simulator(
        _make_state(scenario, run_id=run_id, state_version=1), lease_proof=proof_a
    )

    await asyncio.sleep(1.0)  # leader-a's lease is now expired
    grant_b = await lease_repository.try_acquire_or_renew(lease_name, scenario.race_id, "leader-b", ttl_seconds=30.0)
    assert grant_b is not None and grant_b.owner_id == "leader-b"

    # Operator pauses through a fresh SimulatorService pointed at the SAME
    # repository — no lease_repository wired in, exactly like a real
    # `/pause` request handled on a task that never touches leadership.
    operator_service = _build_service(scenario, repository, lease_repository=None)
    paused_control = await operator_service.pause(scenario.race_id)
    assert paused_control.status == SimulatorStatus.PAUSED

    # Leader A's stale, in-flight tick attempts a control write using its
    # now-superseded proof (leader B has since taken over the lease).
    stale_write = _make_control(scenario, run_id=run_id, state_version=1, status=SimulatorStatus.RUNNING)
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(stale_write, lease_proof=proof_a)

    stored_control = await repository.get_control(scenario.race_id)
    assert stored_control.status == SimulatorStatus.PAUSED


# 13. Reset followed by a delayed old-run write remains rejected.
async def test_reset_then_delayed_old_run_write_rejected(scenario, dynamodb_table):
    repository = _build_repository()
    service = _build_service(scenario, repository, lease_repository=None)
    await service.ensure_initialized()

    old_run_id = service._active_run_id  # noqa: SLF001
    old_state = await repository.get_state(service.race_id)

    await service.reset(service.race_id)

    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(
            old_state.model_copy(update={"state_version": old_state.state_version + 1})
        )

    stored = await repository.get_state(service.race_id)
    assert stored.run_id != old_run_id


# 14. A full run reaches COMPLETED correctly in DynamoDB mode.
async def test_full_run_reaches_completed_in_dynamodb_mode(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()

    lease_name = simulator_lease_name(service.race_id)
    max_iterations = len(scenario.laps) + 10
    for _ in range(max_iterations):
        control = await repository.get_control(service.race_id)
        if control.status == SimulatorStatus.COMPLETED:
            break
        grant = await lease_repository.try_acquire_or_renew(
            lease_name, service.race_id, service._instance_id, ttl_seconds=30.0  # noqa: SLF001
        )
        assert grant is not None
        proof = LeaseProof.from_grant(lease_name, grant)
        await service._advance(service.race_id, control.run_id, lease_proof=proof)  # noqa: SLF001
    else:
        pytest.fail("simulator never reached COMPLETED within the expected number of advances")

    final_control = await repository.get_control(service.race_id)
    assert final_control.status == SimulatorStatus.COMPLETED


class _PauseInjectingRepository:
    """Wraps a real `RaceStateRepository` so that the FIRST call to
    `get_control` after construction triggers a caller-supplied async
    callback BEFORE delegating to the real `get_control` — a deterministic
    way to force "an operator's pause lands exactly between the
    simulator's state write and its post-write control re-read" without
    any sleep-based timing. `SimulatorService._advance` calls
    `get_control` exactly once per (non-final-lap) tick, immediately
    after its state write has already committed."""

    def __init__(self, inner, on_first_get_control):
        self._inner = inner
        self._on_first_get_control = on_first_get_control
        self._triggered = False

    async def get_control(self, race_id):
        if not self._triggered:
            self._triggered = True
            await self._on_first_get_control()
        return await self._inner.get_control(race_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# 15. Defect 1 regression, DynamoDB-backed service level (scenarios 2 & 3):
# the simulator reads RUNNING, commits its state write, and — before it
# re-reads control — an operator's pause lands (deterministically forced,
# not sleep-based). The fresh control read `_advance` performs afterwards
# must see PAUSED and PRESERVE it, not force it back to RUNNING. The
# simulator's lease is genuinely still valid throughout (never
# superseded/expired) — proving this exercises the fresh-read fix, not
# the pre-existing stale-LEADER protection (already covered above by
# `test_paused_state_not_overwritten_by_stale_in_flight_tick`, which uses
# a superseded lease instead).
async def test_pause_landing_during_in_flight_tick_is_preserved_in_dynamodb_mode(scenario, dynamodb_table):
    repository = _build_repository()
    lease_repository = _build_lease_repository()
    service = _build_service(scenario, repository, lease_repository)
    await service.ensure_initialized()
    run_id = service._active_run_id  # noqa: SLF001

    await repository.transition_control_from_operator(
        service.race_id,
        run_id,
        allowed_current_statuses=frozenset({SimulatorStatus.STOPPED}),
        new_status=SimulatorStatus.RUNNING,
    )

    lease_name = simulator_lease_name(service.race_id)
    grant = await lease_repository.try_acquire_or_renew(
        lease_name, service.race_id, service._instance_id, ttl_seconds=30.0  # noqa: SLF001
    )
    assert grant is not None
    lease_proof = LeaseProof.from_grant(lease_name, grant)

    async def _operator_pauses() -> None:
        paused = await service.pause(service.race_id)
        assert paused.status == SimulatorStatus.PAUSED

    service._repository = _PauseInjectingRepository(repository, _operator_pauses)  # noqa: SLF001

    advanced = await service._advance(service.race_id, run_id, lease_proof=lease_proof)  # noqa: SLF001
    assert advanced is True  # the tick itself was accepted; not rejected as a stale leader

    stored_state = await repository.get_state(service.race_id)
    assert stored_state.lap == 25  # the state write still committed

    stored_control = await repository.get_control(service.race_id)
    assert stored_control.status == SimulatorStatus.PAUSED  # preserved, not forced back to RUNNING
    assert stored_control.current_lap == 25
    assert stored_control.state_version == stored_state.state_version
