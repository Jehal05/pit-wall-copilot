"""Tests for `TransitionWriter` (spec v1.1 correction 9: DynamoDB alert
atomicity review).

Background this file exists to prove: the ORIGINAL two-step sequence
(`AlertRepository.record_transition()` followed by a separate
`WatchRepository.update_watch()` call) left a real crash-recovery gap
against a DURABLE backend — if the process died between the two writes,
the alert would be durably committed but the watch would durably keep its
OLD status/last_evaluated_state_version. The alert is safely idempotent
against a retry of the exact SAME state_version (the idempotency_key is
stable), but NOT against the next, genuinely newer state_version: it would
be evaluated against the still-stale watch and could recompute and
durably record a second, duplicate alert for a transition that already
fired. `TransitionWriter` (watch_base.py) closes this by committing the
idempotency marker, the alert, and the watch update together, atomically —
`InMemoryTransitionWriter` for local/test single-process safety (no crash
window exists there — a process crash wipes everything symmetrically) and
`DynamoDBTransitionWriter` via a single `TransactWriteItems` call for real
durable storage.

These tests exercise `TransitionWriter.write_transition` directly (not
through `WatchEvaluator`) so the atomic contract itself — independent of
the evaluator's own hysteresis/guard logic, which is already covered by
test_watch_evaluator.py and test_watch_evaluation_guard.py — is verified
for both backends.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.models import utc_now
from app.domain.watch_models import Watch, WatchStatus, WatchType

# Spec v1.1 pass 3, correction 10: exercises moto-simulated AWS (DynamoDB)
# behavior, not just fast in-process logic -- tagged `integration` rather
# than the `unit` default (see tests/conftest.py's auto-tagging hook).
# Repair pass (repo-wide Ruff cleanup): moved below the imports above (pure
# reordering, no behavior change) -- see test_active_run_protection.py's
# matching comment for why.
pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

TABLE_NAME = "pit-wall-transition-writer-test"


def _make_watch(*, race_id="demo-race-001", run_id="run-1", watch_id=None, last_evaluated_state_version=0) -> Watch:
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
        status=WatchStatus.CLOSED,
        last_probability_ahead=0.4,
        last_evaluated_state_version=last_evaluated_state_version,
        created_at=now,
        updated_at=now,
    )


async def _make_alert(watch: Watch, *, state_version: int, new_status=WatchStatus.OPEN, scenario=None):
    """Builds a minimal-but-real Alert against the real scenario/strategy
    pipeline so `Alert.model_validate_json` round-trips exactly like
    production data (a hand-built Alert with placeholder nested objects
    would not exercise the same serialization path)."""
    from tests.conftest import race_state_at_lap
    from app.services.strategy_engine import StrategyEngine
    from app.domain.watch_models import Alert

    engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)
    state = race_state_at_lap(scenario, 27, run_id=watch.run_id, state_version=state_version)
    result = engine.calculate_undercut(state, watch.candidate, watch.target)
    idempotency_key = f"{watch.run_id}:{watch.watch_id}:{state_version}:{new_status.value}"
    return Alert(
        alert_id=f"alert-{uuid.uuid4()}",
        evaluation_id=f"eval-{uuid.uuid4()}",
        watch_id=watch.watch_id,
        race_id=watch.race_id,
        run_id=watch.run_id,
        state_version=state_version,
        previous_status=watch.status,
        new_status=new_status,
        triggering_snapshot=state,
        deterministic_result=result,
        assumptions=result.assumptions,
        model_version=result.model_version,
        idempotency_key=idempotency_key,
        created_at=utc_now(),
    ), idempotency_key


def _make_memory_writer():
    from app.repositories.watch_memory import InMemoryAlertRepository, InMemoryTransitionWriter, InMemoryWatchRepository

    watch_repository = InMemoryWatchRepository()
    alert_repository = InMemoryAlertRepository()
    writer = InMemoryTransitionWriter(watch_repository, alert_repository)
    return watch_repository, alert_repository, writer


def _make_dynamodb_writer():
    from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBTransitionWriter, DynamoDBWatchRepository

    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    watch_repository = DynamoDBWatchRepository(table_name=TABLE_NAME, region_name="us-east-1")
    alert_repository = DynamoDBAlertRepository(table_name=TABLE_NAME, region_name="us-east-1")
    writer = DynamoDBTransitionWriter(table_name=TABLE_NAME, region_name="us-east-1")
    return watch_repository, alert_repository, writer


@pytest.fixture(params=["memory", "dynamodb"])
def writer_backend(request):
    """Yields (watch_repository, alert_repository, transition_writer) for
    each backend, so every test below runs against both without
    duplication."""
    if request.param == "memory":
        yield _make_memory_writer()
    else:
        with mock_aws():
            yield _make_dynamodb_writer()


async def test_write_transition_commits_alert_and_watch_together(scenario, writer_backend):
    watch_repository, alert_repository, writer = writer_backend
    watch = _make_watch(last_evaluated_state_version=0)
    await watch_repository.create_watch(watch)

    alert, idempotency_key = await _make_alert(watch, state_version=3, scenario=scenario)
    updated_watch = watch.model_copy(update={"status": WatchStatus.OPEN, "last_evaluated_state_version": 3})

    published, created = await writer.write_transition(
        idempotency_key, alert, updated_watch, require_last_evaluated_state_version_below=3
    )

    assert created is True
    assert published == alert

    stored_watch = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_watch.status == WatchStatus.OPEN
    assert stored_watch.last_evaluated_state_version == 3

    stored_alert = await alert_repository.get_alert(alert.race_id, alert.alert_id)
    assert stored_alert == alert


async def test_write_transition_is_idempotent_on_replay(scenario, writer_backend):
    """A duplicate delivery of the SAME idempotency_key (e.g. a re-driven
    DynamoDB Streams batch) must return the EXISTING alert, created=False,
    and must NOT touch the watch a second time."""
    watch_repository, alert_repository, writer = writer_backend
    watch = _make_watch(last_evaluated_state_version=0)
    await watch_repository.create_watch(watch)

    alert, idempotency_key = await _make_alert(watch, state_version=3, scenario=scenario)
    updated_watch = watch.model_copy(update={"status": WatchStatus.OPEN, "last_evaluated_state_version": 3})

    first_published, first_created = await writer.write_transition(
        idempotency_key, alert, updated_watch, require_last_evaluated_state_version_below=3
    )
    assert first_created is True

    stored_watch_before_replay = await watch_repository.get_watch(watch.race_id, watch.watch_id)

    # Replay: identical idempotency_key, a freshly-constructed (but
    # logically identical) alert/updated_watch pair, exactly as a retried
    # evaluation would produce.
    replay_published, replay_created = await writer.write_transition(
        idempotency_key, alert, updated_watch, require_last_evaluated_state_version_below=3
    )

    assert replay_created is False
    assert replay_published == first_published  # the ORIGINAL alert, not a new one

    stored_watch_after_replay = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_watch_after_replay.updated_at == stored_watch_before_replay.updated_at  # untouched

    all_alerts = await alert_repository.list_alerts(watch.race_id)
    assert len(all_alerts) == 1  # never duplicated


async def test_write_transition_rolls_back_everything_when_the_watch_condition_fails(scenario, writer_backend):
    """A genuinely NEW idempotency_key, but the watch has ALREADY been
    advanced past this call's baseline by a concurrent/newer evaluation:
    the whole write must roll back — NEITHER the alert NOR the idempotency
    marker may be left behind. This is the core atomicity guarantee this
    class exists for: with the old two-step sequence, the alert half could
    commit even when the watch half was about to be rejected, which is
    exactly the crash-recovery gap this module's docstring (and
    watch_base.py's TransitionWriter class docstring) describes."""
    watch_repository, alert_repository, writer = writer_backend
    watch = _make_watch(last_evaluated_state_version=0)
    await watch_repository.create_watch(watch)

    # Simulate a concurrent/newer evaluation that already advanced the
    # watch to state_version 5 before this call's write lands.
    newer_watch = watch.model_copy(update={"last_evaluated_state_version": 5, "status": WatchStatus.OPEN})
    await watch_repository.update_watch(newer_watch)

    # This call still believes the watch is at version 0 and tries to
    # advance it to version 3 — stale relative to what's actually stored.
    alert, idempotency_key = await _make_alert(watch, state_version=3, scenario=scenario)
    updated_watch = watch.model_copy(update={"status": WatchStatus.OPEN, "last_evaluated_state_version": 3})

    published, created = await writer.write_transition(
        idempotency_key, alert, updated_watch, require_last_evaluated_state_version_below=3
    )

    assert published is None
    assert created is False

    # Nothing was left behind: not the alert, not the idempotency marker.
    # This is the property a two-step write cannot guarantee.
    assert await alert_repository.get_alert(alert.race_id, alert.alert_id) is None
    all_alerts = await alert_repository.list_alerts(alert.race_id)
    assert all_alerts == ()

    # The watch is untouched — still showing the newer evaluation's state.
    stored_watch = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_watch.last_evaluated_state_version == 5


