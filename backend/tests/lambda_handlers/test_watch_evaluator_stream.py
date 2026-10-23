"""Tests for app/lambda_handlers/watch_evaluator_stream.py (spec v1.1
repair 12).

Uses `moto` for a mocked DynamoDB table, exactly like
test_watch_dynamodb_repository.py, plus a hand-built DynamoDB Streams
event payload in the same shape a real event-source mapping delivers
(DynamoDB's own "AttributeValue" JSON encoding, produced here with
boto3's TypeSerializer so the test payload is provably valid DynamoDB
JSON rather than hand-guessed). This proves the handler's event-parsing
and evaluator wiring; it does not and cannot prove a real DynamoDB
Streams event-source mapping invokes it correctly — see this module's own
docstring in app/lambda_handlers/__init__.py.
"""

from __future__ import annotations

import uuid

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from boto3.dynamodb.types import TypeSerializer  # noqa: E402
from moto import mock_aws  # noqa: E402

from app.domain.watch_models import WatchStatus  # noqa: E402
from app.services.strategy_engine import StrategyEngine  # noqa: E402
from app.services.watch_evaluator import WatchEvaluator  # noqa: E402
from tests.conftest import race_state_at_lap  # noqa: E402

TABLE_NAME = "pit-wall-race-state-lambda-test"
_serializer = TypeSerializer()


def _state_item_to_new_image(race_id: str, sk: str, item: dict) -> dict:
    """Converts a plain Python dict (the same shape
    app/repositories/dynamodb.py's save_state builds) into the DynamoDB
    Streams "NewImage" AttributeValue-map encoding."""
    return {key: _serializer.serialize(value) for key, value in item.items()}


def _stream_record(new_image: dict, *, sequence_number: str, event_name: str = "MODIFY") -> dict:
    return {
        "eventID": str(uuid.uuid4()),
        "eventName": event_name,
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "SequenceNumber": sequence_number,
            "StreamViewType": "NEW_IMAGE",
            "NewImage": new_image,
        },
    }


@pytest.fixture
def dynamodb_table(monkeypatch):
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
        monkeypatch.setenv("DYNAMODB_TABLE_NAME", TABLE_NAME)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        yield


@pytest.fixture
def handler_module(dynamodb_table):
    """Imports the handler fresh and resets its module-level (cold-start)
    evaluator cache, so each test gets its own evaluator pointed at its
    own freshly mocked table rather than reusing one built by a previous
    test's (different) mock_aws context."""
    from app.lambda_handlers import watch_evaluator_stream

    watch_evaluator_stream._watch_evaluator = None
    yield watch_evaluator_stream
    watch_evaluator_stream._watch_evaluator = None


@pytest.fixture
def scenario(handler_module):
    from app.config import DEFAULT_SCENARIO_PATH
    from app.domain.scenario import load_scenario

    return load_scenario(str(DEFAULT_SCENARIO_PATH))


@pytest.fixture
def seeded_watch(scenario, dynamodb_table):
    """Creates one CLOSED baseline watch at lap 24 directly through a
    (separately constructed, but pointed at the SAME mocked table)
    WatchEvaluator — mirroring
    test_real_scenario_watch_opens_consistently_with_direct_engine_calls's
    setup in test_watch_evaluator.py. Returns (run_id, watch)."""
    import asyncio

    from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBWatchRepository

    watch_repository = DynamoDBWatchRepository(table_name=TABLE_NAME, region_name="us-east-1")
    alert_repository = DynamoDBAlertRepository(table_name=TABLE_NAME, region_name="us-east-1")
    strategy_engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)
    evaluator = WatchEvaluator(watch_repository=watch_repository, alert_repository=alert_repository, strategy_engine=strategy_engine)

    run_id = str(uuid.uuid4())
    initial_state = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    watch = asyncio.run(evaluator.create_watch(initial_state, "CAR_04", "CAR_03"))
    assert watch.status == WatchStatus.CLOSED
    return run_id, watch, alert_repository


def test_state_write_that_opens_a_watch_publishes_an_alert(handler_module, scenario, seeded_watch):
    """The end-to-end path this handler exists for: a STATE-item DynamoDB
    Streams record that advances the race past the known lap-24->28
    CLOSED->OPEN transition (see test_watch_evaluator.py) results in a
    real Alert being written to the (mocked) alert repository — proving
    the handler's DynamoDB-JSON parsing, RaceState reconstruction and
    WatchEvaluator wiring all actually work, not just that they don't
    raise."""
    run_id, watch, alert_repository = seeded_watch
    later_state = race_state_at_lap(scenario, 28, run_id=run_id, state_version=1)

    item = {
        "pk": f"RACE#{later_state.race_id}",
        "sk": "STATE",
        "race_id": later_state.race_id,
        "run_id": later_state.run_id,
        "state_version": later_state.state_version,
        "data": later_state.model_dump_json(),
    }
    new_image = _state_item_to_new_image(later_state.race_id, "STATE", item)
    event = {"Records": [_stream_record(new_image, sequence_number="1")]}

    response = handler_module.handler(event, None)
    assert response == {"batchItemFailures": []}

    import asyncio

    alerts = asyncio.run(alert_repository.list_alerts(later_state.race_id))
    assert len(alerts) == 1
    assert alerts[0].new_status == WatchStatus.OPEN
    assert alerts[0].previous_status == WatchStatus.CLOSED
    assert alerts[0].watch_id == watch.watch_id


