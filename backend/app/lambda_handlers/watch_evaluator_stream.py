"""Watch Evaluator, invoked by DynamoDB Streams (spec v1.1 section 18 / repair 12).

Spec section 18: "DynamoDB Streams SHALL invoke the Watch Evaluator
whenever race state changes." `app/services/watch_evaluator.py` already
documented this exact split (in-process callback for Gate 0-4's
single-process demo; a Streams-triggered Lambda for a real AWS
deployment) before this module existed. This module is the second half of
that promise: it is a thin adapter that turns a DynamoDB Streams event
into a call to the SAME `WatchEvaluator.evaluate()` the backend process
already calls from `app/main.py`'s `_on_state_committed` hook — no
evaluation rule (hysteresis, idempotency key, run_id/state_version guard,
staleness suppression) is reimplemented here.

Trigger configuration (see ../../../infrastructure/template.yaml):
  - Source: the `pit-wall-race-state` table's DynamoDB Stream
    (StreamViewType: NEW_IMAGE — OLD_IMAGE is never used by the evaluator,
    so it is deliberately not requested, keeping stream records smaller).
  - Filter: `infrastructure/template.yaml`'s event-source-mapping
    `FilterCriteria` restricts invocations to records whose `NewImage.sk.S
    == "STATE"` — CONTROL-item writes (`reset`/demo-control state) never
    reach this handler, matching the in-process hook, which is only ever
    invoked from `SimulatorService`'s state-commit path (see
    `app/services/simulator.py`), never from control-state writes either.

Idempotency and ordering: DynamoDB Streams (via a Lambda event-source
mapping) can invoke this handler more than once for the same change, and
records within a shard are delivered in order but a retried/re-driven
batch can re-deliver an already-processed record. Both are already safe
here because `WatchEvaluator.evaluate()` itself guards on
`race_state.state_version > watch.last_evaluated_state_version` (spec
49/repair 6) — a duplicate or out-of-order delivery for a state_version
that has already advanced a watch past it is a pure no-op, not a second
alert. This handler adds no de-duplication of its own; it relies entirely
on that existing, already-tested guarantee.

Failure handling: each record is evaluated independently and a failure
evaluating one record is logged and skipped rather than raising, so one
malformed or unevaluable record cannot block the rest of the batch or
cause the whole batch to be retried (mirroring `WatchEvaluator.evaluate`'s
own per-watch `try/except StrategyValidationError: continue`, which
already accepts that a watch can be temporarily unevaluable "so the demo
must keep running" — the same philosophy applied one level up, at the
per-record boundary).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

from app.config import DEFAULT_SCENARIO_PATH
from app.domain.models import RaceState
from app.domain.scenario import load_scenario
from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBTransitionWriter, DynamoDBWatchRepository
from app.services.strategy_engine import StrategyEngine
from app.services.watch_evaluator import WatchEvaluator

logger = logging.getLogger("pit_wall_copilot.lambda.watch_evaluator_stream")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_deserializer = TypeDeserializer()

# Built once per (warm) execution environment, not per invocation — the
# scenario file and its derived StrategyEngine are immutable for the
# lifetime of the process, exactly like `app/main.py`'s module-scope
# construction at FastAPI startup. A cold start pays this once; a warm
# invocation reuses it, avoiding a repeated file read and engine
# construction on every stream record.
_watch_evaluator: WatchEvaluator | None = None


def _get_watch_evaluator() -> WatchEvaluator:
    global _watch_evaluator
    if _watch_evaluator is None:
        table_name = os.environ["DYNAMODB_TABLE_NAME"]
        region_name = os.environ.get("AWS_REGION", "us-east-1")
        scenario_path = os.environ.get("SCENARIO_PATH", str(DEFAULT_SCENARIO_PATH))

        scenario = load_scenario(scenario_path)
        strategy_engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)
        watch_repository = DynamoDBWatchRepository(table_name=table_name, region_name=region_name)
        alert_repository = DynamoDBAlertRepository(table_name=table_name, region_name=region_name)
        # Spec v1.1 correction 9: this Lambda is exactly the deployment
        # this atomicity fix matters most for — a real Streams-triggered
        # invocation can be killed mid-execution (timeout, OOM, a bad
        # deploy) far more plausibly than the single long-lived backend
        # process app/main.py runs. See TransitionWriter's class docstring
        # (watch_base.py) for the crash-recovery gap this closes, and
        # DynamoDBTransitionWriter's own class docstring (watch_dynamodb.py)
        # for why it takes table_name/region_name directly rather than
        # reusing watch_repository/alert_repository's boto3 objects.
        transition_writer = DynamoDBTransitionWriter(table_name=table_name, region_name=region_name)

        _watch_evaluator = WatchEvaluator(
            watch_repository=watch_repository,
            alert_repository=alert_repository,
            strategy_engine=strategy_engine,
            transition_writer=transition_writer,
        )
    return _watch_evaluator


def _extract_race_state(record: dict[str, Any]) -> RaceState | None:
    """Returns the RaceState carried by one DynamoDB Streams record, or
    None if the record isn't a STATE-item write this handler cares about
    (a CONTROL-item write, a REMOVE event, or a record missing the fields
    this handler expects — logged and skipped, never raised)."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return None

    dynamodb_payload = record.get("dynamodb", {})
    new_image_raw = dynamodb_payload.get("NewImage")
    if not new_image_raw:
        return None

    new_image = {key: _deserializer.deserialize(value) for key, value in new_image_raw.items()}
    if new_image.get("sk") != "STATE":
        return None

    data = new_image.get("data")
    if not data:
        logger.warning("STATE record missing 'data' field; skipping: pk=%s", new_image.get("pk"))
        return None

    return RaceState.model_validate_json(data)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point (DynamoDB Streams event-source mapping).

    Returns a `batchItemFailures`-shaped response so a caller using
    `FunctionResponseTypes: [ReportBatchItemFailures]` (as
    `infrastructure/template.yaml` configures) can retry only the records
    this invocation could not even parse into a RaceState — evaluation
    failures for a record that WAS parsed successfully are intentionally
    swallowed per the module docstring above, not reported as batch
    failures, since re-driving them would not change WatchEvaluator's
    already-deterministic outcome.
    """
    watch_evaluator = _get_watch_evaluator()
    batch_item_failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        sequence_number = record.get("dynamodb", {}).get("SequenceNumber", "")
        try:
            race_state = _extract_race_state(record)
        except Exception:  # noqa: BLE001 — a malformed record must not crash the batch
            logger.exception("Failed to parse DynamoDB Streams record; reporting as a batch item failure")
            if sequence_number:
                batch_item_failures.append({"itemIdentifier": sequence_number})
            continue

        if race_state is None:
            continue

        try:
            alerts = asyncio.run(watch_evaluator.evaluate(race_state))
        except Exception:  # noqa: BLE001 — see module docstring: log and move on, never block the batch
            logger.exception(
                "watch_evaluator.evaluate() raised for race_id=%s state_version=%s",
                race_state.race_id,
                race_state.state_version,
            )
            continue

        if alerts:
            logger.info(
                "Published %d alert(s) for race_id=%s run_id=%s state_version=%s",
                len(alerts),
                race_state.race_id,
                race_state.run_id,
                race_state.state_version,
            )

    return {"batchItemFailures": batch_item_failures}