async def test_a_rolled_back_write_can_be_cleanly_retried_with_a_fresh_idempotency_key(scenario, writer_backend):
    """After a rollback (previous test), nothing was partially committed,
    so a LATER evaluation for a genuinely newer state_version (a fresh
    idempotency_key) commits cleanly — proving the rollback really did
    leave a clean slate, not a half-written one a later call would trip
    over."""
    watch_repository, alert_repository, writer = writer_backend
    watch = _make_watch(last_evaluated_state_version=0)
    await watch_repository.create_watch(watch)

    newer_watch = watch.model_copy(update={"last_evaluated_state_version": 5, "status": WatchStatus.OPEN})
    await watch_repository.update_watch(newer_watch)

    stale_alert, stale_key = await _make_alert(watch, state_version=3, scenario=scenario)
    stale_updated_watch = watch.model_copy(update={"status": WatchStatus.OPEN, "last_evaluated_state_version": 3})
    _published, created = await writer.write_transition(
        stale_key, stale_alert, stale_updated_watch, require_last_evaluated_state_version_below=3
    )
    assert created is False  # rolled back, per the previous test

    # A fresh, genuinely newer attempt (state_version 6) against the
    # now-current watch baseline (version 5) must succeed cleanly.
    fresh_alert, fresh_key = await _make_alert(
        watch.model_copy(update={"last_evaluated_state_version": 5, "status": WatchStatus.OPEN}),
        state_version=6,
        new_status=WatchStatus.CLOSED,
        scenario=scenario,
    )
    fresh_updated_watch = watch.model_copy(update={"status": WatchStatus.CLOSED, "last_evaluated_state_version": 6})
    published, created = await writer.write_transition(
        fresh_key, fresh_alert, fresh_updated_watch, require_last_evaluated_state_version_below=6
    )

    assert created is True
    assert published == fresh_alert
    stored_watch = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_watch.last_evaluated_state_version == 6
    assert stored_watch.status == WatchStatus.CLOSED


