"""DynamoDB-backed pairing/device-token repositories (spec v1.1 pass 4,
mandatory correction 1).

Reuses the SAME table race state and watches/alerts already use
(`DYNAMODB_TABLE_NAME`) — no new table to provision, matching the pattern
`watch_dynamodb.py` already established. Distinguished by a dedicated,
non-race-scoped partition key, since pairing/device-token state is not
associated with any single race:

  Pairing code item:   pk="AUTH"  sk="PAIRCODE#<code_hash>"
    { pk, sk, code_hash, created_at, expires_at, used, ttl }

  Device token item:   pk="AUTH"  sk="DEVICETOKEN#<token_hash>"
    { pk, sk, device_id, token_hash, device_name, created_at, expires_at,
      revoked, ttl }

  Device-id index item: pk="AUTH"  sk="DEVICEIDX#<device_id>"
    { pk, sk, device_id, token_hash }
    A second, small item purely so `revoke_by_device_id` (an admin
    revoking a lost/compromised Fire TV by its device_id, without holding
    that device's own token) can find the right DEVICETOKEN# item without
    a table scan. Written atomically alongside the DEVICETOKEN# item in
    `create_token` (a `TransactWriteItems` call) so the two can never
    diverge.

`ttl` on both item types is the same epoch-seconds value as `expires_at`,
wired up as this table's DynamoDB TTL attribute (see
`infrastructure/template.yaml`'s `TimeToLiveSpecification`) purely for
storage cost cleanup — every read here re-validates `expires_at`/`used`/
`revoked` in application code FIRST, so an item that has expired but not
yet been physically deleted by DynamoDB's (best-effort, not
instantaneous) TTL sweep is still correctly rejected; nothing here ever
depends on TTL deletion for correctness, only for not paying to store
stale rows forever.

*** Atomic single-use code consumption ***
`try_consume_code` uses a conditional `UpdateItem`
(`ConditionExpression="used = :false"`) to flip `used` to `True`. DynamoDB
guarantees a conditional write either fully succeeds or fully fails with
no partial effect, and evaluates the condition against the item's
CURRENT, fully-committed state — so if two ECS tasks receive the same
pairing code in the same millisecond and both call `try_consume_code`
concurrently, DynamoDB serializes the two UpdateItem calls: exactly one
sees `used = :false` still hold and succeeds; the other's condition
check fails against the (now already-flipped) item and it gets
`ConditionalCheckFailedException`, which this method turns into `False`.
This is the same "conditional write is the source of truth, never a
read-then-write race" pattern `DynamoDBRaceStateRepository` and
`DynamoDBWatchRepository.update_watch` already use, applied to pairing
codes.

Only imported when the pairing repository is DynamoDB-backed
(repositories/factory.py), so boto3 stays an optional runtime dependency
for memory-mode development, matching dynamodb.py/watch_dynamodb.py's
existing convention.
"""

from __future__ import annotations

from typing import Any, Optional

from botocore.exceptions import ClientError

from app.repositories.pairing_base import (
    DeviceTokenRepository,
    PairingCodeRepository,
    PairingExchangeCoordinator,
    PairingExchangeOutcome,
    StoredDeviceToken,
)

_PK = "AUTH"


def _paircode_sk(code_hash: str) -> str:
    return f"PAIRCODE#{code_hash}"


def _devicetoken_sk(token_hash: str) -> str:
    return f"DEVICETOKEN#{token_hash}"


def _deviceidx_sk(device_id: str) -> str:
    return f"DEVICEIDX#{device_id}"


