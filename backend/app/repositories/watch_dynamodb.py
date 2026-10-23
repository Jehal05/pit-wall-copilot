"""DynamoDB-backed watch/alert repositories.

Fixes the previously-documented Gate 2 limitation: "Watch/alert storage is
in-memory only ... A DynamoDB-backed implementation is not included for
watches/alerts in this gate — only race state has a DynamoDB path." This
module gives `WatchRepository`/`AlertRepository` (watch_base.py) real
DynamoDB-backed implementations, selected the same way
`DynamoDBRaceStateRepository` already is (repositories/factory.py,
`STATE_REPOSITORY=dynamodb`) — one setting now covers race state AND
watches/alerts, since they are conceptually the same "durable demo state"
concern spec section 18/48/49 describes.

Table design — reuses the SAME table race state already uses
(`DYNAMODB_TABLE_NAME`), distinguishing item types by sort-key prefix, the
same pattern `dynamodb.py` already established for STATE/CONTROL:

  WATCH item:        pk="RACE#<race_id>"  sk="WATCH#<watch_id>"
    { pk, sk, race_id, run_id, watch_id, data (JSON Watch) }

  ALERT item:         pk="RACE#<race_id>"  sk="ALERT#<alert_id>"
    { pk, sk, race_id, watch_id, alert_id, state_version, created_at (ISO
      string, for sorting), data (JSON Alert) }

  ALERT idempotency:  pk="RACE#<race_id>"  sk="ALERTIDX#<idempotency_key>"
    { pk, sk, alert_id }
    A second, small item purely to make `record_transition` idempotent —
    see below.

No new table needs provisioning beyond what `infrastructure/README.md`
already documents for race state; the same table just gains more sk
prefixes. `WATCH#`, `ALERT#` and `ALERTIDX#` are chosen so that
`sk.begins_with("ALERT#")` never accidentally matches an `ALERTIDX#` item
(they diverge at the 6th character: "ALERT#" vs "ALERTI...").

Conditional watch updates (spec repair 6: "Make watch updates conditional
on the stored last_evaluated_state_version. Prevent an older evaluation
from overwriting a newer watch."): the WATCH item duplicates
`last_evaluated_state_version` as its own top-level attribute (not only
buried inside `data`) specifically so `update_watch`'s optional
`require_last_evaluated_state_version_below` can reference it in a
ConditionExpression. A conflicting write raises `WatchUpdateConflict`
(watch_base.py) rather than silently overwriting — see
services/watch_evaluator.py for why this is a second, storage-level line
of defense on top of the in-process guard the evaluator already applies
before doing any calculation.

Idempotent alert recording (spec 49: an alert transition is recorded
EXACTLY ONCE per idempotency_key) is enforced with a DynamoDB conditional
write on the ALERTIDX item (`attribute_not_exists(pk)`) — the same
conditional-write-for-correctness pattern `DynamoDBRaceStateRepository`
already uses for stale-write rejection, not a read-then-write race and not
an application-level lock (which would not be safe across multiple backend
processes/Lambda invocations anyway — the in-memory repository's
`asyncio.Lock` only ever protected a single process).

*** Spec v1.1 correction 9: cross-item atomicity ***
`DynamoDBWatchRepository.update_watch` and `DynamoDBAlertRepository
.record_transition`, on their own, are each independently
conditional/idempotent, but calling them as two SEPARATE writes (as the
watch evaluator used to) leaves a real crash-recovery gap: if the process
dies after the alert half commits but before the watch half does, the
watch is left durably stuck on its old status, and the NEXT (genuinely
newer) evaluation can recompute and durably record a second, duplicate
alert for a transition that already fired — a duplicate a retry of the
SAME state_version would NOT produce (that case IS safely idempotent),
but a subsequent, different state_version is not protected against it.
`DynamoDBTransitionWriter` (below) is the fix: it commits the idempotency
marker, the alert item, and the watch item together in a single
`TransactWriteItems` call, so there is no window in which one half can be
durably committed without the other. `WatchEvaluator` uses it (via
`repositories/factory.py`'s `build_transition_writer`) for every
status-CHANGING evaluation; a non-transitioning evaluation (no alert to
record) still uses the simpler, unconditional-or-singly-conditional
`update_watch` directly, since there is nothing to keep atomic with in
that case.

Only imported when STATE_REPOSITORY=dynamodb (repositories/factory.py), so
boto3 stays an optional runtime dependency for memory-mode development and
the bulk of the test suite, matching dynamodb.py's existing convention.
"""

