"""P0-2 repair pass regression tests: cross-race lease-proof rejection.

Root cause (see `app/repositories/lease.py` and `app/repositories/base.py`'s
module docstrings for the full writeup): nothing previously checked that a
`LeaseProof`'s `race_id`/`lease_name` actually matched the write's ACTUAL
target race — `dynamodb.py`'s LEASE `ConditionCheck` and `memory.py`'s
`_verify_lease_proof_locked` both compared the live lease record against
`lease_proof.race_id`, a value taken from the proof ITSELF, which is
tautological. A proof legitimately acquired for `race-b` was therefore
wrongly accepted for a write targeting `race-a`.

Every test below is parametrized over BOTH `InMemoryRaceStateRepository`
and `DynamoDBRaceStateRepository` (moto-backed), and exercises BOTH
`save_state_from_simulator` and `save_control_from_simulator` — the exact
requirement this repair pass's verification spec calls out.
"""

from __future__ import annotations

import pytest

from app.domain.models import (
    DemoControlState,
    DriverState,
    RaceState,
    SimulatorStatus,
    TireCompound,
    TrackState,
    utc_now,
)
from app.repositories.base import StaleWriteError
from app.repositories.lease import InMemoryLeaseRepository, LeaseProof, simulator_lease_name
from app.repositories.memory import InMemoryRaceStateRepository

# Spec v1.1 pass 3, correction 10: moto-simulated AWS behavior for the
# dynamodb-parametrized half of these tests.
pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

RACE_A = "race-a"
RACE_B = "race-b"
TABLE_NAME = "pit-wall-cross-race-fencing-test"


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


def _state(race_id: str, run_id: str, version: int) -> RaceState:
    return RaceState(
        race_id=race_id,
        run_id=run_id,
        state_version=version,
        lap=24 + version,
        total_laps=58,
        race_time_ms=1000 * version,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id="scenario-demo-01",
        synthetic=True,
        drivers=(_driver(),),
    )


def _control(race_id: str, run_id: str, version: int) -> DemoControlState:
    return DemoControlState(
        race_id=race_id,
        run_id=run_id,
        status=SimulatorStatus.RUNNING,
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


def _memory_pair():
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    return repository, lease_repository


def _dynamodb_pair():
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository

    repository = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repository = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    return repository, lease_repository


REPO_KINDS = ["memory", "dynamodb"]


@pytest.fixture(params=REPO_KINDS)
def repo_and_lease(request):
    if request.param == "dynamodb":
        request.getfixturevalue("dynamodb_table")
        return _dynamodb_pair()
    return _memory_pair()


async def _bootstrap(repository, race_id: str, run_id: str) -> None:
    await repository.begin_new_run(race_id, _state(race_id, run_id, 0), _control(race_id, run_id, 0))


# 1. Baseline sanity: a proof legitimately acquired for the SAME race is
#    accepted for both state and control writes.
async def test_same_race_proof_accepted(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id = "run-same-race"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=30.0)
    proof = LeaseProof.from_grant(lease_name, grant)

    await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=proof)
    await repository.save_control_from_simulator(_control(RACE_A, run_id, 1), lease_proof=proof)

    stored_state = await repository.get_state(RACE_A)
    stored_control = await repository.get_control(RACE_A)
    assert stored_state.state_version == 1
    assert stored_control.state_version == 1


# 2. THE actual exploit reproduction: a proof legitimately acquired for a
#    DIFFERENT race must never authorize a write to this race.
async def test_different_race_proof_rejected_for_state(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id_a = "run-a"
    run_id_b = "run-b"
    await _bootstrap(repository, RACE_A, run_id_a)
    await _bootstrap(repository, RACE_B, run_id_b)

    lease_name_b = simulator_lease_name(RACE_B)
    grant_b = await lease_repository.try_acquire_or_renew(lease_name_b, RACE_B, "owner-b", ttl_seconds=30.0)
    proof_for_b = LeaseProof.from_grant(lease_name_b, grant_b)

    # `proof_for_b` is entirely genuine and internally self-consistent
    # (its own race_id matches its own lease_name) — it is simply for the
    # WRONG race relative to this write's target, race A.
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id_a, 1), lease_proof=proof_for_b)

    stored = await repository.get_state(RACE_A)
    assert stored.state_version == 0  # the cross-race write did not land


