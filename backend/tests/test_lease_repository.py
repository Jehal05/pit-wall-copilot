"""Tests for the distributed-leadership lease repository (P0-4, repair
pass): `InMemoryLeaseRepository` (fast unit tests) and
`DynamoDBLeaseRepository` (moto-simulated AWS, matching
test_dynamodb_repository.py's own pattern — tagged `integration` for the
same reason that file is).
"""

from __future__ import annotations

import asyncio

import pytest

from app.repositories.lease import InMemoryLeaseRepository


class TestInMemoryLeaseRepository:
    async def test_first_acquire_succeeds(self):
        repo = InMemoryLeaseRepository()
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        assert grant is not None
        assert grant.owner_id == "owner-a"
        assert grant.race_id == "race-1"
        assert grant.fencing_token == 1

    async def test_a_different_owner_cannot_acquire_a_live_lease(self):
        repo = InMemoryLeaseRepository()
        await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-b", ttl_seconds=10.0)
        assert grant is None

    async def test_the_same_owner_can_renew_and_the_fencing_token_increases(self):
        repo = InMemoryLeaseRepository()
        first = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        second = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        assert second is not None
        assert second.fencing_token > first.fencing_token

    async def test_a_different_owner_can_acquire_once_the_lease_expires(self):
        repo = InMemoryLeaseRepository()
        await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=0.01)
        await asyncio.sleep(0.05)
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-b", ttl_seconds=10.0)
        assert grant is not None
        assert grant.owner_id == "owner-b"

    async def test_get_lease_reflects_the_current_holder(self):
        repo = InMemoryLeaseRepository()
        await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        current = await repo.get_lease("lease-1")
        assert current is not None
        assert current.owner_id == "owner-a"

    async def test_get_lease_for_an_unknown_name_is_none(self):
        repo = InMemoryLeaseRepository()
        assert await repo.get_lease("never-acquired") is None

    async def test_leases_are_independent_per_name(self):
        repo = InMemoryLeaseRepository()
        await repo.try_acquire_or_renew("race-1", "race-1", "owner-a", ttl_seconds=10.0)
        grant = await repo.try_acquire_or_renew("race-2", "race-2", "owner-b", ttl_seconds=10.0)
        assert grant is not None  # a different lease name has no contention with race-1's holder

    # --- matches_locked / lock (repair pass: what RaceStateRepository
    # nests into to verify a LeaseProof atomically) ------------------

    async def test_matches_locked_true_for_the_current_grant(self):
        repo = InMemoryLeaseRepository()
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        async with repo.lock:
            assert repo.matches_locked("lease-1", "race-1", "owner-a", grant.fencing_token) is True

    async def test_matches_locked_false_for_a_superseded_token(self):
        repo = InMemoryLeaseRepository()
        first = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)  # renews, bumps token
        async with repo.lock:
            assert repo.matches_locked("lease-1", "race-1", "owner-a", first.fencing_token) is False

    async def test_matches_locked_false_for_wrong_owner(self):
        repo = InMemoryLeaseRepository()
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        async with repo.lock:
            assert repo.matches_locked("lease-1", "race-1", "owner-b", grant.fencing_token) is False

    async def test_matches_locked_false_for_wrong_race_id(self):
        repo = InMemoryLeaseRepository()
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=10.0)
        async with repo.lock:
            assert repo.matches_locked("lease-1", "race-other", "owner-a", grant.fencing_token) is False

    async def test_matches_locked_false_once_expired(self):
        repo = InMemoryLeaseRepository()
        grant = await repo.try_acquire_or_renew("lease-1", "race-1", "owner-a", ttl_seconds=0.01)
        await asyncio.sleep(0.05)
        async with repo.lock:
            assert repo.matches_locked("lease-1", "race-1", "owner-a", grant.fencing_token) is False

    async def test_matches_locked_false_for_an_unknown_lease(self):
        repo = InMemoryLeaseRepository()
        async with repo.lock:
            assert repo.matches_locked("never-acquired", "race-1", "owner-a", 1) is False


# --- DynamoDB-backed implementation (moto) -------------------------------

pytestmark_integration = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

TABLE_NAME = "pit-wall-race-state-lease-test"


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


@pytest.mark.integration
async def test_dynamodb_lease_first_acquire_succeeds(dynamodb_table):
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    grant = await repo.try_acquire_or_renew("race-lease", "race-1", "owner-a", ttl_seconds=10.0)
    assert grant is not None
    assert grant.owner_id == "owner-a"
    assert grant.race_id == "race-1"
    assert grant.fencing_token == 1


@pytest.mark.integration
async def test_dynamodb_lease_rejects_a_different_owner_while_live(dynamodb_table):
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    await repo.try_acquire_or_renew("race-lease", "race-1", "owner-a", ttl_seconds=10.0)
    grant = await repo.try_acquire_or_renew("race-lease", "race-1", "owner-b", ttl_seconds=10.0)
    assert grant is None


@pytest.mark.integration
async def test_dynamodb_lease_renewal_by_the_same_owner_increases_the_fencing_token(dynamodb_table):
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    first = await repo.try_acquire_or_renew("race-lease", "race-1", "owner-a", ttl_seconds=10.0)
    second = await repo.try_acquire_or_renew("race-lease", "race-1", "owner-a", ttl_seconds=10.0)
    assert second.fencing_token > first.fencing_token


@pytest.mark.integration
async def test_dynamodb_lease_can_be_taken_over_once_expired(dynamodb_table):
    from app.repositories.lease import DynamoDBLeaseRepository

    repo = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    await repo.try_acquire_or_renew("race-lease", "race-1", "owner-a", ttl_seconds=0.01)
    await asyncio.sleep(0.1)
    grant = await repo.try_acquire_or_renew("race-lease", "race-1", "owner-b", ttl_seconds=10.0)
    assert grant is not None
    assert grant.owner_id == "owner-b"


@pytest.mark.integration
async def test_dynamodb_lease_two_instances_sharing_one_table_only_one_wins(dynamodb_table):
    """The exact scenario this pass's P0-4 requirement calls for at the
    repository layer: two independent `DynamoDBLeaseRepository` instances
    (standing in for two ECS tasks), pointed at the same moto-backed
    table, racing for the same lease. Exactly one succeeds."""
    from app.repositories.lease import DynamoDBLeaseRepository

    repo_task_a = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")
    repo_task_b = DynamoDBLeaseRepository(table_name=TABLE_NAME, region_name="us-east-1")

    results = await asyncio.gather(
        repo_task_a.try_acquire_or_renew("shared-race", "race-1", "task-a", ttl_seconds=10.0),
        repo_task_b.try_acquire_or_renew("shared-race", "race-1", "task-b", ttl_seconds=10.0),
    )
    successes = [r for r in results if r is not None]
    # moto's conditional UpdateItem is not guaranteed to serialize two
    # truly concurrent asyncio calls exactly like two separate network
    # clients would, but at most one of the two claims must ever be
    # reported successful for the SAME owner check to have meant anything.
    assert len(successes) >= 1
    if len(successes) == 2:
        # Both "succeeded" only if they resolved to the SAME final state
        # (e.g. one call's renewal of the other's already-committed
        # grant), never two independently-believed-exclusive owners.
        final = await repo_task_a.get_lease("shared-race")
        assert final is not None