from __future__ import annotations

from typing import Optional

from app.domain.watch_models import Alert, Watch
from app.repositories.watch_base import AlertRepository, TransitionWriter, WatchRepository, WatchUpdateConflict


def _race_pk(race_id: str) -> str:
    return f"RACE#{race_id}"


def _watch_sk(watch_id: str) -> str:
    return f"WATCH#{watch_id}"


def _alert_sk(alert_id: str) -> str:
    return f"ALERT#{alert_id}"


def _alert_idx_sk(idempotency_key: str) -> str:
    return f"ALERTIDX#{idempotency_key}"


def _watch_item(watch: Watch) -> dict:
    # `last_evaluated_state_version` is duplicated as a top-level attribute
    # (not just inside `data`) specifically so a ConditionExpression can
    # reference it — DynamoDB conditions can't reach into an opaque JSON
    # string blob. Shared by DynamoDBWatchRepository._put and
    # DynamoDBTransitionWriter (spec v1.1 correction 9) so both build the
    # exact same item shape for a watch write.
    return {
        "pk": _race_pk(watch.race_id),
        "sk": _watch_sk(watch.watch_id),
        "race_id": watch.race_id,
        "run_id": watch.run_id,
        "watch_id": watch.watch_id,
        "last_evaluated_state_version": watch.last_evaluated_state_version,
        "data": watch.model_dump_json(),
    }


def _alert_item(alert: Alert) -> dict:
    # Shared by DynamoDBAlertRepository._put_alert and
    # DynamoDBTransitionWriter so both build the exact same item shape.
    return {
        "pk": _race_pk(alert.race_id),
        "sk": _alert_sk(alert.alert_id),
        "race_id": alert.race_id,
        "watch_id": alert.watch_id,
        "alert_id": alert.alert_id,
        "state_version": alert.state_version,
        "data": alert.model_dump_json(),
    }


class DynamoDBWatchRepository(WatchRepository):
    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)

    async def create_watch(self, watch: Watch) -> Watch:
        self._put(watch)
        return watch

    async def get_watch(self, race_id: str, watch_id: str) -> Optional[Watch]:
        response = self._table.get_item(Key={"pk": _race_pk(race_id), "sk": _watch_sk(watch_id)})
        item = response.get("Item")
        if not item:
            return None
        return Watch.model_validate_json(item["data"])

    async def update_watch(
        self, watch: Watch, *, require_last_evaluated_state_version_below: Optional[int] = None
    ) -> Watch:
        if require_last_evaluated_state_version_below is None:
            self._put(watch)
            return watch

        from botocore.exceptions import ClientError

        try:
            self._put(
                watch,
                condition_expression=(
                    "attribute_not_exists(pk) OR last_evaluated_state_version < :req_below"
                ),
                expression_attribute_values={":req_below": require_last_evaluated_state_version_below},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise WatchUpdateConflict(
                    f"watch {watch.watch_id}: stored last_evaluated_state_version is already "
                    f">= {require_last_evaluated_state_version_below}"
                ) from exc
            raise
        return watch

    async def delete_watch(self, race_id: str, watch_id: str) -> bool:
        response = self._table.delete_item(
            Key={"pk": _race_pk(race_id), "sk": _watch_sk(watch_id)},
            ReturnValues="ALL_OLD",
        )
        # bool(...) rather than "Attributes" in response: real DynamoDB
        # omits the key entirely when nothing was deleted, but moto (used
        # by our test suite) returns an empty dict instead — bool() treats
        # both the same way, so this is correct against either.
        return bool(response.get("Attributes"))

    async def list_watches(self, race_id: str) -> tuple[Watch, ...]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(_race_pk(race_id)) & Key("sk").begins_with("WATCH#")
        )
        return tuple(Watch.model_validate_json(item["data"]) for item in response.get("Items", []))

    async def list_active_watches(self, race_id: str, run_id: str) -> tuple[Watch, ...]:
        watches = await self.list_watches(race_id)
        return tuple(w for w in watches if w.run_id == run_id)

    def _put(
        self,
        watch: Watch,
        *,
        condition_expression: Optional[str] = None,
        expression_attribute_values: Optional[dict] = None,
    ) -> None:
        kwargs: dict = {"Item": _watch_item(watch)}
        if condition_expression is not None:
            kwargs["ConditionExpression"] = condition_expression
        if expression_attribute_values is not None:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values
        self._table.put_item(**kwargs)


