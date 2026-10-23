from __future__ import annotations

import pytest


# Spec v1.1 pass 3, correction 10: exercises moto-simulated AWS (DynamoDB)
# behavior, not just fast in-process logic -- tagged `integration` rather
# than the `unit` default (see tests/conftest.py's auto-tagging hook).
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

TABLE_NAME = "pit-wall-race-state-test"


def _make_driver(driver_id: str) -> DriverState:
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


def _make_state(run_id: str, version: int, lap: int) -> RaceState:
    return RaceState(
        race_id="demo-race-001",
        run_id=run_id,
        state_version=version,
        lap=lap,
        total_laps=58,
        race_time_ms=1000,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id="scenario-demo-01",
        synthetic=True,
        drivers=(_make_driver("CAR_03"),),
    )


def _make_control(run_id: str, version: int) -> DemoControlState:
    return DemoControlState(
        race_id="demo-race-001",
        run_id=run_id,
        status=SimulatorStatus.RUNNING,
        scenario_id="scenario-demo-01",
        current_lap=1,
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


# 12. An older DynamoDB state version cannot replace a newer version.
async def test_dynamodb_rejects_older_state_version(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")

    run_id = "run-abc"
    newer = _make_state(run_id, version=5, lap=27)
    await repo.save_state_from_simulator(newer)

    older = _make_state(run_id, version=3, lap=25)
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(older)

    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 5
    assert stored.lap == 27


async def test_dynamodb_accepts_strictly_increasing_versions(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")

    run_id = "run-xyz"
    await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24))
    await repo.save_state_from_simulator(_make_state(run_id, version=1, lap=25))

    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 1
    assert stored.lap == 25


# AWS hackathon-final pass, repair pass: persistence-level lease-proof
# fencing, enforced through the REAL (moto-simulated) DynamoDB
# TransactWriteItems ConditionCheck against the authoritative LEASE item —
# not just the in-memory repository's dict-based stand-in
# (tests/test_simulator_leadership.py covers that one). These tests share
# ONE moto table between a `DynamoDBRaceStateRepository` and a
# `DynamoDBLeaseRepository`, exactly as production does, and drive lease
# acquisition through the real `try_acquire_or_renew` — never a fabricated
# `LeaseProof` — so they exercise the exact-match/liveness ConditionCheck
# this module's docstring describes, not a stand-in for it.


def _lease_proof_from_repo(lease_repository, lease_name: str, grant):
    from app.repositories.lease import LeaseProof

    return LeaseProof.from_grant(lease_name, grant)


async def test_dynamodb_old_leader_rejected_after_new_leader_acquires_lease_before_writing(dynamodb_table):
    """The exact attack sequence mandatory correction 2 describes: instance
    A holds token 5, pauses past lease expiry, instance B acquires token 6
    but has NOT yet written race state, then A resumes and submits its
    captured (now-stale) proof. A's write must be rejected even though no
    successor write has landed yet — the live LEASE item, not write
    history, is what's authoritative."""
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-fenced"

    # A generous TTL for the initial grant+write so this doesn't race
    # moto's own request latency under a full-suite/CPU-contended run —
    # only the SECOND grant (instance-b, below) needs to observe
    # instance-a's lease as already expired, which the sleep past this
    # TTL guarantees regardless of how long the write itself took.
    grant_a = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=0.3)
    proof_a = _lease_proof_from_repo(lease_repo, lease_name, grant_a)
    await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24), lease_proof=proof_a)

    import asyncio

    await asyncio.sleep(1.0)  # instance A's lease is now expired

    grant_b = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-b", ttl_seconds=10.0)
    assert grant_b is not None and grant_b.owner_id == "instance-b"
    # Instance B has NOT written any race state yet.

    # Instance A resumes and submits its stale, captured proof. Even
    # though state_version 1 > 0 (would pass the version check alone),
    # the LEASE ConditionCheck fails because the live lease now names B.
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state(run_id, version=1, lap=25), lease_proof=proof_a)

    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 0  # the stale write did not land

    # The real current leader's write succeeds.
    proof_b = _lease_proof_from_repo(lease_repo, lease_name, grant_b)
    await repo.save_state_from_simulator(_make_state(run_id, version=1, lap=25), lease_proof=proof_b)
    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 1


