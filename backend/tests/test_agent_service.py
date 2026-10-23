"""Unit tests for spec v1.1 repair 9: AgentQueryService and
compute_race_changes/RaceChangeTracker.

These test the pure logic directly against synthetic RaceState objects
(via conftest.py's `race_state_at_lap` helper), independent of the HTTP
layer — REST-contract coverage lives in tests/test_agent_api.py.
"""

from __future__ import annotations

from app.domain.models import DriverState, TireCompound, TrackState
from app.services.agent_service import (
    AgentQueryService,
    RaceChangeTracker,
    compute_race_changes,
    narrate_race_changes,
)
from app.services.regulation_service import RegulationService
from app.config import DEFAULT_KNOWLEDGE_BASE_DIR, DEFAULT_REGULATIONS_DIR

from tests.conftest import race_state_at_lap


def _regulation_service() -> RegulationService:
    return RegulationService(DEFAULT_REGULATIONS_DIR, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)


# ---------------------------------------------------------------------------
# AgentQueryService
# ---------------------------------------------------------------------------


def test_gap_query_is_supported_with_race_state_evidence(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "What is CAR_03's gap to the leader?")
    assert result.status == "SUPPORTED"
    assert result.confidence == 1.0
    assert len(result.evidence) == 1
    assert result.evidence[0].type == "race_state_field"
    assert result.evidence[0].source_id == state.race_id
    assert "CAR_03" in result.evidence[0].field


def test_leader_query_is_supported(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "Who is leading the race?")
    assert result.status == "SUPPORTED"
    leader = min(state.drivers, key=lambda d: d.position)
    assert leader.display_name in result.narrative


def test_regulation_backed_query_is_supported_via_regulation_service(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "What tire compounds are available: soft, medium, hard?")
    assert result.status == "SUPPORTED"
    assert result.confidence == 0.9
    assert all(item.type == "regulation" for item in result.evidence)


def test_regulation_source_unavailable_gives_the_exact_degraded_message(scenario, tmp_path):
    # Spec v1.1 repair 14 / Section 29 "Knowledge Base unavailable": when
    # RegulationService.is_available is False (corpus directory missing),
    # the agent must return the EXACT specified text, not the generic
    # "I don't have information..." unsupported-query wording — these are
    # different failure modes with different honest things to say.
    state = race_state_at_lap(scenario, 25)
    unavailable_regulation_service = RegulationService(tmp_path / "does-not-exist", repo_root=tmp_path)
    assert unavailable_regulation_service.is_available is False

    service = AgentQueryService(regulation_service=unavailable_regulation_service)
    # A query that does not match any race-state-field pattern, so it
    # falls through to the regulation lookup.
    result = service.query(state, "What tire compounds are available: soft, medium, hard?")

    assert result.status == "UNSUPPORTED"
    assert result.confidence == 0.0
    assert result.narrative == "Regulation source unavailable. I won't infer the rule."
    assert result.evidence == ()


def test_unanswerable_query_is_unsupported_with_no_evidence(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "What is the capital of France?")
    assert result.status == "UNSUPPORTED"
    assert result.confidence == 0.0
    assert result.evidence == ()


def test_hallucination_trap_query_is_unsupported(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "Does the tire degradation rule change or get ignored after lap 40?")
    assert result.status == "UNSUPPORTED"
    assert result.evidence == ()


def test_empty_query_is_unsupported(scenario):
    state = race_state_at_lap(scenario, 25)
    service = AgentQueryService(regulation_service=_regulation_service())
    result = service.query(state, "   ")
    assert result.status == "UNSUPPORTED"


# ---------------------------------------------------------------------------
# compute_race_changes / narrate_race_changes
# ---------------------------------------------------------------------------


def test_no_previous_state_yields_no_changes(scenario):
    current = race_state_at_lap(scenario, 25)
    changes = compute_race_changes(None, current)
    assert changes == ()


def test_different_run_id_yields_no_changes(scenario):
    previous = race_state_at_lap(scenario, 24, run_id="run-a")
    current = race_state_at_lap(scenario, 25, run_id="run-b")
    changes = compute_race_changes(previous, current)
    assert changes == ()


