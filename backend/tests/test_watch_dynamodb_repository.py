"""Tests for the DynamoDB-backed watch/alert repositories
(app/repositories/watch_dynamodb.py) — fixes the previously-documented
Gate 2 limitation that only race state had a DynamoDB path.

Uses `moto` (a mocked AWS environment), exactly like
test_dynamodb_repository.py already does for race state — no real AWS
account or credentials are needed to run these.

`Watch` is a small, flat model, so watch-repository tests hand-build it
directly. `Alert` nests a full `RaceState` and `UndercutResult`, so
alert-repository tests instead drive the REAL `WatchEvaluator` +
`StrategyEngine` pipeline (the same pattern
test_watch_evaluator.py's `test_real_scenario_watch_opens_consistently_...`
and test_explanation_engine.py's `_make_real_alert` already use) — this
proves the whole spec-18/48/49 pipeline actually works end-to-end against
DynamoDB, not just that hand-built fixtures round-trip through it.
"""

from __future__ import annotations

import uuid

import pytest


# Spec v1.1 pass 3, correction 10: exercises moto-simulated AWS (DynamoDB)
# behavior, not just fast in-process logic -- tagged `integration` rather
# than the `unit` default (see tests/conftest.py's auto-tagging hook).
pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from app.domain.models import utc_now  # noqa: E402
from app.domain.watch_models import Watch, WatchStatus, WatchType  # noqa: E402
from app.services.strategy_engine import StrategyEngine  # noqa: E402
from app.services.watch_evaluator import WatchEvaluator  # noqa: E402
from tests.conftest import race_state_at_lap  # noqa: E402

TABLE_NAME = "pit-wall-race-state-test"


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


@pytest.fixture
def watch_repository(dynamodb_table):
    from app.repositories.watch_dynamodb import DynamoDBWatchRepository

    return DynamoDBWatchRepository(table_name=TABLE_NAME, region_name="us-east-1")


@pytest.fixture
def alert_repository(dynamodb_table):
    from app.repositories.watch_dynamodb import DynamoDBAlertRepository

    return DynamoDBAlertRepository(table_name=TABLE_NAME, region_name="us-east-1")


def _make_watch(*, race_id="demo-race-001", run_id="run-1", watch_id=None, status=WatchStatus.CLOSED) -> Watch:
    now = utc_now()
    return Watch(
        watch_id=watch_id or f"watch-{uuid.uuid4()}",
        race_id=race_id,
        run_id=run_id,
        type=WatchType.UNDERCUT,
        candidate="CAR_04",
        target="CAR_03",
        open_threshold=0.65,
        close_threshold=0.5,
        status=status,
        last_probability_ahead=0.4,
        last_evaluated_state_version=0,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 1. WatchRepository — create / get / update / delete / list
# ---------------------------------------------------------------------------
async def test_create_and_get_watch(watch_repository):
    watch = _make_watch()
    await watch_repository.create_watch(watch)

    fetched = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert fetched == watch


async def test_get_unknown_watch_returns_none(watch_repository):
    assert await watch_repository.get_watch("demo-race-001", "watch-does-not-exist") is None


async def test_update_watch_overwrites_the_stored_item(watch_repository):
    watch = _make_watch(status=WatchStatus.CLOSED)
    await watch_repository.create_watch(watch)

    opened = watch.model_copy(update={"status": WatchStatus.OPEN, "last_probability_ahead": 0.8})
    await watch_repository.update_watch(opened)

    fetched = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert fetched.status == WatchStatus.OPEN
    assert fetched.last_probability_ahead == 0.8


async def test_delete_watch_returns_true_then_false(watch_repository):
    watch = _make_watch()
    await watch_repository.create_watch(watch)

    assert await watch_repository.delete_watch(watch.race_id, watch.watch_id) is True
    assert await watch_repository.get_watch(watch.race_id, watch.watch_id) is None
    assert await watch_repository.delete_watch(watch.race_id, watch.watch_id) is False


async def test_list_watches_returns_all_watches_for_the_race_across_runs(watch_repository):
    run_1_watch = _make_watch(run_id="run-1")
    run_2_watch = _make_watch(run_id="run-2")
    await watch_repository.create_watch(run_1_watch)
    await watch_repository.create_watch(run_2_watch)

    listed = await watch_repository.list_watches("demo-race-001")
    assert {w.watch_id for w in listed} == {run_1_watch.watch_id, run_2_watch.watch_id}


async def test_list_active_watches_filters_by_run_id(watch_repository):
    current_run_watch = _make_watch(run_id="run-current")
    stale_run_watch = _make_watch(run_id="run-stale")
    await watch_repository.create_watch(current_run_watch)
    await watch_repository.create_watch(stale_run_watch)

    active = await watch_repository.list_active_watches("demo-race-001", "run-current")
    assert [w.watch_id for w in active] == [current_run_watch.watch_id]


async def test_watches_are_isolated_per_race(watch_repository):
    watch_a = _make_watch(race_id="race-a")
    watch_b = _make_watch(race_id="race-b")
    await watch_repository.create_watch(watch_a)
    await watch_repository.create_watch(watch_b)

    assert [w.watch_id for w in await watch_repository.list_watches("race-a")] == [watch_a.watch_id]
    assert [w.watch_id for w in await watch_repository.list_watches("race-b")] == [watch_b.watch_id]


# ---------------------------------------------------------------------------
# 2. AlertRepository — driven by the real Watch Evaluator pipeline
# ---------------------------------------------------------------------------
async def _run_real_scenario_pipeline(scenario, watch_repository, alert_repository):
    """Drives the real Gate 1/2 pipeline against the DynamoDB-backed
    repositories, mirroring test_watch_evaluator.py's real-scenario test —
    proving the whole thing works end-to-end against DynamoDB, not just
    that individual repository calls round-trip."""
    engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)
    evaluator = WatchEvaluator(watch_repository=watch_repository, alert_repository=alert_repository, strategy_engine=engine)

    run_id = str(uuid.uuid4())
    baseline = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    watch = await evaluator.create_watch(baseline, "CAR_04", "CAR_03")
    assert watch.status == WatchStatus.CLOSED

    published: list = []
    for lap, version in [(25, 1), (26, 2), (27, 3), (28, 4)]:
        state = race_state_at_lap(scenario, lap, run_id=run_id, state_version=version)
        published += await evaluator.evaluate(state)
    return evaluator, watch, published


