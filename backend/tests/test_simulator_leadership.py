"""Distributed simulator leadership tests (P0-4).

Two `SimulatorService` instances share ONE `InMemoryRaceStateRepository`
and ONE `InMemoryLeaseRepository` — the same stand-in-for-two-ECS-tasks
pattern `test_concurrent_reset_service_level.py` already uses for the
repository alone, extended here to also share the lease repository, which
is exactly what two tasks pointed at the same DynamoDB table would do.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.domain.models import RaceState, SimulatorStatus
from app.repositories.base import StaleWriteError
from app.repositories.lease import InMemoryLeaseRepository, LeaseProof
from app.repositories.memory import InMemoryRaceStateRepository
from app.services.simulator import SimulatorService
from tests.conftest import TEST_TICK_SECONDS


def _make_pair(repository, lease_repository, scenario):
    committed_a: list[RaceState] = []
    committed_b: list[RaceState] = []

    async def _record_a(state: RaceState) -> None:
        committed_a.append(state)

    async def _record_b(state: RaceState) -> None:
        committed_b.append(state)

    instance_a = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        on_state_committed=_record_a,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 4,
    )
    instance_b = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        on_state_committed=_record_b,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 4,
    )
    return instance_a, instance_b, committed_a, committed_b


@pytest.mark.asyncio
async def test_only_one_task_advances_the_race_at_a_time(scenario):
    """Both instances have their own `_run_loop` ticking (both `start()`
    called, standing in for a duplicated /start request landing on two
    different ECS tasks). Only ONE of them should ever actually commit a
    given lap advance — the loser's lease bid fails every tick, so it
    never calls `_advance` at all for as long as the other instance keeps
    renewing."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    instance_a, instance_b, committed_a, committed_b = _make_pair(repository, lease_repository, scenario)

    await instance_a.ensure_initialized()
    await instance_a.start(instance_a.race_id)
    await instance_b.start(instance_b.race_id)  # instance_b's own local _run_loop also starts

    # Let several ticks elapse on both loops.
    await asyncio.sleep(TEST_TICK_SECONDS * 8)

    await instance_a.pause(instance_a.race_id)
    await asyncio.sleep(TEST_TICK_SECONDS * 2)
    await instance_a._cancel_active_loop()  # noqa: SLF001 - test-only cleanup
    await instance_b._cancel_active_loop()  # noqa: SLF001

    # At least one advance happened (the race actually progressed)...
    assert len(committed_a) + len(committed_b) > 0
    # ...but only ONE of the two instances ever won the lease and
    # actually committed an advance -- not both fighting over it.
    assert len(committed_a) == 0 or len(committed_b) == 0


@pytest.mark.asyncio
async def test_a_crashed_leader_is_automatically_replaced_after_its_lease_expires(scenario):
    """Instance A wins the lease once, then simulates a crash (its own
    loop is cancelled without releasing the lease — a lease is never
    explicitly released, only ever left to expire, exactly like a real
    crashed ECS task). Instance B must be able to take over once the
    lease's TTL has elapsed, without any special "detect the crash"
    signal beyond its own next tick's lease bid succeeding."""
    short_ttl = TEST_TICK_SECONDS * 2
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)

    scenario_race_id = scenario.race_id
    instance_a = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
        lease_ttl_seconds=short_ttl,
    )
    await instance_a.ensure_initialized()

    # Instance A wins the lease directly (bypassing the tick loop, for a
    # deterministic "it was definitely the leader" starting point).
    grant = await lease_repository.try_acquire_or_renew(  # noqa: SLF001
        f"simulator:{scenario_race_id}", scenario_race_id, instance_a._instance_id, short_ttl  # noqa: SLF001
    )
    assert grant is not None
    assert grant.owner_id == instance_a._instance_id  # noqa: SLF001

    # Simulate instance A crashing: it never renews again.
    await asyncio.sleep(short_ttl + TEST_TICK_SECONDS)

    committed_b: list[RaceState] = []

    async def _record_b(state: RaceState) -> None:
        committed_b.append(state)

    instance_b = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        on_state_committed=_record_b,
        lease_repository=lease_repository,
        lease_ttl_seconds=short_ttl,
    )
    await instance_b.start(scenario_race_id)
    await asyncio.sleep(TEST_TICK_SECONDS * 5)
    await instance_b._cancel_active_loop()  # noqa: SLF001

    # Instance B successfully became leader and advanced the race, with
    # no coordination beyond the shared lease repository.
    assert len(committed_b) > 0
    current_lease = await lease_repository.get_lease(f"simulator:{scenario_race_id}")
    assert current_lease is not None
    assert current_lease.owner_id == instance_b._instance_id  # noqa: SLF001