def test_gap_change_is_detected_and_ranked(scenario):
    previous = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    current = race_state_at_lap(scenario, 25, run_id="run-a", state_version=1)
    changes = compute_race_changes(previous, current)
    gap_changes = [c for c in changes if c.field == "gap_change_ms"]
    # The synthetic scenario's lap-24 -> lap-25 transition moves gaps for
    # at least one car (this is asserted generically — the scenario's
    # exact numbers are not this test's concern, only that a real,
    # non-zero gap change on a real lap transition is detected at all).
    assert len(gap_changes) >= 1
    for delta in changes:
        assert delta.significance > 0


def test_pit_stop_is_detected_as_undercut_state_change(scenario):
    previous_driver = DriverState(
        driver_id="CAR_99", display_name="Test Car", team_name="Test Team", position=1,
        gap_to_leader_ms=0, gap_to_ahead_ms=0, compound=TireCompound.MEDIUM, tire_age_laps=10,
        base_pace_ms=90000, current_lap_pace_ms=90000, pit_stops=0, pit_window=(20, 30), traffic_factor=0.0,
    )
    current_driver = previous_driver.model_copy(update={"compound": TireCompound.SOFT, "tire_age_laps": 0, "pit_stops": 1})

    base = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    previous = base.model_copy(update={"drivers": (previous_driver,)})
    current = base.model_copy(update={"drivers": (current_driver,), "state_version": 1})

    changes = compute_race_changes(previous, current)
    pit_changes = [c for c in changes if c.field == "undercut_state_change"]
    assert len(pit_changes) == 1
    assert pit_changes[0].driver_id == "CAR_99"
    # A pit stop must be the single most significant change detected.
    assert changes[0].field == "undercut_state_change"


def test_track_state_change_is_detected(scenario):
    previous = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    current = previous.model_copy(update={"track_state": TrackState.SAFETY_CAR, "state_version": 1})
    changes = compute_race_changes(previous, current)
    track_changes = [c for c in changes if c.field == "track_state_change"]
    assert len(track_changes) == 1
    assert track_changes[0].driver_id is None
    assert track_changes[0].previous_value == "GREEN"
    assert track_changes[0].current_value == "SAFETY_CAR"


def test_narration_covers_only_top_three_changes(scenario):
    previous = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    current = previous.model_copy(update={"track_state": TrackState.SAFETY_CAR, "state_version": 1})
    changes = compute_race_changes(previous, current)
    narrative, explained = narrate_race_changes(changes)
    assert len(explained) <= 3
    assert explained == changes[: len(explained)]
    assert narrative  # non-empty


def test_no_changes_gives_a_clear_no_change_narrative(scenario):
    previous = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    current = previous.model_copy(update={"state_version": 1})
    changes = compute_race_changes(previous, current)
    assert changes == ()
    narrative, explained = narrate_race_changes(changes)
    assert explained == ()
    assert "No changes" in narrative


# ---------------------------------------------------------------------------
# RaceChangeTracker
# ---------------------------------------------------------------------------


async def test_tracker_records_first_state_with_no_previous(scenario):
    tracker = RaceChangeTracker()
    state = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    await tracker.record(state)
    previous, current = tracker.get_previous_and_current(state.race_id)
    assert previous is None
    assert current == state


async def test_tracker_advances_previous_on_second_commit(scenario):
    tracker = RaceChangeTracker()
    state_1 = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    state_2 = race_state_at_lap(scenario, 25, run_id="run-a", state_version=1)
    await tracker.record(state_1)
    await tracker.record(state_2)
    previous, current = tracker.get_previous_and_current(state_1.race_id)
    assert previous == state_1
    assert current == state_2


async def test_tracker_ignores_duplicate_and_older_state_versions(scenario):
    tracker = RaceChangeTracker()
    state_1 = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    state_2 = race_state_at_lap(scenario, 25, run_id="run-a", state_version=2)
    stale = race_state_at_lap(scenario, 25, run_id="run-a", state_version=1)
    await tracker.record(state_1)
    await tracker.record(state_2)
    await tracker.record(stale)  # older version than current (2) — must be ignored
    previous, current = tracker.get_previous_and_current(state_1.race_id)
    assert previous == state_1
    assert current == state_2  # unchanged by the stale delivery


