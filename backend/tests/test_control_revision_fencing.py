"""Production-fix pass regression tests: `control_revision` fencing.

Covers the two defects fixed in this pass (see `app/repositories/base.py`'s
"Production-fix pass" docstrings on `save_control_from_simulator` and
`transition_control_from_operator` for the exact contract these tests
verify):

  1. A valid, still-current simulator leader must never overwrite an
     operator's pause: `save_control_from_simulator` now rejects a write
     whose captured `control_revision` no longer matches what is
     currently stored (a stale READ, not a stale LEASE).
  2. `transition_control_from_operator` must write the SAME
     `next_revision` to the top-level attribute, the serialized `data`
     JSON, and the returned `DemoControlState` — never derive the
     top-level value independently of the JSON blob.

Every scenario here is built deterministically — direct repository calls
with captured/stale objects, or a bounded number of real concurrent calls
via `asyncio.gather` (whose ordering moto's `transact_write_items`
serializes correctly from a single event loop) — no `asyncio.sleep`-based
timing races.

Covers scenarios 1, 5 (partial), 8, 9, 10, 11, 12, 13, 14, 15 of the
mandated regression list; see `tests/test_simulator_leadership.py` and
`tests/test_dynamodb_simulator_service.py` for the `SimulatorService`/
`_advance`-level scenarios (2, 3, 4, 6, 7) that exercise this same fencing
through the real tick loop.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app.domain.models import (
    DemoControlState,
    DriverState,
    RaceState,
    SimulatorStatus,
    TireCompound,
    TrackState,
    utc_now,
)
from app.repositories.base import StaleWriteError
from app.repositories.lease import InMemoryLeaseRepository
from app.repositories.memory import InMemoryRaceStateRepository

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.repositories.dynamodb import DynamoDBRaceStateRepository, _race_pk  # noqa: E402

RACE_ID = "demo-race-001"
TABLE_NAME = "pit-wall-control-revision-fencing-test"


def _driver(driver_id: str = "CAR_03") -> DriverState:
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


def _make_state(*, run_id: str, state_version: int, lap: int = 24) -> RaceState:
    return RaceState(
        race_id=RACE_ID,
        run_id=run_id,
        state_version=state_version,
        lap=lap,
        total_laps=58,
        race_time_ms=1000 * state_version,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id="scenario-demo-01",
        synthetic=True,
        drivers=(_driver(),),
    )


def _make_control(
    *,
    run_id: str,
    state_version: int,
    control_revision: int,
    status: SimulatorStatus = SimulatorStatus.RUNNING,
) -> DemoControlState:
    return DemoControlState(
        race_id=RACE_ID,
        run_id=run_id,
        status=status,
        scenario_id="scenario-demo-01",
        current_lap=24,
        state_version=state_version,
        updated_at=utc_now(),
        control_revision=control_revision,
    )


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_repo():
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    return repository


async def _bootstrap_memory(repository, run_id: str = "run-1") -> None:
    await repository.begin_new_run(
        RACE_ID,
        _make_state(run_id=run_id, state_version=0),
        _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.RUNNING),
    )


# 1 + 5: a simulator's captured (pre-pause) control write is rejected after
# an operator pauses, and remains rejected on a SECOND attempt with the
# same stale object — PAUSED must be durable across multiple attempts, not
# just the first.
async def test_memory_stale_captured_control_rejected_after_operator_pause(memory_repo):
    run_id = "run-1"
    await _bootstrap_memory(memory_repo, run_id=run_id)

    # The simulator "reads" RUNNING (control_revision=0) and builds its own
    # write from that captured object, exactly like `_advance` does via
    # `model_copy`.
    captured = await memory_repo.get_control(RACE_ID)
    assert captured.status == SimulatorStatus.RUNNING
    assert captured.control_revision == 0

    # The operator pauses concurrently, incrementing control_revision.
    paused = await memory_repo.transition_control_from_operator(
        RACE_ID,
        run_id,
        allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}),
        new_status=SimulatorStatus.PAUSED,
    )
    assert paused.status == SimulatorStatus.PAUSED
    assert paused.control_revision == 1

    # The simulator's in-flight write, built from the pre-pause capture,
    # must be rejected even though state_version alone would allow it.
    stale_write = captured.model_copy(update={"state_version": 1, "current_lap": 25})
    with pytest.raises(StaleWriteError):
        await memory_repo.save_control_from_simulator(stale_write)

    stored = await memory_repo.get_control(RACE_ID)
    assert stored.status == SimulatorStatus.PAUSED
    assert stored.control_revision == 1

    # A second attempt with the SAME stale captured object must also be
    # rejected — PAUSED is durable, not merely correct on the first try.
    with pytest.raises(StaleWriteError):
        await memory_repo.save_control_from_simulator(stale_write)

    stored_again = await memory_repo.get_control(RACE_ID)
    assert stored_again.status == SimulatorStatus.PAUSED
    assert stored_again.control_revision == 1


# 14: a simulator control write carrying the CURRENT control_revision
# succeeds.
async def test_memory_current_control_revision_write_succeeds(memory_repo):
    run_id = "run-1"
    await _bootstrap_memory(memory_repo, run_id=run_id)
    current = await memory_repo.get_control(RACE_ID)

    write = current.model_copy(update={"state_version": 1, "current_lap": 25})
    result = await memory_repo.save_control_from_simulator(write)
    assert result.current_lap == 25

    stored = await memory_repo.get_control(RACE_ID)
    assert stored.current_lap == 25
    assert stored.control_revision == current.control_revision


# 13: a simulator control write carrying an OLDER control_revision than
# what's currently stored is rejected.
async def test_memory_older_control_revision_rejected(memory_repo):
    run_id = "run-1"
    await _bootstrap_memory(memory_repo, run_id=run_id)

    # Advance control_revision to 2 via two operator transitions.
    await memory_repo.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}), new_status=SimulatorStatus.PAUSED
    )
    await memory_repo.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.PAUSED}), new_status=SimulatorStatus.RUNNING
    )
    current = await memory_repo.get_control(RACE_ID)
    assert current.control_revision == 2

    stale = _make_control(run_id=run_id, state_version=5, control_revision=0)
    with pytest.raises(StaleWriteError):
        await memory_repo.save_control_from_simulator(stale)

    stored = await memory_repo.get_control(RACE_ID)
    assert stored.control_revision == 2


# 11: every successful operator transition increases control_revision by
# exactly one for a single (non-concurrent) call.
async def test_memory_single_operator_transition_increases_revision_by_exactly_one(memory_repo):
    run_id = "run-1"
    await _bootstrap_memory(memory_repo, run_id=run_id)
    before = await memory_repo.get_control(RACE_ID)

    after = await memory_repo.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}), new_status=SimulatorStatus.PAUSED
    )
    assert after.control_revision == before.control_revision + 1

    stored = await memory_repo.get_control(RACE_ID)
    assert stored.control_revision == before.control_revision + 1


# 8, 9, 10: concurrent operator calls remain successful (no exception),
# idempotent, and never regress or duplicate control_revision.
async def test_memory_concurrent_start_calls_both_succeed_with_distinct_increasing_revisions(memory_repo):
    run_id = "run-1"
    # Begin from STOPPED so "start" (-> RUNNING) is a meaningful transition.
    await memory_repo.begin_new_run(
        RACE_ID,
        _make_state(run_id=run_id, state_version=0),
        _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
    )
    before = await memory_repo.get_control(RACE_ID)

    async def _start():
        return await memory_repo.transition_control_from_operator(
            RACE_ID,
            run_id,
            allowed_current_statuses=frozenset({SimulatorStatus.STOPPED, SimulatorStatus.RUNNING}),
            new_status=SimulatorStatus.RUNNING,
        )

    result_a, result_b = await asyncio.gather(_start(), _start())

    assert result_a.status == SimulatorStatus.RUNNING
    assert result_b.status == SimulatorStatus.RUNNING
    assert result_a.control_revision != result_b.control_revision
    assert result_a.control_revision > before.control_revision
    assert result_b.control_revision > before.control_revision

    final = await memory_repo.get_control(RACE_ID)
    assert final.status == SimulatorStatus.RUNNING
    assert final.control_revision >= before.control_revision + 2


async def test_memory_concurrent_pause_calls_both_succeed_with_distinct_increasing_revisions(memory_repo):
    run_id = "run-1"
    await _bootstrap_memory(memory_repo, run_id=run_id)  # status RUNNING
    before = await memory_repo.get_control(RACE_ID)

    async def _pause():
        return await memory_repo.transition_control_from_operator(
            RACE_ID,
            run_id,
            allowed_current_statuses=frozenset({SimulatorStatus.RUNNING, SimulatorStatus.PAUSED}),
            new_status=SimulatorStatus.PAUSED,
        )

    result_a, result_b = await asyncio.gather(_pause(), _pause())

    assert result_a.status == SimulatorStatus.PAUSED
    assert result_b.status == SimulatorStatus.PAUSED
    assert result_a.control_revision != result_b.control_revision
    assert result_a.control_revision > before.control_revision
    assert result_b.control_revision > before.control_revision

    final = await memory_repo.get_control(RACE_ID)
    assert final.status == SimulatorStatus.PAUSED
    assert final.control_revision >= before.control_revision + 2


# 15: reset followed by an old-run control write remains rejected via the
# run_id/GENERATION check, not because control_revision happens to
# coincidentally match (the new run's baseline control_revision is 0, same
# as a naive old-run write might carry).
async def test_memory_reset_then_old_run_control_write_with_matching_revision_rejected(memory_repo):
    old_run_id = "run-old"
    await _bootstrap_memory(memory_repo, run_id=old_run_id)
    old_control = await memory_repo.get_control(RACE_ID)
    assert old_control.control_revision == 0

    new_run_id = "run-new"
    await memory_repo.begin_new_run(
        RACE_ID,
        _make_state(run_id=new_run_id, state_version=0),
        _make_control(run_id=new_run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
    )
    # The new run's own baseline ALSO carries control_revision=0 — a write
    # from the old run carrying control_revision=0 must still be rejected
    # purely on the run_id/GENERATION mismatch, not accidentally accepted
    # because 0 == 0.
    stale_old_run_write = old_control.model_copy(update={"state_version": 1, "current_lap": 25})
    with pytest.raises(StaleWriteError):
        await memory_repo.save_control_from_simulator(stale_old_run_write)

    stored = await memory_repo.get_control(RACE_ID)
    assert stored.run_id == new_run_id
    assert stored.status == SimulatorStatus.STOPPED


# ---------------------------------------------------------------------------
# DynamoDB (moto) repository
# ---------------------------------------------------------------------------


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
        yield client


def _dynamo_repo() -> DynamoDBRaceStateRepository:
    return DynamoDBRaceStateRepository(table_name=TABLE_NAME, region_name="us-east-1")


async def _bootstrap_dynamo(repository, run_id: str = "run-1") -> None:
    await repository.begin_new_run(
        RACE_ID,
        _make_state(run_id=run_id, state_version=0),
        _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.RUNNING),
    )


# 1 + 5, DynamoDB: mirrors the in-memory test above.
@pytest.mark.integration
async def test_dynamodb_stale_captured_control_rejected_after_operator_pause(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await _bootstrap_dynamo(repository, run_id=run_id)

    captured = await repository.get_control(RACE_ID)
    assert captured.status == SimulatorStatus.RUNNING
    assert captured.control_revision == 0

    paused = await repository.transition_control_from_operator(
        RACE_ID,
        run_id,
        allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}),
        new_status=SimulatorStatus.PAUSED,
    )
    assert paused.status == SimulatorStatus.PAUSED
    assert paused.control_revision == 1

    stale_write = captured.model_copy(update={"state_version": 1, "current_lap": 25})
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(stale_write)

    stored = await repository.get_control(RACE_ID)
    assert stored.status == SimulatorStatus.PAUSED
    assert stored.control_revision == 1

    # Second attempt with the same stale object — still rejected.
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(stale_write)

    stored_again = await repository.get_control(RACE_ID)
    assert stored_again.status == SimulatorStatus.PAUSED
    assert stored_again.control_revision == 1


# 14, DynamoDB.
@pytest.mark.integration
async def test_dynamodb_current_control_revision_write_succeeds(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await _bootstrap_dynamo(repository, run_id=run_id)
    current = await repository.get_control(RACE_ID)

    write = current.model_copy(update={"state_version": 1, "current_lap": 25})
    result = await repository.save_control_from_simulator(write)
    assert result.current_lap == 25

    stored = await repository.get_control(RACE_ID)
    assert stored.current_lap == 25
    assert stored.control_revision == current.control_revision


# 13, DynamoDB.
@pytest.mark.integration
async def test_dynamodb_older_control_revision_rejected(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await _bootstrap_dynamo(repository, run_id=run_id)

    await repository.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}), new_status=SimulatorStatus.PAUSED
    )
    await repository.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.PAUSED}), new_status=SimulatorStatus.RUNNING
    )
    current = await repository.get_control(RACE_ID)
    assert current.control_revision == 2

    stale = _make_control(run_id=run_id, state_version=5, control_revision=0)
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(stale)

    stored = await repository.get_control(RACE_ID)
    assert stored.control_revision == 2


# 11, DynamoDB.
@pytest.mark.integration
async def test_dynamodb_single_operator_transition_increases_revision_by_exactly_one(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await _bootstrap_dynamo(repository, run_id=run_id)
    before = await repository.get_control(RACE_ID)

    after = await repository.transition_control_from_operator(
        RACE_ID, run_id, allowed_current_statuses=frozenset({SimulatorStatus.RUNNING}), new_status=SimulatorStatus.PAUSED
    )
    assert after.control_revision == before.control_revision + 1

    stored = await repository.get_control(RACE_ID)
    assert stored.control_revision == before.control_revision + 1


# 12: top-level attribute, serialized `data` JSON, and the returned
# DemoControlState must all report the IDENTICAL control_revision — for
# several consecutive operator transitions in a row (start, pause, start).
@pytest.mark.integration
async def test_dynamodb_control_revision_matches_top_level_json_and_returned_across_consecutive_transitions(
    dynamodb_table,
):
    repository = _dynamo_repo()
    run_id = "run-1"
    await repository.begin_new_run(
        RACE_ID,
        _make_state(run_id=run_id, state_version=0),
        _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
    )
    client = dynamodb_table  # the raw boto3 client from the fixture

    def _assert_consistent(returned: DemoControlState) -> None:
        raw = client.get_item(TableName=TABLE_NAME, Key={"pk": {"S": f"RACE#{RACE_ID}"}, "sk": {"S": "CONTROL"}})
        item = raw["Item"]
        top_level_revision = int(item["control_revision"]["N"])
        json_revision = json.loads(item["data"]["S"])["control_revision"]
        assert top_level_revision == json_revision == returned.control_revision

    transitions = [
        (frozenset({SimulatorStatus.STOPPED}), SimulatorStatus.RUNNING),
        (frozenset({SimulatorStatus.RUNNING}), SimulatorStatus.PAUSED),
        (frozenset({SimulatorStatus.PAUSED}), SimulatorStatus.RUNNING),
    ]
    previous_revision = -1
    for allowed, new_status in transitions:
        returned = await repository.transition_control_from_operator(
            RACE_ID, run_id, allowed_current_statuses=allowed, new_status=new_status
        )
        _assert_consistent(returned)
        assert returned.control_revision > previous_revision
        previous_revision = returned.control_revision

        # get_control() (which deserializes from `data`) must also agree.
        reread = await repository.get_control(RACE_ID)
        assert reread.control_revision == returned.control_revision
        assert reread.status == new_status


# 8, 9, 10, DynamoDB: real concurrency via asyncio.gather, relying on
# moto's transact_write_items serializing correctly from a single event
# loop (per the task's own note that this is safe to prove deterministically
# this way for a bounded number of calls).
@pytest.mark.integration
async def test_dynamodb_concurrent_start_calls_via_gather_never_duplicate_revision(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await repository.begin_new_run(
        RACE_ID,
        _make_state(run_id=run_id, state_version=0),
        _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
    )
    before = await repository.get_control(RACE_ID)

    async def _start():
        return await repository.transition_control_from_operator(
            RACE_ID,
            run_id,
            allowed_current_statuses=frozenset({SimulatorStatus.STOPPED, SimulatorStatus.RUNNING}),
            new_status=SimulatorStatus.RUNNING,
        )

    results = await asyncio.gather(*(_start() for _ in range(5)))

    assert all(result.status == SimulatorStatus.RUNNING for result in results)
    revisions = [result.control_revision for result in results]
    assert len(revisions) == len(set(revisions)), "two concurrent operator calls reported the same control_revision"
    assert all(revision > before.control_revision for revision in revisions)

    final = await repository.get_control(RACE_ID)
    assert final.status == SimulatorStatus.RUNNING
    assert final.control_revision == max(revisions)


@pytest.mark.integration
async def test_dynamodb_concurrent_pause_calls_via_gather_never_duplicate_revision(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    await _bootstrap_dynamo(repository, run_id=run_id)  # status RUNNING
    before = await repository.get_control(RACE_ID)

    async def _pause():
        return await repository.transition_control_from_operator(
            RACE_ID,
            run_id,
            allowed_current_statuses=frozenset({SimulatorStatus.RUNNING, SimulatorStatus.PAUSED}),
            new_status=SimulatorStatus.PAUSED,
        )

    results = await asyncio.gather(*(_pause() for _ in range(5)))

    assert all(result.status == SimulatorStatus.PAUSED for result in results)
    revisions = [result.control_revision for result in results]
    assert len(revisions) == len(set(revisions)), "two concurrent operator calls reported the same control_revision"
    assert all(revision > before.control_revision for revision in revisions)

    final = await repository.get_control(RACE_ID)
    assert final.status == SimulatorStatus.PAUSED
    assert final.control_revision == max(revisions)


# 15, DynamoDB.
@pytest.mark.integration
async def test_dynamodb_reset_then_old_run_control_write_with_matching_revision_rejected(dynamodb_table):
    repository = _dynamo_repo()
    old_run_id = "run-old"
    await _bootstrap_dynamo(repository, run_id=old_run_id)
    old_control = await repository.get_control(RACE_ID)
    assert old_control.control_revision == 0

    new_run_id = "run-new"
    await repository.begin_new_run(
        RACE_ID,
        _make_state(run_id=new_run_id, state_version=0),
        _make_control(run_id=new_run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
    )

    stale_old_run_write = old_control.model_copy(update={"state_version": 1, "current_lap": 25})
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(stale_old_run_write)

    stored = await repository.get_control(RACE_ID)
    assert stored.run_id == new_run_id
    assert stored.status == SimulatorStatus.STOPPED


# ---------------------------------------------------------------------------
# Forced, genuinely-deterministic CAS collision (repair pass, operational
# correction: proves the retry branch, not just two lucky sequential writes)
# ---------------------------------------------------------------------------
#
# The `asyncio.gather`-based concurrent tests above (e.g.
# `test_dynamodb_concurrent_start_calls_via_gather_never_duplicate_revision`)
# prove that 5 concurrent operator calls all succeed with distinct,
# increasing revisions -- but `transition_control_from_operator`'s internals
# (`get_control`, `self._client.transact_write_items`) never `await`
# anything themselves. A coroutine with no internal `await` runs to
# completion the instant it is scheduled; `asyncio.gather` never actually
# interleaves such coroutines mid-body on a single-threaded event loop, so
# each of those 5 calls in fact runs its ENTIRE read-then-write sequence
# start to finish, one after another, before the next one begins. Every one
# of those calls therefore always succeeds on attempt 1 -- the CAS retry
# branch (`continue` after a `ConditionalCheckFailed`) is never actually
# exercised by that test, even though it looks superficially like a
# concurrency test and does prove the *outcome* (distinct revisions, no
# duplication) is correct either way.
#
# This test forces the real race instead of assuming/hoping for it: two
# genuine OS threads (not two coroutines on one event loop) each run their
# own `transition_control_from_operator` call via `asyncio.run(...)` in a
# dedicated thread, and a real `threading.Barrier(2)` is installed around
# `self._client.transact_write_items` so that BOTH threads' `get_control`
# reads (which happen earlier in the same call, strictly before this patched
# method) are provably complete -- and have provably observed the SAME
# `control_revision` -- before either thread is allowed to attempt its
# write. Whichever thread's write actually lands first at the real
# DynamoDB/moto layer wins outright; the other's `ConditionExpression`
# necessarily fails against the now-stale revision it read, forcing it
# through the real `continue`-and-retry loop in
# `transition_control_from_operator` -- proven below by asserting the total
# `transact_write_items` call count is at least 3 (2 first attempts + at
# least 1 real retry), not merely counting the 2 successful outcomes.
@pytest.mark.integration
def test_dynamodb_forced_barrier_cas_collision_genuinely_exercises_the_retry_path(dynamodb_table):
    repository = _dynamo_repo()
    run_id = "run-1"
    asyncio.run(
        repository.begin_new_run(
            RACE_ID,
            _make_state(run_id=run_id, state_version=0),
            _make_control(run_id=run_id, state_version=0, control_revision=0, status=SimulatorStatus.STOPPED),
        )
    )

    real_transact_write_items = repository._client.transact_write_items
    read_write_barrier = threading.Barrier(2)
    call_lock = threading.Lock()
    call_count = 0
    barrier_consumed_by: set[int] = set()

    def _instrumented_transact_write_items(**kwargs):
        nonlocal call_count
        thread_id = threading.get_ident()
        # Each thread passes through the barrier only on its FIRST call
        # into this method (its first CAS attempt) -- this is the point,
        # within a single `transition_control_from_operator` attempt, that
        # comes strictly after that attempt's `get_control` read and
        # strictly before its write. Forcing BOTH threads to arrive here
        # before either is released proves both reads already happened,
        # and both observed the identical pre-write `control_revision`
        # (moto's in-process state has not been mutated by either thread
        # yet at this point). A thread's own RETRY attempt (its second or
        # later call here, after losing the race) is not routed through
        # the barrier again -- with only 2 parties and only one retrying
        # thread, a second `wait()` from the same thread would deadlock
        # forever waiting for a second-attempt partner that does not exist.
        if thread_id not in barrier_consumed_by:
            barrier_consumed_by.add(thread_id)
            read_write_barrier.wait(timeout=10)
        with call_lock:
            call_count += 1
            # `call_lock` also serializes the actual call into moto's own
            # `transact_write_items` implementation (not just this
            # counter). This is necessary because moto's in-process
            # backend evaluates a transaction's ConditionExpression(s) and
            # applies its writes as separate, non-atomic Python-level
            # steps with no internal locking of its own -- two real OS
            # threads calling it at truly the same instant can each see
            # the condition as still-satisfied and each apply their write,
            # which would silently defeat DynamoDB's actual conditional-
            # write atomicity guarantee (a guarantee real DynamoDB
            # enforces server-side, but which this in-process test double
            # does not reproduce under genuine concurrent access). Holding
            # this lock across the call restores that atomicity for the
            # purposes of this test without weakening what is being
            # proven: both threads still had to reach this point with the
            # IDENTICAL pre-write revision (enforced by the barrier above,
            # already released before either thread could take this lock)
            # -- the lock only ensures moto itself resolves the resulting
            # collision the same way real DynamoDB would (one write wins,
            # the other's condition now genuinely fails), rather than
            # papering over the collision by never letting it happen.
            return real_transact_write_items(**kwargs)

    repository._client.transact_write_items = _instrumented_transact_write_items

    results: dict[str, DemoControlState] = {}
    errors: dict[str, BaseException] = {}

    def _run(label: str) -> None:
        try:
            results[label] = asyncio.run(
                repository.transition_control_from_operator(
                    RACE_ID,
                    run_id,
                    allowed_current_statuses=frozenset({SimulatorStatus.STOPPED, SimulatorStatus.RUNNING}),
                    new_status=SimulatorStatus.RUNNING,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - captured and asserted on below, never silently swallowed
            errors[label] = exc

    thread_a = threading.Thread(target=_run, args=("a",), name="cas-collision-a")
    thread_b = threading.Thread(target=_run, args=("b",), name="cas-collision-b")
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=20)
    thread_b.join(timeout=20)

    assert not thread_a.is_alive() and not thread_b.is_alive(), (
        "a thread never completed -- the barrier likely deadlocked instead of both threads reaching it"
    )
    # Neither call may surface as an unhandled exception (the production
    # equivalent of an HTTP 500 from the /start endpoint) -- a genuine
    # collision must always be absorbed by the retry loop, never raised.
    assert errors == {}, f"a real CAS collision must never surface as an unhandled error / HTTP 500: {errors!r}"
    assert set(results.keys()) == {"a", "b"}

    # The actual proof that the retry branch executed: strictly more
    # transact_write_items calls were made than there were successful
    # outcomes. 2 successful outcomes with call_count == 2 would mean the
    # two calls never actually collided (each simply ran to completion
    # before the other started, exactly like the asyncio.gather tests
    # above) -- call_count >= 3 means at least one call's FIRST attempt was
    # rejected by DynamoDB's own ConditionExpression and looped back for a
    # fresh read-and-retry, driven by the real `continue` statement in
    # `transition_control_from_operator`, not by luck.
    assert call_count >= 3, (
        f"expected at least one genuine CAS retry (>=3 transact_write_items calls for 2 successful "
        f"outcomes), got call_count={call_count} -- the forced collision did not actually occur"
    )

    revisions = sorted(result.control_revision for result in results.values())
    assert revisions == [1, 2], f"expected the two transitions to finish with distinct revisions 1 and 2, got {revisions}"
    assert all(result.status == SimulatorStatus.RUNNING for result in results.values())

    # Final top-level attribute, serialized `data` JSON, and (already
    # asserted above) the values returned directly to each caller must all
    # agree on the same final revision -- no partial/inconsistent write.
    final_via_get_control = asyncio.run(repository.get_control(RACE_ID))
    assert final_via_get_control.control_revision == 2
    assert final_via_get_control.status == SimulatorStatus.RUNNING

    raw_item = repository._table.get_item(Key={"pk": _race_pk(RACE_ID), "sk": "CONTROL"}, ConsistentRead=True)["Item"]
    assert int(raw_item["control_revision"]) == 2
    assert json.loads(raw_item["data"])["control_revision"] == 2