class _CountingRepository(InMemoryRaceStateRepository):
    """Wraps the real in-memory repository to count `save_state` calls
    per caller-supplied label, so a test can prove a specific
    `SimulatorService` instance never attempted a write during a window
    where it did not hold the lease — the direct behavioral proof that
    "a stale leader cannot continue writing after losing leadership"
    (rather than merely inferring it from the DynamoDB conditional-write
    contract that repositories/base.py already documents and tests
    separately)."""

    def __init__(self, lease_repository: Optional[InMemoryLeaseRepository] = None) -> None:
        super().__init__(lease_repository=lease_repository)
        self.save_state_calls: list[int] = []  # state_version of every accepted save_state call

    async def save_state_from_simulator(self, state, *, lease_proof=None):  # type: ignore[override]
        result = await super().save_state_from_simulator(state, lease_proof=lease_proof)
        self.save_state_calls.append(state.state_version)
        return result


@pytest.mark.asyncio
async def test_a_stale_leader_cannot_continue_writing_after_losing_leadership(scenario):
    """Instance A holds the lease and is actively ticking. Leadership is
    then forcibly handed to instance B (simulating instance A's lease
    expiring and B winning the next bid, without waiting out a real TTL).
    From that point on, instance A's own tick loop -- which never
    explicitly learns "you lost leadership," it just keeps bidding every
    tick like any real ECS task would -- must NEVER succeed in calling
    save_state again: every one of its lease bids fails (B now holds and
    keeps renewing it), so it never even reaches its own `_advance` call,
    let alone writes."""
    lease_repository = InMemoryLeaseRepository()
    repository = _CountingRepository(lease_repository=lease_repository)
    lease_name = f"simulator:{scenario.race_id}"

    instance_a = SimulatorService(
        repository=repository, scenario=scenario, sleep_fn=asyncio.sleep, tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository, lease_ttl_seconds=TEST_TICK_SECONDS * 4,
    )
    await instance_a.ensure_initialized()
    await instance_a.start(instance_a.race_id)

    # Let instance A win the lease and advance at least once, then
    # freeze its loop (standing in for a network partition / scheduling
    # delay right after its last successful renewal -- the realistic
    # moment a takeover happens in production) so the handover below is
    # deterministic rather than racing instance A's own renewal cadence.
    await asyncio.sleep(TEST_TICK_SECONDS * 3)
    assert len(repository.save_state_calls) > 0
    calls_before_handover = len(repository.save_state_calls)
    await instance_a._cancel_active_loop()  # noqa: SLF001

    # Force instance A's most recent lease grant to look expired (it is
    # frozen and can no longer renew it itself), then have instance B win
    # the now-expired lease with a long TTL -- "another task has taken
    # over and instance A's lease has expired, permanently, from here on."
    existing = lease_repository._leases[lease_name]  # noqa: SLF001 - test-only direct manipulation to force expiry deterministically
    lease_repository._leases[lease_name] = dataclasses.replace(  # noqa: SLF001
        existing, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    instance_b_id = "instance-b-permanent-leader"
    grant = await lease_repository.try_acquire_or_renew(
        lease_name, scenario.race_id, instance_b_id, TEST_TICK_SECONDS * 1000
    )
    assert grant is not None and grant.owner_id == instance_b_id

    # Instance A "wakes back up" and resumes ticking -- exactly like a
    # real task recovering from a transient delay, with no idea it lost
    # leadership in the meantime -- via the ordinary start() path, which
    # is idempotent and safe to call again since its previous task is
    # done.
    await instance_a.start(instance_a.race_id)
    try:
        # Give instance A several more ticks -- every one of its lease
        # bids should lose to instance B's still-live, long-TTL grant.
        await asyncio.sleep(TEST_TICK_SECONDS * 8)
    finally:
        await instance_a._cancel_active_loop()  # noqa: SLF001

    assert len(repository.save_state_calls) == calls_before_handover


@pytest.mark.asyncio
async def test_supervisor_takes_over_a_crashed_leader_with_no_new_start_call(scenario):
    """AWS hackathon-final pass, phase 2: the gap the pre-phase-2 design
    had. Instance A is started via the ordinary `/start` path and crashes
    (its loop is cancelled, its lease is left to expire — same crash
    simulation as the sibling test above). Instance B is NEVER given an
    explicit `start()`/`/start` call at all — the only thing driving it is
    its own supervisor loop, exactly mirroring every backend ECS task
    running `run_supervisor` in the background regardless of which task
    (if any) ever received the original HTTP request. Instance B must
    still pick up the race and advance it, proving automatic takeover no
    longer depends on a second `/start` call landing anywhere."""
    short_ttl = TEST_TICK_SECONDS * 2
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    scenario_race_id = scenario.race_id

    instance_a = SimulatorService(
        repository=repository, scenario=scenario, sleep_fn=asyncio.sleep, tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository, lease_ttl_seconds=short_ttl,
    )
    await instance_a.start(scenario_race_id)
    await asyncio.sleep(TEST_TICK_SECONDS * 3)
    await instance_a._cancel_active_loop()  # noqa: SLF001 - simulate the process crashing outright

    committed_b: list[RaceState] = []

    async def _record_b(state: RaceState) -> None:
        committed_b.append(state)

    instance_b = SimulatorService(
        repository=repository, scenario=scenario, sleep_fn=asyncio.sleep, tick_interval_seconds=TEST_TICK_SECONDS,
        on_state_committed=_record_b, lease_repository=lease_repository, lease_ttl_seconds=short_ttl,
    )
    # Deliberately NOT calling instance_b.start()/`/start` anywhere in this
    # test — only the supervisor loop drives instance_b.
    supervisor_task = asyncio.create_task(
        instance_b.run_supervisor(scenario_race_id, poll_interval_seconds=TEST_TICK_SECONDS)
    )
    try:
        # Long enough for: instance_a's lease to expire, a supervisor poll
        # to notice control state is still RUNNING (instance_a never
        # paused/stopped it — a real crash doesn't call pause() either),
        # spin up instance_b's local loop, win the now-expired lease, and
        # commit at least one advance.
        await asyncio.sleep(short_ttl + TEST_TICK_SECONDS * 6)
    finally:
        supervisor_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass
        await instance_b._cancel_active_loop()  # noqa: SLF001

    assert len(committed_b) > 0
    current_lease = await lease_repository.get_lease(f"simulator:{scenario_race_id}")
    assert current_lease is not None
    assert current_lease.owner_id == instance_b._instance_id  # noqa: SLF001


@pytest.mark.asyncio
async def test_supervisor_does_nothing_while_paused_stopped_or_uninitialized(scenario):
    """The supervisor must not spin up a loop when there is nothing to
    supervise: no run initialized yet, or control state PAUSED/STOPPED/
    COMPLETED. Directly exercises `ensure_loop_running_if_active` (what
    `run_supervisor` calls every tick) rather than waiting out a full
    poll cycle, so this stays fast and deterministic."""
    repository = InMemoryRaceStateRepository()
    lease_repository = InMemoryLeaseRepository()
    service = SimulatorService(
        repository=repository, scenario=scenario, sleep_fn=asyncio.sleep, tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
    )

    # No run initialized yet at all.
    await service.ensure_loop_running_if_active(service.race_id)
    assert service._task is None  # noqa: SLF001

    await service.ensure_initialized()  # control status is STOPPED after this
    await service.ensure_loop_running_if_active(service.race_id)
    assert service._task is None  # noqa: SLF001

    await service.pause(service.race_id)  # no-op while STOPPED, still not RUNNING
    await service.ensure_loop_running_if_active(service.race_id)
    assert service._task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_persistence_level_fencing_rejects_a_stale_leaders_write_directly(scenario):
    """AWS hackathon-final pass, repair pass: proves fencing is enforced by
    the REPOSITORY itself, at the write boundary — not merely by the
    simulator's own lease-bid gate (which the sibling test above already
    covers at the service layer). A write carrying a LeaseProof for a
    superseded fencing token must be rejected even though it otherwise
    looks like a perfectly valid, strictly-increasing state_version write
    for the currently active run — exactly the "a delayed stale leader
    cannot write after takeover, even though its own state_version
    bookkeeping still looks fine" scenario the spec calls out. This is the
    exact-match LEASE check, not a floor: the stale leader's proof is
    rejected because it no longer matches the LIVE lease record, not
    because some historical counter moved past it."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    lease_name = f"simulator:{scenario.race_id}"
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id="run-1", state_version=0),
        _make_control(scenario, run_id="run-1", state_version=0),
    )

    # The current leader acquires the lease and writes once, establishing
    # a valid proof of leadership at fencing_token=1.
    stale_grant = await lease_repository.try_acquire_or_renew(lease_name, scenario.race_id, "leader-a", ttl_seconds=10.0)
    stale_proof = LeaseProof.from_grant(lease_name, stale_grant)
    await repository.save_state_from_simulator(_make_state(scenario, run_id="run-1", state_version=1), lease_proof=stale_proof)

    # A new leader now takes over the SAME lease (renewal by a different
    # owner is only possible once the previous grant is stale/expired in
    # real usage, but here we directly force a takeover to make the
    # "old leader still holds an old, now-superseded proof" scenario
    # deterministic): forcibly expire the existing grant, then let a new
    # owner acquire it.
    existing = lease_repository._leases[lease_name]  # noqa: SLF001 - test-only direct manipulation
    lease_repository._leases[lease_name] = dataclasses.replace(  # noqa: SLF001
        existing, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    new_grant = await lease_repository.try_acquire_or_renew(lease_name, scenario.race_id, "leader-b", ttl_seconds=10.0)
    assert new_grant is not None and new_grant.owner_id == "leader-b"
    new_proof = LeaseProof.from_grant(lease_name, new_grant)

    # The stale leader ("leader-a") attempts to write using its now-old
    # proof, with a state_version that is still strictly greater than
    # what's stored (2 > 1) — by state_version alone this would be
    # accepted, but the lease-proof check must reject it outright because
    # the LIVE lease item no longer names leader-a as owner.
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_make_state(scenario, run_id="run-1", state_version=2), lease_proof=stale_proof)

    # The real current leader's correctly-fenced write still succeeds.
    await repository.save_state_from_simulator(_make_state(scenario, run_id="run-1", state_version=2), lease_proof=new_proof)

    # save_control applies the identical fencing rule.
    with pytest.raises(StaleWriteError):
        await repository.save_control_from_simulator(_make_control(scenario, run_id="run-1", state_version=2), lease_proof=stale_proof)
    await repository.save_control_from_simulator(_make_control(scenario, run_id="run-1", state_version=2), lease_proof=new_proof)


@pytest.mark.asyncio
async def test_reset_to_a_new_run_id_still_rejects_old_run_writes(scenario):
    """A reset to a new run_id must NOT reopen the door for an old-run
    leader's stale write: even though the lease-proof design has no
    per-run "floor" to reset, `begin_new_run`'s own active_run_id check
    (part of the same atomic verification) must still reject a write
    carrying the OLD run_id, regardless of how valid its lease proof
    looks, once a new run has begun."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    lease_name = f"simulator:{scenario.race_id}"
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id="run-1", state_version=0),
        _make_control(scenario, run_id="run-1", state_version=0),
    )

    grant = await lease_repository.try_acquire_or_renew(lease_name, scenario.race_id, "leader-a", ttl_seconds=10.0)
    proof = LeaseProof.from_grant(lease_name, grant)
    await repository.save_state_from_simulator(_make_state(scenario, run_id="run-1", state_version=1), lease_proof=proof)

    # A reset establishes run-2. The lease itself is race-scoped, not
    # run-scoped, so the SAME lease proof remains technically "live" --
    # but the write still targets the now-superseded run_id "run-1" and
    # must be rejected on that basis.
    await repository.begin_new_run(
        scenario.race_id,
        _make_state(scenario, run_id="run-2", state_version=0),
        _make_control(scenario, run_id="run-2", state_version=0),
    )
    with pytest.raises(StaleWriteError):
        await repository.save_state_from_simulator(_make_state(scenario, run_id="run-1", state_version=2), lease_proof=proof)

    # A write against the NEW run_id, using the same still-live lease
    # proof, is accepted.
    await repository.save_state_from_simulator(_make_state(scenario, run_id="run-2", state_version=1), lease_proof=proof)