def _force_lease_expiry_in_the_past(lease_repository, lease_name: str, *, seconds_ago: float = 3600.0) -> None:
    """Repair pass (deterministic-clock rewrite): directly overwrites the
    stored `expires_at_epoch` attribute of an already-acquired lease item,
    through the SAME `self._table` the repository itself uses, to a value
    far in the past — deterministically simulating "this lease is now
    expired" without any wall-clock sleep and without touching production
    code's own `time.time()`-based expiry logic at all.

    This is a direct, low-level `update_item` against the real (moto-
    simulated) table — it does not call any lease-acquisition method, so
    it cannot itself race against or be confused with a legitimate
    acquire/renew. The subsequent read that evaluates
    `expires_at_epoch > :now` inside `_save_versioned_item`'s
    ConditionCheck (see dynamodb.py) still computes `:now` from the REAL
    `time.time()` at that call's own moment — exactly as production does.
    Setting `expires_at_epoch` to `real_now - seconds_ago` guarantees
    `expires_at_epoch > :now` evaluates to `False` regardless of exactly
    how much real wall-clock time this test process takes to reach that
    line, which is what makes this deterministic under CPU contention
    (a parallel full-suite run) where a short real sleep is not: no
    interval, however small, has to have "actually elapsed" for the
    condition to be exercised — the stored value is already unambiguously
    in the past the instant it is written.
    """
    import time
    from decimal import Decimal

    lease_repository._table.update_item(
        Key=lease_repository.lease_key(lease_name),
        UpdateExpression="SET expires_at_epoch = :expired",
        ExpressionAttributeValues={":expired": Decimal(str(time.time() - seconds_ago))},
    )


async def test_dynamodb_expired_lease_rejected_even_when_no_successor_has_written(dynamodb_table):
    """An expired lease is not live, regardless of whether anyone else has
    claimed it yet — `expires_at_epoch > :now` must fail on its own.

    Repair pass: rewritten to a deterministic controlled-clock design.
    The prior version relied on a real `ttl_seconds=0.3` lease plus
    `asyncio.sleep(1.0)`, which can flake under a CPU-contended full-suite
    run if the initial acquire+write themselves take long enough to eat
    into that 0.3s budget before the sleep even starts — the lease could
    already be expired (or not yet, depending on scheduling) before this
    test's own logic is what's being exercised. This version instead:
      (1) acquires the lease with a long, generous TTL and writes with it
          while it is genuinely, unambiguously live — proving the "valid
          write succeeds" half of the contract without any race;
      (2) deterministically forces that SAME lease's stored
          `expires_at_epoch` into the past via a direct table write (see
          `_force_lease_expiry_in_the_past` above) — no sleep, no ttl
          racing real elapsed time, no dependency on wall-clock speed;
      (3) proves the second write, reusing the SAME (now provably expired)
          proof, is rejected.
    Production's own expiry check (`expires_at_epoch > :now` in
    dynamodb.py's `_save_versioned_item`, and the equivalent in
    `lease.py`'s `try_acquire_or_renew`) is completely untouched by this
    test — only the STORED value being compared against is set up
    deterministically instead of waited for.
    """
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-expired"

    # (1) Acquire with a long TTL (1 real hour) so the initial write is
    # provably made while the lease is live, with zero risk of racing
    # this test's own execution time.
    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=3600.0)
    proof = _lease_proof_from_repo(lease_repo, lease_name, grant)
    await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24), lease_proof=proof)
    stored_while_valid = await repo.get_state("demo-race-001")
    assert stored_while_valid.state_version == 0  # the valid-lease write landed

    # (2) Deterministically force expiry — no sleep, no timing dependency.
    _force_lease_expiry_in_the_past(lease_repo, lease_name)

    # (3) The SAME captured proof, reused against the now-expired lease,
    # must be rejected — nobody else has claimed the lease; expiry alone
    # is sufficient.
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state(run_id, version=1, lap=25), lease_proof=proof)

    stored_after_expiry = await repo.get_state("demo-race-001")
    assert stored_after_expiry.state_version == 0  # the post-expiry write did not land