class DynamoDBPairingCodeRepository(PairingCodeRepository):
    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        self._table_name = table_name
        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)

    def create_code(self, code_hash: str, created_at: float, expires_at: float) -> bool:
        # AWS hackathon-final pass, P1-11: collision-safe pairing-code
        # generation. A 6-digit code is drawn from a space of only
        # 1,000,000 values (see device_pairing.py's PAIRING_CODE_LENGTH),
        # so at any real demo/production concurrency level two
        # independently-generated codes CAN collide within their 5-minute
        # overlap window -- see `PairingCodeRepository.create_code`'s
        # docstring for the full rationale. The bare `put_item` this used
        # to be would silently overwrite (and thereby invalidate) a
        # still-active code a different, unrelated pairing session was
        # relying on. This conditional PutItem instead only ever writes
        # when the same hash's prior item (if any) is either absent,
        # already used, or already expired -- all of which are safe to
        # recycle -- and reports the collision back to the caller
        # (`DevicePairingService.generate_pairing_code`) otherwise, so it
        # can draw a fresh, independently-random code and retry.
        try:
            self._table.put_item(
                Item={
                    "pk": _PK,
                    "sk": _paircode_sk(code_hash),
                    "code_hash": code_hash,
                    "created_at": _to_decimal(created_at),
                    "expires_at": _to_decimal(expires_at),
                    "used": False,
                    "ttl": _to_decimal(expires_at),
                },
                ConditionExpression="attribute_not_exists(pk) OR used = :true OR expires_at < :created_at",
                ExpressionAttributeValues={
                    ":true": True,
                    ":created_at": _to_decimal(created_at),
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def try_consume_code(self, code_hash: str, now: float) -> bool:
        try:
            self._table.update_item(
                Key={"pk": _PK, "sk": _paircode_sk(code_hash)},
                UpdateExpression="SET used = :true",
                ConditionExpression="attribute_exists(pk) AND used = :false AND expires_at > :now",
                ExpressionAttributeValues={
                    ":true": True,
                    ":false": False,
                    ":now": _to_decimal(now),
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


class DynamoDBDeviceTokenRepository(DeviceTokenRepository):
    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        self._table_name = table_name
        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)
        self._client = session.client("dynamodb", region_name=region_name, endpoint_url=endpoint_url)

    def create_token(
        self,
        device_id: str,
        token_hash: str,
        device_name: str,
        created_at: float,
        expires_at: float,
    ) -> None:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        token_item = {
            "pk": _PK,
            "sk": _devicetoken_sk(token_hash),
            "device_id": device_id,
            "token_hash": token_hash,
            "device_name": device_name,
            "created_at": _to_decimal(created_at),
            "expires_at": _to_decimal(expires_at),
            "revoked": False,
            "ttl": _to_decimal(expires_at),
        }
        index_item = {
            "pk": _PK,
            "sk": _deviceidx_sk(device_id),
            "device_id": device_id,
            "token_hash": token_hash,
        }
        # Both items are written in a single transaction so a crash between
        # them can never leave the device-id index pointing at nothing (or
        # the token existing without a way for an admin to revoke it by
        # device_id) -- device_id and token_hash are both freshly generated
        # random values (see device_pairing.py), so the attribute_not_exists
        # conditions below are a defense-in-depth guard against a
        # vanishingly unlikely collision, not the primary correctness
        # mechanism (that's the two independent random-value generators).
        self._client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": {k: serializer.serialize(v) for k, v in token_item.items()},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": {k: serializer.serialize(v) for k, v in index_item.items()},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )

    def get_token(self, token_hash: str) -> Optional[StoredDeviceToken]:
        response = self._table.get_item(Key={"pk": _PK, "sk": _devicetoken_sk(token_hash)})
        item = response.get("Item")
        if item is None:
            return None
        return _item_to_record(item)

    def revoke_token(self, token_hash: str) -> bool:
        try:
            self._table.update_item(
                Key={"pk": _PK, "sk": _devicetoken_sk(token_hash)},
                UpdateExpression="SET revoked = :true",
                ConditionExpression="attribute_exists(pk) AND revoked = :false",
                ExpressionAttributeValues={":true": True, ":false": False},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def revoke_by_device_id(self, device_id: str) -> bool:
        response = self._table.get_item(Key={"pk": _PK, "sk": _deviceidx_sk(device_id)})
        index_item = response.get("Item")
        if index_item is None:
            return False
        token_hash = index_item["token_hash"]
        return self.revoke_token(token_hash)


def _item_to_record(item: dict) -> StoredDeviceToken:
    return StoredDeviceToken(
        device_id=str(item["device_id"]),
        token_hash=str(item["token_hash"]),
        device_name=str(item["device_name"]),
        created_at=float(item["created_at"]),
        expires_at=float(item["expires_at"]),
        revoked=bool(item["revoked"]),
    )


def _to_decimal(value: float):
    from decimal import Decimal

    # DynamoDB has no native float type; boto3's resource-level API
    # requires Decimal for numeric attributes. Matches the same conversion
    # dynamodb.py/watch_dynamodb.py already do for numeric fields.
    return Decimal(str(value))


class DynamoDBPairingExchangeCoordinator(PairingExchangeCoordinator):
    """AWS hackathon-final pass, phase 3: the real, durable, multi-task
    -safe implementation of the atomic pairing exchange — see
    `pairing_base.py`'s `PairingExchangeCoordinator` docstring for the
    full rationale. A single `TransactWriteItems` call combines:

      1. An `Update` on the PAIRCODE# item, conditioned exactly like the
         existing `try_consume_code` (`attribute_exists(pk) AND
         used = :false AND expires_at > :now`) — marks it used.
      2. A `Put` on the DEVICETOKEN# item, conditioned on
         `attribute_not_exists(pk)` — collision-safe against an existing
         token sharing this (freshly, independently random) hash.
      3. A `Put` on the DEVICEIDX# item, same condition — collision-safe
         against an existing device_id.

    All three succeed together, or DynamoDB cancels the ENTIRE
    transaction and none of them take effect — including the PAIRCODE#
    update, so a `TOKEN_HASH_COLLISION`/`DEVICE_ID_COLLISION` outcome
    never burns the code; it remains exactly as consumable as it was
    before this call, and `DevicePairingService.exchange_pairing_code`
    (phase 3) is what retries the whole call with fresh random values.
    `TransactWriteItems`'s `CancellationReasons` (returned in the
    `ClientError`'s response, in the SAME order as `TransactItems`) is
    what lets this method tell which of the three conditions actually
    failed, so it can report a precise, correct outcome rather than a
    single generic failure."""

    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        self._table_name = table_name
        session = boto3.session.Session()
        self._client = session.client("dynamodb", region_name=region_name, endpoint_url=endpoint_url)

    def atomic_exchange(
        self,
        *,
        code_hash: str,
        now: float,
        device_id: str,
        token_hash: str,
        device_name: str,
        token_expires_at: float,
    ) -> PairingExchangeOutcome:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()

        def _ser(values: dict[str, Any]) -> dict[str, Any]:
            return {key: serializer.serialize(value) for key, value in values.items()}

        paircode_key = {"pk": _PK, "sk": _paircode_sk(code_hash)}
        token_item = {
            "pk": _PK,
            "sk": _devicetoken_sk(token_hash),
            "device_id": device_id,
            "token_hash": token_hash,
            "device_name": device_name,
            "created_at": _to_decimal(now),
            "expires_at": _to_decimal(token_expires_at),
            "revoked": False,
            "ttl": _to_decimal(token_expires_at),
        }
        index_item = {
            "pk": _PK,
            "sk": _deviceidx_sk(device_id),
            "device_id": device_id,
            "token_hash": token_hash,
            "ttl": _to_decimal(token_expires_at),
        }

        # Order matters: `atomic_exchange`'s own outcome-disambiguation
        # logic below indexes into `CancellationReasons` by position, and
        # relies on this exact order (paircode=0, token=1, deviceidx=2).
        transact_items = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _ser(paircode_key),
                    "UpdateExpression": "SET used = :true",
                    "ConditionExpression": "attribute_exists(pk) AND used = :false AND expires_at > :now",
                    "ExpressionAttributeValues": _ser({":true": True, ":false": False, ":now": _to_decimal(now)}),
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _ser(token_item),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _ser(index_item),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]

        try:
            self._client.transact_write_items(TransactItems=transact_items)
            return PairingExchangeOutcome.SUCCESS
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = exc.response.get("CancellationReasons", [])

            def _failed(index: int) -> bool:
                return index < len(reasons) and reasons[index].get("Code") == "ConditionalCheckFailed"

            # Priority order matches the docstring above: an invalid code
            # is never resolved by regenerating device_id/token_hash, so
            # it is reported first and takes precedence even if one of
            # the other two conditions also happened to fail in the same
            # attempt (e.g. an astronomically unlikely double-collision).
            if _failed(0):
                return PairingExchangeOutcome.CODE_REJECTED
            if _failed(1):
                return PairingExchangeOutcome.TOKEN_HASH_COLLISION
            if _failed(2):
                return PairingExchangeOutcome.DEVICE_ID_COLLISION
            # No condition check reported as the cause (e.g. a transient
            # TransactionConflict) — safest to surface as CODE_REJECTED
            # (no retry-with-fresh-values loop) rather than silently
            # retrying against unknown root cause; the caller (or an
            # upstream retry at the HTTP layer) can simply try again.
            return PairingExchangeOutcome.CODE_REJECTED


class DynamoDBPairingRateLimiterExceeded(Exception):
    """Raised by `DynamoDBPairingRateLimiter.check_and_record` when the
    caller has exceeded the configured attempt budget for the current
    fixed window. Kept distinct from
    `app.services.device_pairing.PairingCodeRateLimitedError` (the
    in-process limiter's exception) so callers depend on the interface
    behavior, not the class identity; `routes.py` catches both."""


class DynamoDBPairingRateLimiter:
    """A distributed, multi-task-safe fixed-window rate limiter for
    pairing-code EXCHANGE attempts, backed by the same DynamoDB table as
    everything else here (spec v1.1 pass 4, mandatory correction 1: "do
    not depend only on an in-process dictionary").

    Uses a single atomic `UpdateItem` per attempt
    (`ADD attempt_count :one` guarded by
    `ConditionExpression="attempt_count < :max"`), which DynamoDB
    evaluates against the item's committed state before applying the
    update -- exactly the same "conditional write is the source of
    truth" pattern the rest of this module uses, so two ECS tasks
    handling concurrent requests from the same client never under-count
    each other's attempts the way two independent in-process dictionaries
    on two different tasks silently would.

    This is a FIXED window (not sliding/leaky-bucket): all attempts whose
    `now` falls in the same `window_seconds`-wide bucket share one
    counter, which resets at the window boundary. That is a coarser
    guarantee than the in-process `PairingAttemptRateLimiter` (fixed
    window as well, but recomputed on every check from a rolling list) --
    an intentional, documented trade-off for O(1) storage per
    window/key rather than storing a timestamp per attempt in DynamoDB.
    `docs/deployment-runbook.md` additionally recommends an AWS WAF rate
    -based rule on the public `POST /v1/auth/pair` route as a
    complementary, network-edge control that stops abusive traffic before
    it reaches the application at all -- this class is the
    application-level backstop for when WAF is not configured or a
    request gets through it.
    """

    def __init__(self, table_name: str, region_name: str, max_attempts: int, window_seconds: float, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        self._table_name = table_name
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        session = boto3.session.Session()
        self._table = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url).Table(table_name)

    def check_and_record(self, key: str) -> None:
        import time

        now = time.time()
        window_start = int(now // self._window_seconds) * self._window_seconds
        sk = f"RATELIMIT#{key}#{window_start}"
        try:
            self._table.update_item(
                Key={"pk": _PK, "sk": sk},
                UpdateExpression="ADD attempt_count :one SET #ttl = :ttl",
                ConditionExpression="attribute_not_exists(attempt_count) OR attempt_count < :max",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":max": self._max_attempts,
                    ":ttl": _to_decimal(window_start + self._window_seconds + 60),
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise DynamoDBPairingRateLimiterExceeded(
                    f"Too many pairing attempts from this source; try again after the "
                    f"current {self._window_seconds:.0f}s window."
                )
            raise