@pytest.mark.asyncio
async def test_two_active_simulator_instances_never_commit_duplicate_laps(scenario):
    """Direct proof of the property the whole fencing design exists to
    guarantee: with two `SimulatorService` instances sharing one
    repository and one lease repository, both ticking concurrently for an
    extended period (well past several lease renewal cycles, so ownership
    has every opportunity to bounce back and forth), the sequence of
    committed state_versions across BOTH instances combined is strictly
    increasing with no repeats -- no lap is ever committed twice, by
    either instance, at any point."""
    lease_repository = InMemoryLeaseRepository()
    # Wire the SAME lease repository into the state repository so this
    # test exercises real fenced writes, matching production DynamoDB
    # behavior (require_lease_proof=True there) rather than the unfenced
    # local-development default.
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    instance_a, instance_b, committed_a, committed_b = _make_pair(repository, lease_repository, scenario)

    await instance_a.ensure_initialized()
    await instance_a.start(instance_a.race_id)
    await instance_b.start(instance_b.race_id)

    # Run long enough for leadership to plausibly bounce between the two
    # instances several times via renewal/expiry timing jitter.
    await asyncio.sleep(TEST_TICK_SECONDS * 20)

    await instance_a._cancel_active_loop()  # noqa: SLF001
    await instance_b._cancel_active_loop()  # noqa: SLF001

    all_versions = [state.state_version for state in committed_a] + [state.state_version for state in committed_b]
    assert len(all_versions) > 0
    assert len(all_versions) == len(set(all_versions)), "a state_version was committed more than once across instances"