async def test_different_race_proof_rejected_for_control(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id_a = "run-a"
    run_id_b = "run-b"
    await _bootstrap(repository, RACE_A, run_id_a)
    await _bootstrap(repository, RACE_B, run_id_b)

    lease_name_b = simulator_lease_name(RACE_B)
    grant_b = await lease_repository.try_acquire_or_renew(lease_name_b, RACE_B, "owner-b", ttl_seconds=30.0)
    proof_for_b = LeaseProof.from_grant(lease_name_b, grant_b)

    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(_control(RACE_A, run_id_a, 1), lease_proof=proof_for_b)

    stored = await repository.get_control(RACE_A)
    assert stored.state_version == 0


# 3. Correct race_id embedded in the proof, but a mismatched lease_name —
#    rejected.
async def test_correct_race_id_wrong_lease_name_rejected(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=30.0)
    forged_proof = LeaseProof(
        lease_name="totally-not-the-simulator-lease-name",
        race_id=RACE_A,
        owner_id=grant.owner_id,
        fencing_token=grant.fencing_token,
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=forged_proof)


# 4. Correct lease_name, but the race_id field itself points elsewhere —
#    rejected (this is the field the original bug never actually checked
#    against the write's real target).
async def test_correct_lease_name_wrong_race_id_field_rejected(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id_a = "run-a"
    run_id_b = "run-b"
    await _bootstrap(repository, RACE_A, run_id_a)
    await _bootstrap(repository, RACE_B, run_id_b)

    # Acquire the REAL lease for race A, but forge a proof claiming (falsely)
    # to be for race B while keeping race A's own lease_name string.
    lease_name_a = simulator_lease_name(RACE_A)
    grant_a = await lease_repository.try_acquire_or_renew(lease_name_a, RACE_A, "owner-a", ttl_seconds=30.0)
    forged_proof = LeaseProof(
        lease_name=lease_name_a, race_id=RACE_B, owner_id=grant_a.owner_id, fencing_token=grant_a.fencing_token
    )
    # Targeting race B with a proof whose lease_name says race A: rejected
    # on the lease_name/race_id mismatch check alone, regardless of what
    # the live lease record for race A actually contains.
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_B, run_id_b, 1), lease_proof=forged_proof)


# 5. Correct race/lease, wrong owner — regression (should already work via
#    the live-lease ConditionCheck/matches_locked check; explicit test).
async def test_wrong_owner_rejected(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=30.0)
    forged_proof = LeaseProof(
        lease_name=lease_name, race_id=RACE_A, owner_id="not-owner-a", fencing_token=grant.fencing_token
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=forged_proof)


# 6. Correct race/lease, wrong token — regression.
async def test_wrong_token_rejected(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=30.0)
    forged_proof = LeaseProof(
        lease_name=lease_name, race_id=RACE_A, owner_id=grant.owner_id, fencing_token=grant.fencing_token + 1
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=forged_proof)


# 7. A fabricated, implausibly large token must still be rejected — it
#    won't match the live lease's actual current token, no matter how
#    large it is.
async def test_fabricated_large_token_rejected(repo_and_lease):
    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=30.0)
    forged_proof = LeaseProof(
        lease_name=lease_name, race_id=RACE_A, owner_id=grant.owner_id, fencing_token=grant.fencing_token + 10_000_000
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=forged_proof)


# 8. Expired proof rejected — regression.
async def test_expired_proof_rejected(repo_and_lease):
    import asyncio

    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=0.3)
    proof = LeaseProof.from_grant(lease_name, grant)
    await asyncio.sleep(1.0)

    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=proof)


# 9. Superseded proof rejected — a newer leader acquired the same
#    lease_name/race_id; the old proof is now stale.
async def test_superseded_proof_rejected(repo_and_lease):
    import asyncio

    repository, lease_repository = repo_and_lease
    run_id = "run-a"
    await _bootstrap(repository, RACE_A, run_id)

    lease_name = simulator_lease_name(RACE_A)
    grant_a = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-a", ttl_seconds=0.3)
    proof_a = LeaseProof.from_grant(lease_name, grant_a)
    await asyncio.sleep(1.0)  # owner-a's lease is now expired

    grant_b = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "owner-b", ttl_seconds=30.0)
    assert grant_b is not None and grant_b.owner_id == "owner-b"

    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=proof_a)

    proof_b = LeaseProof.from_grant(lease_name, grant_b)
    await repository.save_state_from_simulator(_state(RACE_A, run_id, 1), lease_proof=proof_b)


# 10. An existing lease name cannot be rebound to a different race_id, even
#     for the exact same owner_id, even after the original grant expired.
async def test_existing_lease_name_cannot_be_rebound_to_a_different_race(repo_and_lease):
    import asyncio

    _repository, lease_repository = repo_and_lease
    lease_name = "shared-lease-name"

    grant_a = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "same-owner", ttl_seconds=0.3)
    assert grant_a is not None and grant_a.race_id == RACE_A

    # Same owner, same lease_name, but a DIFFERENT race_id — must fail,
    # both while the original grant is still live...
    rebind_attempt_live = await lease_repository.try_acquire_or_renew(
        lease_name, RACE_B, "same-owner", ttl_seconds=30.0
    )
    assert rebind_attempt_live is None

    await asyncio.sleep(1.0)  # ...and after the original grant has expired.
    rebind_attempt_expired = await lease_repository.try_acquire_or_renew(
        lease_name, RACE_B, "same-owner", ttl_seconds=30.0
    )
    assert rebind_attempt_expired is None

    # The lease remains bound to race A throughout.
    current = await lease_repository.get_lease(lease_name)
    assert current is not None
    assert current.race_id == RACE_A

    # A renewal for the SAME race_id, same owner, still works normally.
    renewal = await lease_repository.try_acquire_or_renew(lease_name, RACE_A, "same-owner", ttl_seconds=30.0)
    assert renewal is not None
    assert renewal.race_id == RACE_A