async def test_dynamodb_wrong_owner_rejected(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository, LeaseProof

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-wrong-owner"

    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=10.0)
    forged_proof = LeaseProof(
        lease_name=lease_name, race_id="demo-race-001", owner_id="not-instance-a", fencing_token=grant.fencing_token
    )
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24), lease_proof=forged_proof)


async def test_dynamodb_wrong_token_rejected(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository, LeaseProof

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-wrong-token"

    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=10.0)
    forged_proof = LeaseProof(
        lease_name=lease_name, race_id="demo-race-001", owner_id=grant.owner_id, fencing_token=grant.fencing_token + 1000
    )
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24), lease_proof=forged_proof)


async def test_dynamodb_correct_current_owner_and_token_accepted(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-correct"

    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=10.0)
    proof = _lease_proof_from_repo(lease_repo, lease_name, grant)
    await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24), lease_proof=proof)
    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 0


async def test_dynamodb_state_and_control_writes_apply_identical_fencing_rules(dynamodb_table):
    """save_control must enforce the exact same lease-proof check as
    save_state — a stale proof is rejected on control writes too."""
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository, LeaseProof

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"
    run_id = "run-control-fenced"

    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=10.0)
    proof = _lease_proof_from_repo(lease_repo, lease_name, grant)
    await repo.begin_new_run(
        "demo-race-001", _make_state(run_id, version=0, lap=24), _make_control(run_id, version=0)
    )
    await repo.save_control_from_simulator(_make_control(run_id, version=1), lease_proof=proof)

    stale_proof = LeaseProof(
        lease_name=lease_name, race_id="demo-race-001", owner_id="instance-a", fencing_token=grant.fencing_token - 1
    )
    with pytest.raises(StaleWriteError):
        await repo.save_control_from_simulator(_make_control(run_id, version=2), lease_proof=stale_proof)


async def test_dynamodb_reset_to_a_new_run_id_still_rejects_old_run_writes_when_fenced(dynamodb_table):
    from app.repositories.dynamodb import DynamoDBRaceStateRepository
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    lease_name = "simulator:demo-race-001"

    grant = await lease_repo.try_acquire_or_renew(lease_name, "demo-race-001", "instance-a", ttl_seconds=10.0)
    proof = _lease_proof_from_repo(lease_repo, lease_name, grant)

    await repo.begin_new_run(
        "demo-race-001", _make_state("run-1", version=0, lap=24), _make_control("run-1", version=0)
    )
    await repo.save_state_from_simulator(_make_state("run-1", version=1, lap=25), lease_proof=proof)

    await repo.begin_new_run(
        "demo-race-001", _make_state("run-2", version=0, lap=1), _make_control("run-2", version=0)
    )
    # Same still-live lease proof, but targeting the now-superseded run_id.
    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state("run-1", version=2, lap=26), lease_proof=proof)

    # The new run accepts it.
    await repo.save_state_from_simulator(_make_state("run-2", version=1, lap=2), lease_proof=proof)


async def test_dynamodb_unfenced_writes_are_unaffected_when_not_required(dynamodb_table):
    """A caller that never passes lease_proof (lease_proof=None, the
    default) against a repository constructed with require_lease_proof
    left at its default (False) sees exactly the pre-repair-pass
    behavior: no lease condition applied at all. Production
    (factory.py) always sets require_lease_proof=True for
    STATE_REPOSITORY=dynamodb — this test exercises the direct
    construction path other tests/scripts still legitimately use."""
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")
    run_id = "run-unfenced"

    await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24))
    await repo.save_state_from_simulator(_make_state(run_id, version=1, lap=25))
    stored = await repo.get_state("demo-race-001")
    assert stored.state_version == 1


async def test_dynamodb_require_lease_proof_fails_closed_with_no_proof(dynamodb_table):
    """Mandatory correction 2: 'production DynamoDB simulator writes must
    fail closed without a valid lease proof.' Proves the
    require_lease_proof=True path (what factory.py always sets for
    STATE_REPOSITORY=dynamodb) rejects a lease_proof=None write
    immediately, before even attempting a transaction."""
    from app.repositories.dynamodb import DynamoDBRaceStateRepository

    repo = DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1", require_lease_proof=True)
    run_id = "run-fail-closed"

    with pytest.raises(StaleWriteError):
        await repo.save_state_from_simulator(_make_state(run_id, version=0, lap=24))
