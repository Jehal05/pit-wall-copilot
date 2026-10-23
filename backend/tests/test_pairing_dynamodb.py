"""Tests for the durable, DynamoDB-backed device-pairing repositories
(spec v1.1 pass 4, mandatory correction 1).

These exercise real DynamoDB semantics via moto (not just fast in-process
logic), so this file is tagged `integration` like `test_dynamodb_repository.py`
and `test_watch_dynamodb_repository.py` — see tests/conftest.py's
auto-tagging hook.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from app.repositories.pairing_dynamodb import (  # noqa: E402
    DynamoDBDeviceTokenRepository,
    DynamoDBPairingCodeRepository,
    DynamoDBPairingExchangeCoordinator,
    DynamoDBPairingRateLimiter,
    DynamoDBPairingRateLimiterExceeded,
)
from app.repositories.pairing_base import PairingExchangeOutcome  # noqa: E402
from app.services.device_pairing import DevicePairingService  # noqa: E402

TABLE_NAME = "pit-wall-race-state-pairing-test"


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


def _service() -> DevicePairingService:
    return DevicePairingService(
        DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1"),
    )


# 1. A backend "restart" does not invalidate a paired device: a second,
#    independently-constructed service instance (standing in for a second
#    process after a redeploy) reads the SAME durable table and accepts
#    the token the first instance minted.
def test_device_token_survives_a_simulated_backend_restart(dynamodb_table):
    first_process = _service()
    issued = first_process.generate_pairing_code()
    paired = first_process.exchange_pairing_code(issued.code, "Living Room Fire TV")
    assert paired is not None

    # Simulate a restart: throw away the first service/process entirely and
    # build a brand-new one against the same table, as a fresh ECS task
    # after a deploy would.
    del first_process
    second_process = _service()

    assert second_process.verify_device_token(paired.device_token) is True


# 2. Two "backend instances" (two independently-constructed services
#    against the same table) agree on verification and revocation.
def test_two_backend_instances_agree_on_verification_and_revocation(dynamodb_table):
    instance_a = _service()
    instance_b = _service()

    issued = instance_a.generate_pairing_code()
    paired = instance_a.exchange_pairing_code(issued.code, "device")
    assert paired is not None

    # Instance B, which never generated the code or performed the
    # exchange, sees the SAME device as valid -- proving pairing state is
    # shared, not process-local.
    assert instance_b.verify_device_token(paired.device_token) is True

    # Revoking through instance B is immediately visible to instance A.
    assert instance_b.revoke_device_token(paired.device_token) is True
    assert instance_a.verify_device_token(paired.device_token) is False


# 3. A pairing code cannot be consumed twice, even when the two "requests"
#    are handled by two independently-constructed service instances
#    (simulating two ECS tasks) both attempting to exchange the SAME code.
#
#    Repair pass (mandatory correction 3): this used to fire the two
#    exchanges from separate `threading.Thread`s racing each other against
#    the SAME moto-mocked backend. That is a flaky test DESIGN, not a
#    flaky assertion: moto's DynamoDB backend is not internally
#    thread-safe for concurrent `transact_write_items` calls from two
#    Python threads (observed directly while diagnosing this -- see
#    `TestAtomicPairingExchangeConcurrencyViaStubber`'s class docstring
#    below for the full reproduction). Retrying, skipping, or serializing
#    the production `DynamoDBPairingExchangeCoordinator` itself would all
#    either hide a spurious failure or weaken a real guarantee for the
#    wrong reason -- so this test is redesigned instead of patched:
#
#      - This test now drives the two "requests" SEQUENTIALLY (no
#        threads), against a real moto-backed table, at the SERVICE
#        layer (`DevicePairingService.exchange_pairing_code`) -- proving
#        the DURABLE, cross-instance guarantee ("two independently
#        constructed services against the same table agree a code is
#        single-use") without depending on moto's thread-safety at all.
#      - `TestAtomicPairingExchangeConcurrencyViaStubber` below covers the
#        genuinely CONCURRENT race outcome (the losing writer's request
#        actually overlapping the winner's, exactly as two real ECS tasks
#        racing in production would) deterministically, via
#        `botocore.stub.Stubber`, with the exact request/response shapes
#        DynamoDB's own TransactWriteItems race produces -- rather than
#        hoping two real threads happen to interleave the way the test
#        wants inside a mock backend never designed for that.
def test_a_second_instance_cannot_reuse_a_code_already_exchanged_by_the_first(dynamodb_table):
    instance_a = _service()
    instance_b = _service()
    issued = instance_a.generate_pairing_code()

    first_result = instance_a.exchange_pairing_code(issued.code, "device-a")
    second_result = instance_b.exchange_pairing_code(issued.code, "device-b")

    assert first_result is not None
    assert second_result is None


# 4. Device-token expiry is honored durably (a fresh instance still
#    rejects an expired token; it is not relying on any in-process cache).
def test_device_token_expiry_is_honored_by_a_fresh_instance(dynamodb_table):
    clock = {"now": 1000.0}
    first_process = DevicePairingService(
        DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1"),
        clock=lambda: clock["now"],
    )
    issued = first_process.generate_pairing_code()
    paired = first_process.exchange_pairing_code(issued.code, "device")
    assert paired is not None

    clock["now"] += 31 * 24 * 3600  # past the 30-day device token TTL

    second_process = DevicePairingService(
        DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1"),
        clock=lambda: clock["now"],
    )
    assert second_process.verify_device_token(paired.device_token) is False


# 5. Independent revocation: revoking one device by device_id does not
#    affect another device, durably.
def test_independent_revocation_by_device_id_across_instances(dynamodb_table):
    instance_a = _service()
    instance_b = _service()

    device_a = instance_a.exchange_pairing_code(instance_a.generate_pairing_code().code, "device-a")
    device_b = instance_a.exchange_pairing_code(instance_a.generate_pairing_code().code, "device-b")
    assert device_a is not None and device_b is not None

    assert instance_b.revoke_device_by_id(device_a.device_id) is True

    assert instance_a.verify_device_token(device_a.device_token) is False
    assert instance_a.verify_device_token(device_b.device_token) is True


# 6. A DynamoDB conditional failure on re-consuming an already-used code
#    surfaces as a clean `False`/`None`, not an unhandled exception, when
#    exercised directly at the repository level (below the service).
def test_repository_level_conditional_failure_on_reused_code(dynamodb_table):
    repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
    now = time.time()
    repository.create_code(code_hash="abc123", created_at=now, expires_at=now + 300)

    assert repository.try_consume_code("abc123", now) is True
    # Second consumption of the SAME code hash fails cleanly.
    assert repository.try_consume_code("abc123", now) is False


def test_repository_level_conditional_failure_on_expired_code(dynamodb_table):
    repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
    now = time.time()
    repository.create_code(code_hash="def456", created_at=now, expires_at=now + 1)

    assert repository.try_consume_code("def456", now + 1000) is False


def test_repository_level_conditional_failure_on_unknown_code(dynamodb_table):
    repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
    assert repository.try_consume_code("never-issued", time.time()) is False


# 7. TTL attributes are present on both item types (application-level
#    rejection is still what actually enforces expiry -- see the module
#    docstring -- but the attribute must exist for DynamoDB TTL to ever
#    physically clean the table up).
def test_ttl_attribute_is_present_on_pairing_code_and_device_token_items(dynamodb_table):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)
    pairing_code_repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
    device_token_repository = DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1")
    now = time.time()

    pairing_code_repository.create_code(code_hash="ttltest", created_at=now, expires_at=now + 300)
    device_token_repository.create_token(
        device_id="dev-1", token_hash="tokhash", device_name="x", created_at=now, expires_at=now + 300
    )

    code_item = table.get_item(Key={"pk": "AUTH", "sk": "PAIRCODE#ttltest"})["Item"]
    token_item = table.get_item(Key={"pk": "AUTH", "sk": "DEVICETOKEN#tokhash"})["Item"]
    assert "ttl" in code_item
    assert "ttl" in token_item


# 8. Expired-but-not-yet-TTL-deleted records are still rejected by
#    application logic (moto does not physically expire items on its own,
#    which is exactly the scenario this proves is safe either way).
def test_expired_but_not_physically_deleted_code_is_still_rejected(dynamodb_table):
    repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
    now = time.time()
    repository.create_code(code_hash="stillthere", created_at=now, expires_at=now + 1)
    # The item still physically exists (moto never runs a TTL sweep), but
    # is correctly rejected as expired by the conditional consume itself.
    assert repository.try_consume_code("stillthere", now + 3600) is False


def test_device_id_index_item_written_atomically_with_token(dynamodb_table):
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)
    repository = DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1")
    now = time.time()
    repository.create_token(
        device_id="dev-42", token_hash="tokhash42", device_name="x", created_at=now, expires_at=now + 300
    )
    index_item = table.get_item(Key={"pk": "AUTH", "sk": "DEVICEIDX#dev-42"})["Item"]
    assert index_item["token_hash"] == "tokhash42"


# AWS hackathon-final pass, P1-11: collision-safe pairing-code storage.
# `create_code` must not silently clobber a still-active code sharing the
# same hash (i.e. the same 6-digit code value), but must allow recycling
# a hash that belongs to an expired or already-used code.
class TestCollisionSafePairingCodeCreation:
    def test_create_code_returns_true_for_a_brand_new_hash(self, dynamodb_table):
        repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
        now = time.time()
        assert repository.create_code(code_hash="fresh-hash", created_at=now, expires_at=now + 300) is True

    def test_create_code_returns_false_when_colliding_with_a_still_active_code(self, dynamodb_table):
        repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
        now = time.time()
        assert repository.create_code(code_hash="collide-me", created_at=now, expires_at=now + 300) is True

        # A second, independent pairing session happens to draw the SAME
        # 6-digit code (i.e. the same hash) while the first is still
        # active (unused, unexpired) -- this must be refused, not
        # silently overwrite the first session's still-live code.
        assert repository.create_code(code_hash="collide-me", created_at=now + 1, expires_at=now + 301) is False

        # And the ORIGINAL code must still be exactly as it was --
        # unaffected by the refused collision attempt.
        assert repository.try_consume_code("collide-me", now + 1) is True

    def test_create_code_recycles_a_hash_belonging_to_an_expired_code(self, dynamodb_table):
        repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
        now = time.time()
        assert repository.create_code(code_hash="recycle-me", created_at=now, expires_at=now + 1) is True

        # Far past the first code's expiry -- safe to recycle the same
        # hash for a brand-new code.
        later = now + 3600
        assert repository.create_code(code_hash="recycle-me", created_at=later, expires_at=later + 300) is True
        # The recycled code is now the currently-valid one.
        assert repository.try_consume_code("recycle-me", later) is True

    def test_create_code_recycles_a_hash_belonging_to_an_already_used_code(self, dynamodb_table):
        repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
        now = time.time()
        assert repository.create_code(code_hash="used-then-reused", created_at=now, expires_at=now + 300) is True
        assert repository.try_consume_code("used-then-reused", now) is True

        # The code was consumed (used=True) -- safe to recycle its hash
        # for a fresh code even though it hasn't technically expired yet.
        assert repository.create_code(code_hash="used-then-reused", created_at=now + 1, expires_at=now + 301) is True
        assert repository.try_consume_code("used-then-reused", now + 1) is True


class TestDynamoDBPairingRateLimiter:
    def test_blocks_after_the_configured_number_of_attempts(self, dynamodb_table):
        limiter = DynamoDBPairingRateLimiter(
            table_name=TABLE_NAME, region_name="us-east-1", max_attempts=3, window_seconds=60.0
        )
        for _ in range(3):
            limiter.check_and_record("1.2.3.4")
        with pytest.raises(DynamoDBPairingRateLimiterExceeded):
            limiter.check_and_record("1.2.3.4")

    def test_scoped_per_key(self, dynamodb_table):
        limiter = DynamoDBPairingRateLimiter(
            table_name=TABLE_NAME, region_name="us-east-1", max_attempts=1, window_seconds=60.0
        )
        limiter.check_and_record("1.2.3.4")
        limiter.check_and_record("5.6.7.8")  # different source — not blocked

    def test_shared_across_two_limiter_instances_against_the_same_table(self, dynamodb_table):
        """Proves the counter is durable/shared storage, not
        process-local: two independently-constructed limiter objects
        (standing in for two ECS tasks) share the same budget."""
        limiter_a = DynamoDBPairingRateLimiter(
            table_name=TABLE_NAME, region_name="us-east-1", max_attempts=2, window_seconds=60.0
        )
        limiter_b = DynamoDBPairingRateLimiter(
            table_name=TABLE_NAME, region_name="us-east-1", max_attempts=2, window_seconds=60.0
        )
        limiter_a.check_and_record("9.9.9.9")
        limiter_b.check_and_record("9.9.9.9")
        with pytest.raises(DynamoDBPairingRateLimiterExceeded):
            limiter_a.check_and_record("9.9.9.9")


# AWS hackathon-final pass, phase 3: fully transactional pairing exchange.
# These exercise `DynamoDBPairingExchangeCoordinator.atomic_exchange`
# directly (not through `DevicePairingService`) so each scenario can be
# driven precisely, against moto's real DynamoDB TransactWriteItems
# semantics rather than the in-memory stand-in.
class TestAtomicPairingExchange:
    def _issue_code(self, code_hash: str = "atomic-code-hash") -> tuple[DynamoDBPairingCodeRepository, float]:
        repository = DynamoDBPairingCodeRepository(table_name=TABLE_NAME, region_name="us-east-1")
        now = time.time()
        assert repository.create_code(code_hash=code_hash, created_at=now, expires_at=now + 300) is True
        return repository, now

    def test_a_simulated_token_write_failure_leaves_the_code_usable(self, dynamodb_table):
        """The exact bug this phase fixes: pre-seed a DEVICETOKEN# item
        that collides with the token_hash this exchange will try to
        write (standing in for "the token-creation half of the old
        two-step flow failed"), then attempt the atomic exchange. The
        whole transaction must be rejected -- AND, critically, the
        pairing code must NOT have been consumed: a caller retrying with
        a fresh token_hash against the SAME code must still succeed."""
        code_repository, now = self._issue_code()
        device_token_repository = DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1")
        device_token_repository.create_token(
            device_id="pre-existing-device", token_hash="colliding-token-hash",
            device_name="someone else", created_at=now, expires_at=now + 300,
        )

        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")
        outcome = coordinator.atomic_exchange(
            code_hash="atomic-code-hash", now=now, device_id="new-device",
            token_hash="colliding-token-hash", device_name="new device", token_expires_at=now + 300,
        )
        assert outcome is PairingExchangeOutcome.TOKEN_HASH_COLLISION

        # The code was NOT burned by the failed attempt -- a retry with a
        # fresh, non-colliding token_hash succeeds.
        assert code_repository.try_consume_code("atomic-code-hash", now) is True

    def test_a_device_id_collision_is_rejected_without_consuming_the_code(self, dynamodb_table):
        code_repository, now = self._issue_code("device-id-collision-code")
        device_token_repository = DynamoDBDeviceTokenRepository(table_name=TABLE_NAME, region_name="us-east-1")
        device_token_repository.create_token(
            device_id="already-taken-device-id", token_hash="some-other-hash",
            device_name="someone else", created_at=now, expires_at=now + 300,
        )

        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")
        outcome = coordinator.atomic_exchange(
            code_hash="device-id-collision-code", now=now, device_id="already-taken-device-id",
            token_hash="brand-new-hash", device_name="new device", token_expires_at=now + 300,
        )
        assert outcome is PairingExchangeOutcome.DEVICE_ID_COLLISION
        assert code_repository.try_consume_code("device-id-collision-code", now) is True

    def test_an_expired_or_used_code_is_rejected_and_no_token_or_index_record_is_created(self, dynamodb_table):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)
        _code_repository, now = self._issue_code("expired-code-hash")

        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")
        outcome = coordinator.atomic_exchange(
            code_hash="expired-code-hash", now=now + 10_000,  # long past the 5-minute expiry
            device_id="orphan-device", token_hash="orphan-token-hash",
            device_name="x", token_expires_at=now + 20_000,
        )
        assert outcome is PairingExchangeOutcome.CODE_REJECTED

        # No orphan token or index record was created by the rejected attempt.
        assert "Item" not in table.get_item(Key={"pk": "AUTH", "sk": "DEVICETOKEN#orphan-token-hash"})
        assert "Item" not in table.get_item(Key={"pk": "AUTH", "sk": "DEVICEIDX#orphan-device"})

    def test_a_successful_exchange_writes_all_three_records_with_ttl_atomically(self, dynamodb_table):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)
        code_repository, now = self._issue_code("full-success-code")

        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")
        outcome = coordinator.atomic_exchange(
            code_hash="full-success-code", now=now, device_id="full-success-device",
            token_hash="full-success-token-hash", device_name="Living Room Fire TV",
            token_expires_at=now + 300,
        )
        assert outcome is PairingExchangeOutcome.SUCCESS

        code_item = table.get_item(Key={"pk": "AUTH", "sk": "PAIRCODE#full-success-code"})["Item"]
        token_item = table.get_item(Key={"pk": "AUTH", "sk": "DEVICETOKEN#full-success-token-hash"})["Item"]
        index_item = table.get_item(Key={"pk": "AUTH", "sk": "DEVICEIDX#full-success-device"})["Item"]
        assert code_item["used"] is True
        assert "ttl" in code_item
        assert "ttl" in token_item
        assert "ttl" in index_item
        assert index_item["token_hash"] == "full-success-token-hash"

        # The code really was consumed this time -- a second attempt with
        # the same code fails outright.
        assert code_repository.try_consume_code("full-success-code", now) is False

    def test_a_second_sequential_attempt_against_an_already_consumed_code_is_rejected(self, dynamodb_table):
        """Direct proof at the coordinator level (complementing the
        service-level `test_a_second_instance_cannot_reuse_a_code_already_
        exchanged_by_the_first` above) that the single-transaction design
        itself -- not just `DevicePairingService`'s own retry logic -- is
        what prevents double-consumption: a second `atomic_exchange` call
        against a code the first call already consumed is rejected purely
        by DynamoDB's own conditional check on the PAIRCODE# item, with no
        application-level "already used" bookkeeping involved at all.

        Repair pass (mandatory correction 3): the real, genuinely
        CONCURRENT version of this scenario -- two calls actually
        overlapping in time, which is what a real race between two ECS
        tasks looks like -- is covered deterministically by
        `TestAtomicPairingExchangeConcurrencyViaStubber` below instead of
        by two `threading.Thread`s racing against moto here (see that
        class's docstring for why: moto's backend is not thread-safe for
        concurrent `transact_write_items` calls, which made the old
        version of this test flaky through no fault of the production
        code)."""
        _code_repository, now = self._issue_code("race-code-hash")
        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")

        first = coordinator.atomic_exchange(
            code_hash="race-code-hash", now=now, device_id="device-a",
            token_hash="token-a", device_name="x", token_expires_at=now + 300,
        )
        second = coordinator.atomic_exchange(
            code_hash="race-code-hash", now=now, device_id="device-b",
            token_hash="token-b", device_name="x", token_expires_at=now + 300,
        )

        assert first is PairingExchangeOutcome.SUCCESS
        assert second is PairingExchangeOutcome.CODE_REJECTED


# AWS hackathon-final pass, repair pass (mandatory correction 3): a
# deterministic, non-flaky replacement for what used to be two
# THREADING-based concurrency tests in this file (a service-level one and
# a coordinator-level one, both named to describe "concurrent" exchange
# attempts racing over real `threading.Thread`s against a moto-mocked
# DynamoDB backend).
#
# Root cause of the flake: moto's DynamoDB backend is not internally
# thread-safe for concurrent `transact_write_items` calls made from
# separate Python threads against the SAME mocked backend. Diagnosing this
# directly (running the old test file's full suite repeatedly) produced
# 11 clean consecutive passes and then a hang that exceeded a 2-minute
# timeout with no further output -- consistent with two threads
# deadlocking or corrupting moto's own internal state (a documented class
# of moto issue, not a bug in this project's code), rather than any
# property of `DynamoDBPairingExchangeCoordinator.atomic_exchange` itself
# being unsafe. Retrying the test, ignoring a thread's exception, marking
# it flaky/skipping it, or serializing the *production* implementation
# would each either hide a spurious failure or weaken a real guarantee for
# the wrong reason -- none of those are used here.
#
# The fix: `botocore.stub.Stubber` replaces the two racing threads with
# two SEQUENTIAL calls against a hand-specified, fully deterministic pair
# of request/response exchanges that reproduce EXACTLY what two real
# concurrent `TransactWriteItems` calls racing for the same pairing code
# produce from DynamoDB's own point of view: the winning call's request is
# stubbed to succeed; the losing call (same code_hash) is stubbed with the
# precise `TransactionCanceledException` / `CancellationReasons` shape
# DynamoDB itself returns when the PAIRCODE# item's ConditionCheck (index
# 0 -- see `atomic_exchange`'s own docstring for why the transact items
# are ordered exactly this way) is what failed. This exercises the
# identical request-building/response-parsing code path a real race would
# take, asserts the exact transaction shape via `expected_params` rather
# than merely inferring it from the outcome, and depends on no thread
# scheduling or moto internals whatsoever -- so it is exactly as
# deterministic running once as running a thousand times.
class TestAtomicPairingExchangeConcurrencyViaStubber:
    def _stubbed_coordinator(self) -> "tuple[DynamoDBPairingExchangeCoordinator, object]":
        from botocore.stub import Stubber

        coordinator = DynamoDBPairingExchangeCoordinator(table_name=TABLE_NAME, region_name="us-east-1")
        stubber = Stubber(coordinator._client)  # noqa: SLF001 - the whole point: inject a deterministic transport
        stubber.activate()
        return coordinator, stubber

    def test_exactly_one_exchange_succeeds_when_the_second_loses_the_paircode_race(self):
        """The core property mandatory correction 3 requires: exactly one
        exchange succeeds; a code cannot be consumed twice. The second
        call's stubbed response is the EXACT shape DynamoDB returns when
        another writer's `TransactWriteItems` has already flipped
        `used = true` on the PAIRCODE# item a moment earlier -- the
        losing side of a real race, reproduced deterministically."""
        coordinator, stubber = self._stubbed_coordinator()
        stubber.add_response("transact_write_items", {})  # the winner: unconditional success
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message=(
                "Transaction cancelled, please refer cancellation reasons for specific "
                "reasons [ConditionalCheckFailed, None, None]"
            ),
            http_status_code=400,
            modeled_fields={
                "CancellationReasons": [
                    {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"},
                    {"Code": "None"},
                    {"Code": "None"},
                ]
            },
        )

        winner = coordinator.atomic_exchange(
            code_hash="race-code-hash", now=1000.0, device_id="device-a",
            token_hash="token-a", device_name="x", token_expires_at=1300.0,
        )
        loser = coordinator.atomic_exchange(
            code_hash="race-code-hash", now=1000.0, device_id="device-b",
            token_hash="token-b", device_name="x", token_expires_at=1300.0,
        )

        assert winner is PairingExchangeOutcome.SUCCESS
        assert loser is PairingExchangeOutcome.CODE_REJECTED
        stubber.assert_no_pending_responses()

    def test_token_or_device_id_collisions_do_not_burn_the_code_even_under_the_stubbed_race(self):
        """A DEVICE_ID_COLLISION/TOKEN_HASH_COLLISION outcome (indices 1
        or 2 failing instead of index 0) must be distinguishable from a
        genuinely consumed code -- the code consumption/token
        creation/device-index creation triple is atomic, so a collision on
        either of the latter two never burns the code, and the caller can
        safely retry with fresh random values."""
        coordinator, stubber = self._stubbed_coordinator()
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message=(
                "Transaction cancelled, please refer cancellation reasons for specific "
                "reasons [None, ConditionalCheckFailed, None]"
            ),
            http_status_code=400,
            modeled_fields={
                "CancellationReasons": [
                    {"Code": "None"},
                    {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"},
                    {"Code": "None"},
                ]
            },
        )

        outcome = coordinator.atomic_exchange(
            code_hash="race-code-hash", now=1000.0, device_id="device-a",
            token_hash="colliding-token-hash", device_name="x", token_expires_at=1300.0,
        )

        assert outcome is PairingExchangeOutcome.TOKEN_HASH_COLLISION
        stubber.assert_no_pending_responses()

    def test_an_unexpected_transaction_failure_is_not_misreported_as_an_invalid_code(self):
        """Mandatory correction 3: 'unexpected transaction failures are
        not misreported as invalid codes.' A `ClientError` whose code is
        NOT `TransactionCanceledException` at all (e.g. a throttling or
        capacity error, or any other AWS-side failure unrelated to a
        losing conditional check) must propagate as a genuine exception,
        never get silently swallowed into a `CODE_REJECTED`/`None`
        outcome the caller would misinterpret as 'this code was invalid
        or already used.'"""
        coordinator, stubber = self._stubbed_coordinator()
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="ProvisionedThroughputExceededException",
            service_message="The level of configured provisioned throughput was exceeded.",
            http_status_code=400,
        )

        with pytest.raises(ClientError) as exc_info:
            coordinator.atomic_exchange(
                code_hash="race-code-hash", now=1000.0, device_id="device-a",
                token_hash="token-a", device_name="x", token_expires_at=1300.0,
            )
        assert exc_info.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"
        stubber.assert_no_pending_responses()

    def test_the_exact_transaction_shape_the_second_writer_submits_is_asserted(self):
        """Explicit verification of the exact transaction structure this
        module's own docstring specifies (item order paircode=0,
        token=1, deviceidx=2; the paircode item's ConditionExpression),
        via `expected_params` -- a real assertion Stubber enforces on the
        outgoing request itself, not an inference drawn from the
        response."""
        from botocore.stub import ANY

        coordinator, stubber = self._stubbed_coordinator()
        stubber.add_response(
            "transact_write_items",
            {},
            expected_params={
                "TransactItems": [
                    {
                        "Update": {
                            "TableName": TABLE_NAME,
                            "Key": {"pk": {"S": "AUTH"}, "sk": {"S": "PAIRCODE#shape-check-hash"}},
                            "UpdateExpression": "SET used = :true",
                            "ConditionExpression": "attribute_exists(pk) AND used = :false AND expires_at > :now",
                            "ExpressionAttributeValues": ANY,
                        }
                    },
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": ANY,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": ANY,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            },
        )

        outcome = coordinator.atomic_exchange(
            code_hash="shape-check-hash", now=1000.0, device_id="device-shape",
            token_hash="token-shape", device_name="x", token_expires_at=1300.0,
        )
        assert outcome is PairingExchangeOutcome.SUCCESS
        stubber.assert_no_pending_responses()