async def test_real_scenario_watch_opens_consistently_against_dynamodb(scenario, watch_repository, alert_repository):
    _evaluator, watch, published = await _run_real_scenario_pipeline(scenario, watch_repository, alert_repository)

    assert len(published) == 1
    alert = published[0]
    assert alert.previous_status == WatchStatus.CLOSED
    assert alert.new_status == WatchStatus.OPEN
    # Spec v1.1 correction 6: the golden demo narrative's CLOSED->OPEN
    # transition fires on lap 27 (not lap 26 — see
    # scenarios/scenario-demo-01.json's watch_thresholds.open_probability,
    # tuned from 0.65 to 0.72 so this transition emerges from the
    # deterministic strategy calculation itself, not a hardcoded lap
    # check — see scripts/golden_demo.py's own per-lap assertions for the
    # full narrative this mirrors).
    assert alert.triggering_snapshot.lap == 27
    assert alert.triggering_snapshot.state_version == 3
    assert alert.deterministic_result.confidence.probability_ahead == pytest.approx(0.775, abs=1e-6)

    # Round-trips correctly through DynamoDB, not just held in memory by the
    # evaluator call above.
    fetched = await alert_repository.get_alert(alert.race_id, alert.alert_id)
    assert fetched == alert

    listed = await alert_repository.list_alerts(alert.race_id, watch_id=watch.watch_id)
    assert [a.alert_id for a in listed] == [alert.alert_id]


async def test_replaying_the_same_state_does_not_duplicate_the_alert_via_dynamodb(
    scenario, watch_repository, alert_repository
):
    """Spec 49's idempotency guarantee, enforced this time by DynamoDB's
    conditional write on the ALERTIDX item rather than the in-memory
    repository's asyncio.Lock + dict — see watch_dynamodb.py's module
    docstring for why that distinction matters (a lock only protects a
    single process; the conditional write is safe across many)."""
    evaluator, _watch, published = await _run_real_scenario_pipeline(scenario, watch_repository, alert_repository)
    assert len(published) == 1

    # Re-evaluate the exact same triggering state_version again (a replay,
    # e.g. a duplicated DynamoDB Streams event in a real deployment).
    alert = published[0]
    replay_state = alert.triggering_snapshot
    replayed = await evaluator.evaluate(replay_state)

    assert replayed == ()
    all_alerts = await alert_repository.list_alerts(alert.race_id)
    assert len(all_alerts) == 1


async def test_record_transition_returns_the_existing_alert_on_a_direct_duplicate_call(
    scenario, watch_repository, alert_repository
):
    """Exercises AlertRepository.record_transition directly (not just
    through the evaluator) with a genuinely duplicated idempotency_key, to
    prove the DynamoDB conditional-write guard itself — not just the
    evaluator's own replay-avoidance logic layered on top of it."""
    _evaluator, _watch, published = await _run_real_scenario_pipeline(scenario, watch_repository, alert_repository)
    first_alert = published[0]

    duplicate_attempt = first_alert.model_copy(update={"alert_id": f"alert-{uuid.uuid4()}"})
    returned_alert, created = await alert_repository.record_transition(
        first_alert.idempotency_key, duplicate_attempt
    )

    assert created is False
    assert returned_alert.alert_id == first_alert.alert_id  # the ORIGINAL alert, not the duplicate attempt

    all_alerts = await alert_repository.list_alerts(first_alert.race_id)
    assert len(all_alerts) == 1


# ---------------------------------------------------------------------------
# Repair 6: conditional watch updates (WatchUpdateConflict)
# ---------------------------------------------------------------------------
async def test_update_watch_conditional_write_succeeds_when_stored_version_is_lower(watch_repository):
    watch = _make_watch().model_copy(update={"last_evaluated_state_version": 2})
    await watch_repository.create_watch(watch)

    advanced = watch.model_copy(update={"last_evaluated_state_version": 5})
    result = await watch_repository.update_watch(advanced, require_last_evaluated_state_version_below=5)
    assert result.last_evaluated_state_version == 5

    stored = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored.last_evaluated_state_version == 5


async def test_update_watch_conditional_write_raises_conflict_when_stored_version_already_advanced(
    watch_repository,
):
    from app.repositories.watch_base import WatchUpdateConflict

    watch = _make_watch().model_copy(update={"last_evaluated_state_version": 5})
    await watch_repository.create_watch(watch)

    # Simulate a concurrent, faster evaluation that already advanced the
    # stored watch to state_version 5. A late writer trying to apply its
    # own (now stale) state_version=3 result must be rejected, not silently
    # overwrite the newer stored state.
    stale_write = watch.model_copy(update={"last_evaluated_state_version": 3})
    with pytest.raises(WatchUpdateConflict):
        await watch_repository.update_watch(stale_write, require_last_evaluated_state_version_below=3)

    stored = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored.last_evaluated_state_version == 5  # untouched by the rejected write