class _PauseInjectingRepository:
    """Wraps a real `RaceStateRepository` so that the FIRST call to
    `get_control` after construction triggers a caller-supplied async
    callback BEFORE delegating to the real `get_control` — a deterministic
    way to force "an operator's pause lands exactly between the
    simulator's state write and its post-write control re-read" without
    any sleep-based timing. `SimulatorService._advance` calls
    `get_control` exactly once per (non-final-lap) tick, immediately
    after its state write has already committed (see
    `app/services/simulator.py`'s own "Production-fix pass" comment on
    `_advance`) — this hook fires at exactly that read."""

    def __init__(self, inner, on_first_get_control):
        self._inner = inner
        self._on_first_get_control = on_first_get_control
        self._triggered = False

    async def get_control(self, race_id):
        if not self._triggered:
            self._triggered = True
            await self._on_first_get_control()
        return await self._inner.get_control(race_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_pause_landing_during_in_flight_tick_is_preserved_not_overwritten_to_running(scenario):
    """Defect 1 regression, service level (scenarios 2 & 3): the simulator
    reads RUNNING, commits its state write, and — before it re-reads
    control — an operator's pause lands (deterministically forced via
    `_PauseInjectingRepository`, not a sleep-based race). The fresh
    control read `_advance` performs afterwards must see PAUSED and
    PRESERVE it (the fix), not force it back to RUNNING. The simulator's
    lease is genuinely still valid throughout this whole call (never
    superseded/expired) — proving this exercises the fresh-read fix, not
    the pre-existing stale-LEADER protection (a different code path,
    covered separately below and in test_dynamodb_simulator_service.py)."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 1000,  # long TTL: never expires mid-test
    )
    await service.ensure_initialized()
    run_id = service._active_run_id  # noqa: SLF001

    # Start the race (STOPPED -> RUNNING) directly through the repository
    # so this test can drive `_advance` itself, deterministically, without
    # a background tick loop also running.
    await repository.transition_control_from_operator(
        service.race_id,
        run_id,
        allowed_current_statuses=frozenset({SimulatorStatus.STOPPED}),
        new_status=SimulatorStatus.RUNNING,
    )

    # The simulator wins a genuinely valid, long-lived, unexpired lease.
    lease_name = f"simulator:{service.race_id}"
    grant = await lease_repository.try_acquire_or_renew(
        lease_name, service.race_id, service._instance_id, TEST_TICK_SECONDS * 1000  # noqa: SLF001
    )
    assert grant is not None
    lease_proof = LeaseProof.from_grant(lease_name, grant)

    async def _operator_pauses() -> None:
        paused = await service.pause(service.race_id)
        assert paused.status == SimulatorStatus.PAUSED

    service._repository = _PauseInjectingRepository(repository, _operator_pauses)  # noqa: SLF001

    advanced = await service._advance(service.race_id, run_id, lease_proof=lease_proof)  # noqa: SLF001
    assert advanced is True  # the tick itself was accepted; not rejected as a stale leader

    stored_state = await repository.get_state(service.race_id)
    assert stored_state.lap == 25  # the state write still committed

    stored_control = await repository.get_control(service.race_id)
    assert stored_control.status == SimulatorStatus.PAUSED  # preserved, not forced back to RUNNING
    assert stored_control.current_lap == 25  # bookkeeping fields still brought forward
    assert stored_control.state_version == stored_state.state_version


@pytest.mark.asyncio
async def test_advance_level_stale_leader_with_superseded_lease_is_rejected(scenario):
    """Scenario 4: existing coverage (this file's own
    `test_persistence_level_fencing_rejects_a_stale_leaders_write_directly`
    and `test_dynamodb_simulator_service.py`'s
    `test_paused_state_not_overwritten_by_stale_in_flight_tick`) exercises
    this at the raw repository level. This test confirms the same
    protection holds one layer up, driving `SimulatorService._advance`
    itself: a leader whose lease proof has been superseded by a newer
    leader's own acquisition must have its `_advance` call fail closed —
    `advanced` is False and nothing is written — even though its captured
    state_version still looks individually valid."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 4,
    )
    await service.ensure_initialized()
    run_id = service._active_run_id  # noqa: SLF001
    await repository.transition_control_from_operator(
        service.race_id,
        run_id,
        allowed_current_statuses=frozenset({SimulatorStatus.STOPPED}),
        new_status=SimulatorStatus.RUNNING,
    )

    lease_name = f"simulator:{service.race_id}"
    stale_grant = await lease_repository.try_acquire_or_renew(
        lease_name, service.race_id, service._instance_id, TEST_TICK_SECONDS  # noqa: SLF001
    )
    assert stale_grant is not None
    stale_proof = LeaseProof.from_grant(lease_name, stale_grant)

    # A new leader takes over once the lease above has expired.
    await asyncio.sleep(TEST_TICK_SECONDS * 3)
    new_grant = await lease_repository.try_acquire_or_renew(lease_name, service.race_id, "leader-b", 30.0)
    assert new_grant is not None and new_grant.owner_id == "leader-b"

    before_state = await repository.get_state(service.race_id)
    advanced = await service._advance(service.race_id, run_id, lease_proof=stale_proof)  # noqa: SLF001
    assert advanced is False

    after_state = await repository.get_state(service.race_id)
    assert after_state.lap == before_state.lap  # nothing advanced
    assert after_state.state_version == before_state.state_version