def test_duplicate_stream_delivery_publishes_only_one_alert(handler_module, scenario, seeded_watch):
    """Spec v1.1 repair 12 / this handler's own module docstring promise:
    'a duplicate or out-of-order delivery... must be a pure no-op'. A real
    DynamoDB Streams event-source mapping can, and does, occasionally
    redeliver the same record (at-least-once delivery) — this simulates
    exactly that by handing the SAME record to `handler()` twice in a row
    and asserts the second call publishes no additional alert, proving the
    idempotency guarantee this handler relies on (WatchEvaluator's own
    state_version guard) actually holds across two separate Lambda
    invocations sharing one warm evaluator instance, not just within a
    single evaluate() call."""
    import asyncio

    run_id, watch, alert_repository = seeded_watch
    later_state = race_state_at_lap(scenario, 28, run_id=run_id, state_version=1)
    item = {
        "pk": f"RACE#{later_state.race_id}",
        "sk": "STATE",
        "race_id": later_state.race_id,
        "run_id": later_state.run_id,
        "state_version": later_state.state_version,
        "data": later_state.model_dump_json(),
    }
    new_image = _state_item_to_new_image(later_state.race_id, "STATE", item)
    record = _stream_record(new_image, sequence_number="1")

    first_response = handler_module.handler({"Records": [record]}, None)
    second_response = handler_module.handler({"Records": [record]}, None)  # redelivery of the SAME record
    assert first_response == {"batchItemFailures": []}
    assert second_response == {"batchItemFailures": []}

    alerts = asyncio.run(alert_repository.list_alerts(later_state.race_id))
    assert len(alerts) == 1  # NOT 2 — the redelivery must be a pure no-op


def test_control_item_write_is_ignored(handler_module, dynamodb_table):
    """A CONTROL-item write (reset/demo-control state) must never reach
    the evaluator — mirroring app/main.py's in-process hook, which is
    only ever invoked from the state-commit path, never from control
    writes."""
    control_item = {
        "pk": "RACE#demo-race-001",
        "sk": "CONTROL",
        "race_id": "demo-race-001",
        "run_id": "run-x",
        "state_version": 1,
        "data": "{}",
    }
    new_image = _state_item_to_new_image("demo-race-001", "CONTROL", control_item)
    event = {"Records": [_stream_record(new_image, sequence_number="1")]}

    # Must not raise (a naive implementation might try
    # RaceState.model_validate_json("{}") and blow up) and must report no
    # batch item failures — a CONTROL write is a legitimate, expected
    # record this handler simply has nothing to do with.
    response = handler_module.handler(event, None)
    assert response == {"batchItemFailures": []}


def test_malformed_record_is_reported_as_a_batch_item_failure(handler_module, dynamodb_table):
    """A record this handler cannot even parse (missing `dynamodb.NewImage`
    entirely) is reported via `batchItemFailures` rather than raising and
    failing the whole batch — see the handler's own module docstring."""
    malformed_record = {
        "eventID": str(uuid.uuid4()),
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "dynamodb": {"SequenceNumber": "42"},
    }
    event = {"Records": [malformed_record]}

    response = handler_module.handler(event, None)
    # A record with no NewImage at all is treated as "nothing to evaluate"
    # (matches a REMOVE event's shape), not a parse failure — it has no
    # data to fail on, so it is simply skipped, not reported.
    assert response == {"batchItemFailures": []}


def test_state_write_with_no_matching_watch_is_a_safe_no_op(handler_module, scenario, dynamodb_table):
    """A STATE write for a race/run with no active watches yet (e.g. the
    very first tick of a new run) must not raise and must publish no
    alerts — there is simply nothing to evaluate."""
    state = race_state_at_lap(scenario, 24, run_id=str(uuid.uuid4()), state_version=0)
    item = {
        "pk": f"RACE#{state.race_id}",
        "sk": "STATE",
        "race_id": state.race_id,
        "run_id": state.run_id,
        "state_version": state.state_version,
        "data": state.model_dump_json(),
    }
    new_image = _state_item_to_new_image(state.race_id, "STATE", item)
    event = {"Records": [_stream_record(new_image, sequence_number="1", event_name="INSERT")]}

    response = handler_module.handler(event, None)
    assert response == {"batchItemFailures": []}