async def test_tracker_discards_previous_on_new_run_id(scenario):
    tracker = RaceChangeTracker()
    state_a1 = race_state_at_lap(scenario, 24, run_id="run-a", state_version=0)
    state_a2 = race_state_at_lap(scenario, 25, run_id="run-a", state_version=1)
    state_b1 = race_state_at_lap(scenario, 24, run_id="run-b", state_version=0)  # a reset
    await tracker.record(state_a1)
    await tracker.record(state_a2)
    await tracker.record(state_b1)
    previous, current = tracker.get_previous_and_current(state_a1.race_id)
    assert previous is None  # reset run has no valid predecessor to diff against
    assert current == state_b1


# ---------------------------------------------------------------------------
# Undercut strategy questions (local-demo-fixed repair, issue 2): routed to the
# real StrategyEngine; every asserted value comes from the engine, never a
# hard-coded number.
# ---------------------------------------------------------------------------

import re  # noqa: E402

import pytest  # noqa: E402


def _service_with_strategy(strategy_engine) -> AgentQueryService:
    return AgentQueryService(regulation_service=_regulation_service(), strategy_engine=strategy_engine)


def _evidence(result) -> dict:
    return {item.field: item.value for item in result.evidence}


def test_undercut_question_is_supported_and_matches_the_engine(scenario, strategy_engine):
    state = race_state_at_lap(scenario, 28)
    expected = strategy_engine.calculate_undercut(state, "CAR_04", "CAR_03")
    result = _service_with_strategy(strategy_engine).query(state, "Should CAR_04 pit now to undercut CAR_03?")
    assert result.status == "SUPPORTED"
    ev = _evidence(result)
    assert ev["candidate"] == "CAR_04" and ev["target"] == "CAR_03"
    assert ev["window"] == str(expected.window)
    assert ev["expected_margin_ms"] == str(expected.expected_margin_ms)
    assert ev["probability_ahead"] == str(expected.confidence.probability_ahead)
    assert ev["source_state_version"] == str(expected.source_state_version)
    for schedule in expected.pit_schedules:
        assert f"pit_lap={schedule.pit_lap}" in ev[f"pit_schedule.{schedule.driver_id}"]


@pytest.mark.parametrize(
    "question",
    [
        "Can CAR_04 undercut CAR_03?",
        "Is the undercut window open for CAR_04 against CAR_03?",
        "What is CAR_04's chance of getting ahead of CAR_03?",
        "should car_04 pit now to undercut car_03?",
    ],
)
def test_undercut_alternate_wordings_and_case(scenario, strategy_engine, question):
    state = race_state_at_lap(scenario, 28)
    result = _service_with_strategy(strategy_engine).query(state, question)
    assert result.status == "SUPPORTED"
    assert _evidence(result)["candidate"] == "CAR_04"


@pytest.mark.parametrize(
    "question",
    [
        "Can CAR_99 undercut CAR_03?",
        "Can CAR_04 undercut CAR_98?",
        "Can CAR_04 undercut?",
        "Can CAR_04 undercut CAR_04?",
        "Can CAR_03 undercut CAR_04?",  # candidate already ahead
    ],
)
def test_undercut_invalid_questions_are_unsupported(scenario, strategy_engine, question):
    state = race_state_at_lap(scenario, 28)
    result = _service_with_strategy(strategy_engine).query(state, question)
    assert result.status == "UNSUPPORTED"
    assert result.evidence == ()


def test_undercut_without_engine_and_unrelated_question_refuse_safely(scenario, strategy_engine):
    state = race_state_at_lap(scenario, 28)
    no_engine = AgentQueryService(regulation_service=_regulation_service())
    assert no_engine.query(state, "Can CAR_04 undercut CAR_03?").status == "UNSUPPORTED"
    assert _service_with_strategy(strategy_engine).query(state, "What is the capital of France?").status == "UNSUPPORTED"


def test_undercut_narrative_contains_no_invented_numbers(scenario, strategy_engine):
    state = race_state_at_lap(scenario, 28)
    expected = strategy_engine.calculate_undercut(state, "CAR_04", "CAR_03")
    result = _service_with_strategy(strategy_engine).query(state, "Can CAR_04 undercut CAR_03?")
    allowed = {
        "04", "03", str(expected.expected_margin_ms), str(expected.confidence.trials),
        f"{expected.confidence.probability_ahead * 100:.1f}",
    } | {str(s.pit_lap) for s in expected.pit_schedules}
    numbers = set(re.findall(r"\d+(?:\.\d+)?", result.narrative.replace("CAR_", "")))
    assert numbers <= allowed, numbers - allowed