@pytest.mark.asyncio
async def test_resume_after_pause_allows_advancing_again(scenario):
    """Scenario 7: pause, then start again (RUNNING is reachable from
    PAUSED via `start()`'s own `allowed_current_statuses`) — status
    returns to RUNNING and the tick loop can actually advance the race
    again afterwards, not merely report the right status."""
    lease_repository = InMemoryLeaseRepository()
    repository = InMemoryRaceStateRepository(lease_repository=lease_repository)
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
        lease_repository=lease_repository,
        lease_ttl_seconds=TEST_TICK_SECONDS * 20,
    )
    await service.ensure_initialized()
    await service.start(service.race_id)
    # `_run_loop` always sleeps a full tick interval before its first
    # check, so pausing immediately (no sleep at all) deterministically
    # lands before the loop's first advance attempt — avoiding any race
    # against the scenario completing (only 4 laps remain).
    paused = await service.pause(service.race_id)
    assert paused.status == SimulatorStatus.PAUSED
    lap_at_pause = (await repository.get_state(service.race_id)).lap

    await asyncio.sleep(TEST_TICK_SECONDS * 3)
    stalled = await repository.get_state(service.race_id)
    assert stalled.lap == lap_at_pause  # confirms it really is stopped, not just slow

    resumed = await service.start(service.race_id)
    assert resumed.status == SimulatorStatus.RUNNING

    advanced = False
    for _ in range(80):
        await asyncio.sleep(TEST_TICK_SECONDS)
        state = await repository.get_state(service.race_id)
        if state.lap > lap_at_pause:
            advanced = True
            break
    assert advanced, "simulator never advanced again after resume"

    await service._cancel_active_loop()  # noqa: SLF001


def _make_state(scenario, *, run_id: str, state_version: int) -> RaceState:
    from app.services.simulator import _build_race_state

    return _build_race_state(
        scenario,
        scenario.laps[min(state_version, len(scenario.laps) - 1)],
        race_id=scenario.race_id,
        run_id=run_id,
        state_version=state_version,
        generated_at=datetime.now(timezone.utc),
    )


def _make_control(scenario, *, run_id: str, state_version: int):
    from app.domain.models import DemoControlState, SimulatorStatus

    return DemoControlState(
        race_id=scenario.race_id,
        run_id=run_id,
        status=SimulatorStatus.RUNNING,
        scenario_id=scenario.scenario_id,
        current_lap=scenario.laps[0].lap,
        state_version=state_version,
        updated_at=datetime.now(timezone.utc),
    )
