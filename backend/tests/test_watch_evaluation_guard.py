"""Tests for the v1.1 spec "repair 6" guard: a watch is evaluated only if

    race_state.run_id == watch.run_id
    AND race_state.state_version > watch.last_evaluated_state_version

and, if either check fails, evaluation returns without calculation, alert
creation or watch update. This must hold for duplicated, delayed and
out-of-order state_versions, and for both the in-memory and DynamoDB
repository backends (repair 6 explicitly requires "in-memory and DynamoDB
tests for duplicated, delayed and out-of-order state versions").

The in-process guard lives in WatchEvaluator.evaluate() (watch_evaluator.py)
and is backend-agnostic — it runs identically regardless of which
WatchRepository/AlertRepository implementation is wired in. These tests
therefore parametrize the SAME scenarios over both backends via a fixture,
rather than duplicating the test bodies.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.watch_models import WatchStatus
from app.repositories.watch_base import WatchUpdateConflict
from app.services.watch_evaluator import WatchEvaluator
from tests.conftest import race_state_at_lap

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

TABLE_NAME = "pit-wall-guard-test"


def _make_memory_repos():
    from app.repositories.watch_memory import InMemoryAlertRepository, InMemoryWatchRepository

    return InMemoryWatchRepository(), InMemoryAlertRepository()


def _make_dynamodb_repos():
    from app.repositories.watch_dynamodb import DynamoDBAlertRepository, DynamoDBWatchRepository

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
    return (
        DynamoDBWatchRepository(table_name=TABLE_NAME, region_name="us-east-1"),
        DynamoDBAlertRepository(table_name=TABLE_NAME, region_name="us-east-1"),
    )


@pytest.fixture(params=["memory", "dynamodb"])
def repo_backend(request):
    """Yields (watch_repository, alert_repository) for each backend in
    turn, so every test in this file runs against both without
    duplication. DynamoDB is wrapped in a fresh `mock_aws()` per test."""
    if request.param == "memory":
        yield _make_memory_repos()
    else:
        with mock_aws():
            yield _make_dynamodb_repos()


@pytest.fixture
def evaluator(strategy_engine, repo_backend):
    watch_repository, alert_repository = repo_backend
    return WatchEvaluator(
        watch_repository=watch_repository, alert_repository=alert_repository, strategy_engine=strategy_engine
    )


async def _open_watch(scenario, evaluator, run_id: str):
    """Creates a watch at lap 24 (CLOSED baseline) and evaluates lap 27
    (CLOSED->OPEN, per the canonical scenario's documented lap-27 threshold
    crossing) so `watch.last_evaluated_state_version == 3`. Returns
    (watch, alerts_from_lap_27)."""
    baseline = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    watch = await evaluator.create_watch(baseline, "CAR_04", "CAR_03")
    assert watch.last_evaluated_state_version == 0

    state_27 = race_state_at_lap(scenario, 27, run_id=run_id, state_version=3)
    alerts = await evaluator.evaluate(state_27)
    return watch, alerts


async def test_duplicate_state_version_is_a_pure_noop(scenario, evaluator, repo_backend):
    """Replaying the SAME state_version a second time (e.g. an
    at-least-once DynamoDB Streams redelivery) must not recompute, must
    not create a second alert, and must not touch the watch."""
    watch_repository, _alert_repository = repo_backend
    run_id = str(uuid.uuid4())
    watch, first_alerts = await _open_watch(scenario, evaluator, run_id)
    assert len(first_alerts) == 1
    assert first_alerts[0].new_status == WatchStatus.OPEN

    stored_before = await watch_repository.get_watch(watch.race_id, watch.watch_id)

    # Exact duplicate redelivery: same state_version (3) again.
    duplicate_state = race_state_at_lap(scenario, 27, run_id=run_id, state_version=3)
    second_alerts = await evaluator.evaluate(duplicate_state)

    assert second_alerts == ()  # no new alert published
    stored_after = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_after.last_evaluated_state_version == stored_before.last_evaluated_state_version
    assert stored_after.status == stored_before.status
    assert stored_after.updated_at == stored_before.updated_at  # never rewritten


async def test_delayed_older_state_version_is_rejected(scenario, evaluator, repo_backend):
    """A DELAYED network response carrying an OLDER state_version than one
    already evaluated (e.g. lap 25 arriving late, after lap 27 was already
    processed) must be rejected outright — it must never regress the
    watch's last_evaluated_state_version or re-open/re-close it based on
    stale data."""
    watch_repository, _alert_repository = repo_backend
    run_id = str(uuid.uuid4())
    watch, _ = await _open_watch(scenario, evaluator, run_id)  # advances to state_version 3, OPEN

    stored_before = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_before.last_evaluated_state_version == 3
    assert stored_before.status == WatchStatus.OPEN

    # Delayed lap-25 response (state_version=1), arriving AFTER lap 27 was
    # already evaluated. The scenario's own gap at lap 25 (2.9s, still
    # above open_threshold's crossing point) would not by itself flip
    # anything back, but the point of this test is that the guard rejects
    # it on state_version alone, before the deterministic model is even
    # invoked, so replaying old data can NEVER move the watch, regardless
    # of what that old data says.
    delayed_state = race_state_at_lap(scenario, 25, run_id=run_id, state_version=1)
    alerts = await evaluator.evaluate(delayed_state)

    assert alerts == ()
    stored_after = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored_after.last_evaluated_state_version == 3  # unchanged, NOT regressed to 1
    assert stored_after.status == WatchStatus.OPEN  # unchanged


async def test_out_of_order_delivery_only_the_newest_wins(scenario, evaluator, repo_backend):
    """Two state updates arrive with their state_versions swapped in
    delivery order (newer one processed first, older one processed
    second, both for the same run). The final watch state must reflect
    only the genuinely newest state_version ever evaluated, never the
    later-arriving-but-numerically-older one."""
    watch_repository, _alert_repository = repo_backend
    run_id = str(uuid.uuid4())
    baseline = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    watch = await evaluator.create_watch(baseline, "CAR_04", "CAR_03")

    newer_state = race_state_at_lap(scenario, 27, run_id=run_id, state_version=3)
    older_state = race_state_at_lap(scenario, 26, run_id=run_id, state_version=2)

    # Newer arrives FIRST (out of order).
    newer_alerts = await evaluator.evaluate(newer_state)
    assert len(newer_alerts) == 1

    # Older arrives SECOND, out of order — must be rejected, not applied.
    older_alerts = await evaluator.evaluate(older_state)
    assert older_alerts == ()

    stored = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored.last_evaluated_state_version == 3  # the newer one, not regressed to 2
    assert stored.status == WatchStatus.OPEN


async def test_a_different_run_id_is_never_evaluated_even_with_a_higher_state_version(
    scenario, evaluator, repo_backend
):
    """A race state from a DIFFERENT (e.g. previous, pre-reset) run_id must
    never be evaluated against this watch, no matter how high its
    state_version is — run isolation, not just version isolation."""
    watch_repository, _alert_repository = repo_backend
    run_id_a = str(uuid.uuid4())
    run_id_b = str(uuid.uuid4())
    watch, _ = await _open_watch(scenario, evaluator, run_id_a)  # watch belongs to run_id_a

    other_run_state = race_state_at_lap(scenario, 27, run_id=run_id_b, state_version=99)
    alerts = await evaluator.evaluate(other_run_state)

    # list_active_watches(race_id, run_id_b) would already exclude this
    # watch (it belongs to run_id_a), so there is nothing to evaluate —
    # confirmed here by the watch being completely untouched.
    assert alerts == ()
    stored = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored.last_evaluated_state_version == 3


async def test_update_watch_conflict_is_raised_directly_at_the_repository_layer(scenario):
    """Exercises WatchRepository.update_watch's conditional-write contract
    directly (not through the evaluator, which never triggers this itself
    in-process since its own guard runs first) — for BOTH backends, proving
    the repository-level defense works independently of the evaluator."""
    for repo_factory in (_make_memory_repos, None):
        if repo_factory is not None:
            watch_repository, _alert_repository = repo_factory()
        else:
            with mock_aws():
                watch_repository, _alert_repository = _make_dynamodb_repos()
                await _assert_conflict_behavior(watch_repository)
            continue
        await _assert_conflict_behavior(watch_repository)


async def _assert_conflict_behavior(watch_repository):
    from app.domain.watch_models import Watch, WatchStatus, WatchType
    from app.domain.models import utc_now

    now = utc_now()
    watch = Watch(
        watch_id=f"watch-{uuid.uuid4()}",
        race_id="demo-race-001",
        run_id="run-1",
        type=WatchType.UNDERCUT,
        candidate="CAR_04",
        target="CAR_03",
        open_threshold=0.65,
        close_threshold=0.5,
        status=WatchStatus.CLOSED,
        last_probability_ahead=0.4,
        last_evaluated_state_version=5,
        created_at=now,
        updated_at=now,
    )
    await watch_repository.create_watch(watch)

    stale_write = watch.model_copy(update={"last_evaluated_state_version": 3})
    with pytest.raises(WatchUpdateConflict):
        await watch_repository.update_watch(stale_write, require_last_evaluated_state_version_below=3)

    stored = await watch_repository.get_watch(watch.race_id, watch.watch_id)
    assert stored.last_evaluated_state_version == 5