async def test_watch_evaluator_uses_the_transition_writer_when_configured(scenario, writer_backend):
    """End-to-end: WatchEvaluator, wired with a transition_writer (as
    repositories/factory.py's build_transition_writer + app/main.py always
    do in real deployments), goes through the atomic path for a
    status-changing evaluation — not the old two-step
    record_transition()+update_watch() sequence. Confirms the wiring
    itself (not just the writer in isolation) is exercised correctly."""
    watch_repository, alert_repository, writer = writer_backend
    from app.services.strategy_engine import StrategyEngine
    from app.services.watch_evaluator import WatchEvaluator
    from tests.conftest import race_state_at_lap

    engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)
    evaluator = WatchEvaluator(
        watch_repository=watch_repository,
        alert_repository=alert_repository,
        strategy_engine=engine,
        transition_writer=writer,
    )

    run_id = str(uuid.uuid4())
    baseline = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    watch = await evaluator.create_watch(baseline, "CAR_04", "CAR_03")
    assert watch.last_evaluated_state_version == 0

    later_state = race_state_at_lap(scenario, 27, run_id=run_id, state_version=3)
    alerts = await evaluator.evaluate(later_state)

    assert len(alerts) == 1
    assert alerts[0].new_status == WatchStatus.OPEN

    stored_watch = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_watch.status == WatchStatus.OPEN
    assert stored_watch.last_evaluated_state_version == 3

    # Replaying the exact same state_version must remain a pure no-op —
    # the same guarantee test_watch_evaluation_guard.py already covers for
    # the non-atomic path, re-verified here for the atomic one.
    replayed = await evaluator.evaluate(later_state)
    assert replayed == ()
    all_alerts = await alert_repository.list_alerts(watch.race_id)
    assert len(all_alerts) == 1