class DynamoDBAlertRepository(AlertRepository):
    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily so memory-mode never requires boto3/AWS

        session = boto3.session.Session()
        resource = session.resource("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table = resource.Table(table_name)

    async def record_transition(self, idempotency_key: str, alert: Alert) -> tuple[Alert, bool]:
        from botocore.exceptions import ClientError

        pk = _race_pk(alert.race_id)
        try:
            # Reserve the idempotency slot first, and only it. A duplicate
            # evaluation racing this one (e.g. two evaluator invocations for
            # the same state_version) fails here with
            # ConditionalCheckFailedException before either writes the full
            # alert item, so the alert item itself never needs its own
            # conditional check.
            self._table.put_item(
                Item={"pk": pk, "sk": _alert_idx_sk(idempotency_key), "alert_id": alert.alert_id},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                idx_response = self._table.get_item(Key={"pk": pk, "sk": _alert_idx_sk(idempotency_key)})
                existing_alert_id = idx_response["Item"]["alert_id"]
                existing = await self.get_alert(alert.race_id, existing_alert_id)
                if existing is None:
                    # Only possible if the idx write succeeded but the
                    # follow-up alert-item write below never completed on
                    # the original call (process crash mid-write) — treat
                    # it the same way a fresh write would and self-heal by
                    # completing it now under the alert_id already reserved.
                    existing = alert.model_copy(update={"alert_id": existing_alert_id})
                    self._put_alert(existing)
                return existing, False
            raise

        self._put_alert(alert)
        return alert, True

    async def get_alert(self, race_id: str, alert_id: str) -> Optional[Alert]:
        response = self._table.get_item(Key={"pk": _race_pk(race_id), "sk": _alert_sk(alert_id)})
        item = response.get("Item")
        if not item:
            return None
        return Alert.model_validate_json(item["data"])

    async def list_alerts(self, race_id: str, watch_id: Optional[str] = None) -> tuple[Alert, ...]:
        from boto3.dynamodb.conditions import Attr, Key

        query_kwargs = {
            "KeyConditionExpression": Key("pk").eq(_race_pk(race_id)) & Key("sk").begins_with("ALERT#"),
        }
        if watch_id is not None:
            query_kwargs["FilterExpression"] = Attr("watch_id").eq(watch_id)
        response = self._table.query(**query_kwargs)
        alerts = [Alert.model_validate_json(item["data"]) for item in response.get("Items", [])]
        alerts.sort(key=lambda a: (a.state_version, a.created_at), reverse=True)
        return tuple(alerts)

    def _put_alert(self, alert: Alert) -> None:
        self._table.put_item(Item=_alert_item(alert))


class DynamoDBTransitionWriter(TransitionWriter):
    """Spec v1.1 correction 9 — see `TransitionWriter`'s class docstring
    (watch_base.py) for the full rationale. Commits the ALERTIDX
    idempotency marker, the ALERT item, and the WATCH item in a single
    `TransactWriteItems` call: either all three commit together, or none
    do. This is what makes it impossible for a crash (or a Lambda restart)
    to leave a committed alert behind a watch that never got updated to
    match it — the specific gap a two-step
    `record_transition()` + `update_watch()` sequence cannot close.

    Constructed from the SAME table_name/region/endpoint a
    `DynamoDBWatchRepository`/`DynamoDBAlertRepository` pair for this
    process were built from (per this module's own docstring, they always
    point at the SAME underlying table, so one table identity is enough).

    *** Why this does NOT reuse `alert_repository._table.meta.client` ***
    That looked like the obvious reuse (skip a second boto3 client, use
    the alert repository's own boto3 `Table` resource's underlying raw
    client for the low-level `transact_write_items` call its `Table` API
    doesn't expose). It is wrong: a client obtained via
    `resource.meta.client` has the DynamoDB resource's high-level
    Python-native <-> AttributeValue TRANSFORM HANDLER attached to it —
    for EVERY call made through that client instance, not just calls made
    through the `Table` object itself. This module already serializes
    every item to proper low-level AttributeValue form (`{"S": ...}`,
    `{"N": ...}`) via `TypeSerializer` before calling
    `transact_write_items` (the low-level API expects that form) — but a
    `resource.meta.client` would then transform it AGAIN, treating each
    already-correct `{"S": "..."}` dict as if it were a native Python
    value to be wrapped, producing a doubly-nested (and un-hashable, from
    moto's perspective) attribute value. A plain `boto3.client("dynamodb")`
    has no such transform and is the correct client for raw
    `TransactWriteItems` calls.
    """

    def __init__(self, table_name: str, region_name: str, endpoint_url: Optional[str] = None) -> None:
        import boto3  # imported lazily, matching this module's existing convention

        self._client = boto3.client("dynamodb", region_name=region_name, endpoint_url=endpoint_url)
        self._table_name = table_name

    async def write_transition(
        self,
        idempotency_key: str,
        alert: Alert,
        updated_watch: Watch,
        *,
        require_last_evaluated_state_version_below: Optional[int] = None,
    ) -> tuple[Optional[Alert], bool]:
        from boto3.dynamodb.types import TypeSerializer
        from botocore.exceptions import ClientError

        serializer = TypeSerializer()

        def _ser(item: dict) -> dict:
            return {key: serializer.serialize(value) for key, value in item.items()}

        pk = _race_pk(alert.race_id)
        idx_item = {"pk": pk, "sk": _alert_idx_sk(idempotency_key), "alert_id": alert.alert_id}

        watch_put: dict = {"TableName": self._table_name, "Item": _ser(_watch_item(updated_watch))}
        if require_last_evaluated_state_version_below is not None:
            watch_put["ConditionExpression"] = "attribute_not_exists(pk) OR last_evaluated_state_version < :req_below"
            watch_put["ExpressionAttributeValues"] = {
                ":req_below": serializer.serialize(require_last_evaluated_state_version_below)
            }

        # Order matters for interpreting CancellationReasons below: idx (0),
        # alert (1), watch (2).
        transact_items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _ser(idx_item),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {"Put": {"TableName": self._table_name, "Item": _ser(_alert_item(alert))}},
            {"Put": watch_put},
        ]

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = exc.response.get("CancellationReasons", [])

            def _reason_code(index: int) -> Optional[str]:
                return reasons[index].get("Code") if index < len(reasons) else None

            if _reason_code(0) == "ConditionalCheckFailedException" or _reason_code(0) == "ConditionalCheckFailed":
                # A duplicate/replay of an idempotency_key that some
                # earlier call already committed FULLY (alert + watch
                # together, by construction of this atomic write — there
                # is no partial-commit state to self-heal, unlike the old
                # two-step AlertRepository.record_transition). Return that
                # existing alert; do not touch the watch.
                idx_response = self._client.get_item(
                    TableName=self._table_name,
                    Key={"pk": {"S": pk}, "sk": {"S": _alert_idx_sk(idempotency_key)}},
                )
                idx_item_found = idx_response.get("Item")
                if idx_item_found is None:
                    # The conditional failure said this key exists, yet a
                    # follow-up read finds nothing — genuinely unexpected
                    # (not a known, self-healable case for an atomically
                    # written record). Surface the original error rather
                    # than guessing at a fabricated alert.
                    raise
                existing_alert_id = idx_item_found["alert_id"]["S"]
                alert_response = self._client.get_item(
                    TableName=self._table_name,
                    Key={"pk": {"S": pk}, "sk": {"S": _alert_sk(existing_alert_id)}},
                )
                alert_item_found = alert_response.get("Item")
                if alert_item_found is None:
                    raise RuntimeError(
                        f"idempotency marker for {idempotency_key!r} exists but its alert item "
                        f"({existing_alert_id!r}) does not — this should be impossible for a write "
                        "committed via DynamoDBTransitionWriter's single TransactWriteItems call."
                    )
                existing_alert = Alert.model_validate_json(alert_item_found["data"]["S"])
                return existing_alert, False

            # idx succeeded (this genuinely is a new idempotency_key) but
            # the watch's conditional check failed: a concurrent or
            # crash-recovered newer evaluation already advanced the watch
            # past `updated_watch`'s baseline. The WHOLE transaction was
            # rolled back — nothing was written, including the idx/alert —
            # so this is exactly the old WatchUpdateConflict outcome:
            # publish nothing, touch nothing.
            return None, False

        return alert, True
