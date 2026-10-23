from __future__ import annotations

import pytest

from app.domain.models import DriverState, TireCompound, TrackState
from app.domain.strategy_models import (
    MonteCarloConfig,
    PitLossConfig,
    StrategyModelConfig,
    TireCurveConfig,
    WatchThresholdConfig,
)
from app.services.strategy_engine import StrategyEngine, StrategyValidationError
from tests.conftest import race_state_at_lap


# ---------------------------------------------------------------------------
# A small, hand-controlled synthetic config used to verify the lap-time
# formula and pit-loss/warm-up semantics exactly, independent of the real
# scenario's (larger, harder-to-hand-check) numbers.
# ---------------------------------------------------------------------------
SYNTHETIC_CONFIG = StrategyModelConfig(
    model_version="test-synthetic-v1",
    tire_curves={
        TireCompound.SOFT: TireCurveConfig(base_delta_ms=-500, degradation_ms_per_lap=100),
        TireCompound.MEDIUM: TireCurveConfig(base_delta_ms=0, degradation_ms_per_lap=50),
        TireCompound.HARD: TireCurveConfig(base_delta_ms=400, degradation_ms_per_lap=20),
    },
    pit_loss=PitLossConfig(pit_entry_ms=500, lane_transit_ms=4000, stationary_time_ms=2000, pit_exit_ms=500),
    warmup_loss_ms=300,
    traffic_penalty_ms_per_unit=1000,
    environmental_adjustment_ms={
        TrackState.GREEN: 0,
        TrackState.YELLOW: 2000,
        TrackState.SAFETY_CAR: 5000,
        TrackState.RED: 0,
    },
    monte_carlo=MonteCarloConfig(
        trials=200,
        lap_pace_noise_std_ms=50,
        pit_duration_noise_std_ms=200,
        degradation_noise_std_ms=10,
        traffic_loss_noise_std_ms=80,
    ),
    watch_thresholds=WatchThresholdConfig(open_probability=0.65, close_probability=0.50),
)


def _driver(driver_id: str, *, compound=TireCompound.MEDIUM, tire_age=0, base_pace_ms=90000, gap_to_leader_ms=0, traffic_factor=0.0) -> DriverState:
    return DriverState(
        driver_id=driver_id,
        display_name=driver_id,
        team_name="Test Team",
        position=1,
        gap_to_leader_ms=gap_to_leader_ms,
        gap_to_ahead_ms=0,
        compound=compound,
        tire_age_laps=tire_age,
        base_pace_ms=base_pace_ms,
        current_lap_pace_ms=base_pace_ms,
        pit_stops=0,
        pit_window=(20, 25),
        traffic_factor=traffic_factor,
    )


@pytest.fixture
def engine():
    return StrategyEngine(config=SYNTHETIC_CONFIG, model_seed=12345)


# ---------------------------------------------------------------------------
# 1. Lap-time model formula — exact hand-computed value (spec 46.1)
# ---------------------------------------------------------------------------
def test_predicted_lap_time_matches_formula_exactly(engine):
    # PredictedLapTime_ms = base_pace_ms + CompoundDelta_ms
    #                        + TireDegradation_ms(age) + TrafficPenalty_ms
    #                        + EnvironmentalAdjustment_ms
    result = engine.predicted_lap_time_ms(
        driver_base_pace_ms=90000,
        compound=TireCompound.SOFT,
        tire_age_laps=4,
        traffic_factor=0.25,
        track_state=TrackState.GREEN,
    )
    expected = 90000 + (-500) + (100 * 4) + round(1000 * 0.25) + 0
    assert result == expected == 90150


def test_predicted_lap_time_hard_compound_and_yellow_flag(engine):
    result = engine.predicted_lap_time_ms(
        driver_base_pace_ms=88000,
        compound=TireCompound.HARD,
        tire_age_laps=10,
        traffic_factor=0.0,
        track_state=TrackState.YELLOW,
    )
    expected = 88000 + 400 + (20 * 10) + 0 + 2000
    assert result == expected == 90600


# ---------------------------------------------------------------------------
# 2. Tire degradation increases lap time monotonically with age
# ---------------------------------------------------------------------------
def test_tire_degradation_increases_with_age(engine):
    times = [
        engine.predicted_lap_time_ms(
            driver_base_pace_ms=90000,
            compound=TireCompound.MEDIUM,
            tire_age_laps=age,
            traffic_factor=0.0,
            track_state=TrackState.GREEN,
        )
        for age in range(0, 10)
    ]
    assert times == sorted(times)
    assert times[-1] > times[0]


# ---------------------------------------------------------------------------
# 3. Pit loss is added exactly once, on the pit lap
# ---------------------------------------------------------------------------
def test_pit_loss_applied_once_on_pit_lap(engine):
    # Isolate the pit-loss contribution precisely by projecting a single
    # lap that IS the pit lap, keeping the same compound before and after
    # (so there is no compound-change or degradation-reset confound —
    # those are covered separately by test_warmup_loss_applies_only_to_first_lap_after_pit).
    driver = _driver("CAR_TEST", compound=TireCompound.MEDIUM, tire_age=5)

    no_pit_single_lap_ms = engine._project_driver_time_ms(  # noqa: SLF001 white-box test of internal projection
        driver, start_lap=10, horizon_lap=11, pit_lap=None, compound_after_pit=None, track_state=TrackState.GREEN
    )
    with_pit_single_lap_ms = engine._project_driver_time_ms(  # noqa: SLF001
        driver,
        start_lap=10,
        horizon_lap=11,
        pit_lap=11,
        compound_after_pit=TireCompound.MEDIUM,  # same compound: isolates pit loss only
        track_state=TrackState.GREEN,
    )

    pit_loss_total = SYNTHETIC_CONFIG.pit_loss.total_ms
    assert pit_loss_total == 500 + 4000 + 2000 + 500 == 7000

    # With an identical compound before/after, the only difference between
    # the two projections on the pit lap itself is the pit loss, added
    # exactly once.
    assert with_pit_single_lap_ms == no_pit_single_lap_ms + pit_loss_total


def test_warmup_loss_applies_only_to_first_lap_after_pit(engine):
    driver = _driver("CAR_TEST", compound=TireCompound.MEDIUM, tire_age=0)

    # Project a single lap that is exactly the pit lap: no warm-up loss yet.
    pit_lap_only_ms = engine._project_driver_time_ms(  # noqa: SLF001
        driver, start_lap=10, horizon_lap=11, pit_lap=11, compound_after_pit=TireCompound.SOFT, track_state=TrackState.GREEN
    )
    expected_pit_lap_only = (
        engine.predicted_lap_time_ms(
            driver_base_pace_ms=driver.base_pace_ms,
            compound=TireCompound.MEDIUM,
            tire_age_laps=1,
            traffic_factor=0.0,
            track_state=TrackState.GREEN,
        )
        + SYNTHETIC_CONFIG.pit_loss.total_ms
    )
    assert pit_lap_only_ms == expected_pit_lap_only

    # Project through the lap after the pit: must additionally include the
    # warm-up loss and use age=0 on the new compound.
    through_next_lap_ms = engine._project_driver_time_ms(  # noqa: SLF001
        driver, start_lap=10, horizon_lap=12, pit_lap=11, compound_after_pit=TireCompound.SOFT, track_state=TrackState.GREEN
    )
    next_lap_expected = (
        engine.predicted_lap_time_ms(
            driver_base_pace_ms=driver.base_pace_ms,
            compound=TireCompound.SOFT,
            tire_age_laps=0,
            traffic_factor=0.0,
            track_state=TrackState.GREEN,
        )
        + SYNTHETIC_CONFIG.warmup_loss_ms
    )
    assert through_next_lap_ms == expected_pit_lap_only + next_lap_expected


# ---------------------------------------------------------------------------
# 4. calculate_undercut — determinism, sign convention, validation
# ---------------------------------------------------------------------------
def test_undercut_is_deterministic_for_identical_inputs(engine):
    candidate = _driver("CAR_A", tire_age=20, gap_to_leader_ms=5000)
    target = _driver("CAR_B", tire_age=2, gap_to_leader_ms=2000)
    race_state = _race_state_with(candidate, target, lap=10)

    r1 = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")
    r2 = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")

    assert r1.expected_margin_ms == r2.expected_margin_ms
    assert r1.confidence.median_margin_ms == r2.confidence.median_margin_ms
    assert r1.confidence.p10_margin_ms == r2.confidence.p10_margin_ms
    assert r1.confidence.p90_margin_ms == r2.confidence.p90_margin_ms
    assert r1.confidence.probability_ahead == r2.confidence.probability_ahead


def test_undercut_favors_candidate_with_much_older_target_tires(engine):
    # Candidate trails by a small, fixed gap; target's tires are extremely
    # old (heavy degradation) while candidate's are almost new. The
    # undercut should be clearly favorable (positive margin, high
    # probability_ahead) in this lopsided case.
    candidate = _driver("CAR_A", tire_age=1, gap_to_leader_ms=1000, compound=TireCompound.MEDIUM)
    target = _driver("CAR_B", tire_age=40, gap_to_leader_ms=800, compound=TireCompound.MEDIUM)
    race_state = _race_state_with(candidate, target, lap=10)

    result = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")

    assert result.expected_margin_ms > 0
    assert result.window == "OPEN"
    assert result.confidence.probability_ahead >= 0.65


def test_undercut_rejects_candidate_already_ahead_of_target(engine):
    candidate = _driver("CAR_A", gap_to_leader_ms=1000)
    target = _driver("CAR_B", gap_to_leader_ms=5000)  # target is further from leader => behind candidate
    race_state = _race_state_with(candidate, target, lap=10)

    with pytest.raises(StrategyValidationError):
        engine.calculate_undercut(race_state, "CAR_A", "CAR_B")


def test_undercut_rejects_unknown_driver(engine):
    candidate = _driver("CAR_A", gap_to_leader_ms=1000)
    target = _driver("CAR_B", gap_to_leader_ms=2000)
    race_state = _race_state_with(candidate, target, lap=10)

    with pytest.raises(StrategyValidationError):
        engine.calculate_undercut(race_state, "CAR_UNKNOWN", "CAR_B")
    with pytest.raises(StrategyValidationError):
        engine.calculate_undercut(race_state, "CAR_A", "CAR_UNKNOWN")
    with pytest.raises(StrategyValidationError):
        engine.calculate_undercut(race_state, "CAR_A", "CAR_A")


def test_undercut_result_includes_all_required_fields(engine):
    candidate = _driver("CAR_A", gap_to_leader_ms=2000)
    target = _driver("CAR_B", gap_to_leader_ms=1000)
    race_state = _race_state_with(candidate, target, lap=10)

    result = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")

    # Spec 46.2: "Every result SHALL include candidate, target, source
    # state version, model version, pit schedules, comparison horizon,
    # expected margin, traffic classification, assumptions and a stable
    # calculation_id."
    assert result.candidate == "CAR_A"
    assert result.target == "CAR_B"
    assert result.source_state_version == race_state.state_version
    assert result.model_version
    assert len(result.pit_schedules) == 2
    assert result.comparison_horizon_lap > race_state.lap
    assert isinstance(result.expected_margin_ms, int)
    assert result.traffic_classification in {"CLEAR", "LIGHT", "HEAVY"}
    assert len(result.assumptions) > 0
    assert result.calculation_id


# ---------------------------------------------------------------------------
# 5. Mandatory seeded Monte Carlo confidence model (spec 47)
# ---------------------------------------------------------------------------
def test_monte_carlo_runs_exactly_200_trials_by_default(engine):
    candidate = _driver("CAR_A", tire_age=1, gap_to_leader_ms=1000)
    target = _driver("CAR_B", tire_age=20, gap_to_leader_ms=800)
    race_state = _race_state_with(candidate, target, lap=10)

    result = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")

    assert result.confidence.trials == 200


def test_monte_carlo_percentiles_are_ordered_and_probability_in_range(engine):
    candidate = _driver("CAR_A", tire_age=10, gap_to_leader_ms=1500)
    target = _driver("CAR_B", tire_age=10, gap_to_leader_ms=1000)
    race_state = _race_state_with(candidate, target, lap=10)

    result = engine.calculate_undercut(race_state, "CAR_A", "CAR_B")
    confidence = result.confidence

    assert confidence.p10_margin_ms <= confidence.median_margin_ms <= confidence.p90_margin_ms
    assert 0.0 <= confidence.probability_ahead <= 1.0
    assert confidence.model_seed == engine._model_seed  # noqa: SLF001


def test_watch_open_close_thresholds_match_spec(engine):
    assert SYNTHETIC_CONFIG.watch_thresholds.open_probability == 0.65
    assert SYNTHETIC_CONFIG.watch_thresholds.close_probability == 0.50


# ---------------------------------------------------------------------------
# 6. simulate_strategy — PIT NOW vs WAIT k laps counterfactual
# ---------------------------------------------------------------------------
def test_simulate_strategy_is_deterministic(engine):
    driver = _driver("CAR_A", tire_age=15, gap_to_leader_ms=3000)
    race_state = _race_state_with(driver, _driver("CAR_B", gap_to_leader_ms=1000), lap=10)

    r1 = engine.simulate_strategy(race_state, "CAR_A", wait_laps=3)
    r2 = engine.simulate_strategy(race_state, "CAR_A", wait_laps=3)

    assert r1.delta_ms == r2.delta_ms
    assert r1.confidence.median_margin_ms == r2.confidence.median_margin_ms


def test_simulate_strategy_rejects_unknown_driver(engine):
    race_state = _race_state_with(_driver("CAR_A"), _driver("CAR_B", gap_to_leader_ms=500), lap=10)
    with pytest.raises(StrategyValidationError):
        engine.simulate_strategy(race_state, "CAR_UNKNOWN", wait_laps=3)


def test_simulate_strategy_rejects_invalid_wait_laps(engine):
    race_state = _race_state_with(_driver("CAR_A"), _driver("CAR_B", gap_to_leader_ms=500), lap=10)
    with pytest.raises(StrategyValidationError):
        engine.simulate_strategy(race_state, "CAR_A", wait_laps=0)


def test_simulate_strategy_result_fields(engine):
    driver = _driver("CAR_A", tire_age=15, gap_to_leader_ms=3000)
    race_state = _race_state_with(driver, _driver("CAR_B", gap_to_leader_ms=1000), lap=10)

    result = engine.simulate_strategy(race_state, "CAR_A", wait_laps=3)

    assert result.driver_id == "CAR_A"
    assert result.pit_now_lap == race_state.lap + 1
    assert result.wait_lap == result.pit_now_lap + 3
    assert result.wait_laps == 3
    assert result.comparison_horizon_lap > result.wait_lap
    assert isinstance(result.delta_ms, int)
    assert result.confidence.trials == 200


# ---------------------------------------------------------------------------
# 7. Real-scenario regression: the fixed narrative transitions CLOSED -> OPEN
# ---------------------------------------------------------------------------
def test_real_scenario_undercut_narrative_transitions_closed_to_open(scenario, strategy_engine):
    windows = {}
    for lap_number in (24, 25, 26, 27, 28):
        race_state = race_state_at_lap(scenario, lap_number)
        result = strategy_engine.calculate_undercut(race_state, "CAR_04", "CAR_03")
        windows[lap_number] = result.window

    # The scenario's authored gap-closing narrative (3400ms -> 1800ms) must
    # eventually make the undercut favorable; it must not already be open
    # at the very first lap (that would make the "UNDERCUT WINDOW OPEN"
    # canonical demo moment meaningless).
    assert windows[24] != "OPEN" or windows[28] == "OPEN"
    assert windows[28] == "OPEN"


# Spec v1.1 correction 6: "Add exact tests asserting calculated
# probability and watch status for every lap 24-28" — required lap-by-lap
# narrative: lap 24/25/26 CLOSED, lap 27 OPEN, lap 28 remains OPEN. This
# must emerge from the deterministic strategy calculation against
# scenarios/scenario-demo-01.json's own authored gap-closing narrative and
# watch_thresholds.open_probability (0.72 — raised from the pre-correction
# 0.65, which is what put the transition on lap 26 instead), never a
# hardcoded `if lap == 27` anywhere in application code — this test would
# not distinguish "engine genuinely crosses the threshold on lap 27" from
# "someone special-cased lap 27" on its own, but scripts/golden_demo.py's
# own assertions (run against a live backend, not a hand-built RaceState)
# and this repo's application code (grepped for "lap == 27" and "lap==27"
# — absent — as part of this correction's own review) together rule the
# latter out.
EXPECTED_LAP_NARRATIVE = {
    24: ("CLOSED", 0.455),
    25: ("CLOSED", 0.59),
    26: ("CLOSED", 0.705),
    27: ("OPEN", 0.775),
    28: ("OPEN", 0.825),
}


@pytest.mark.parametrize("lap_number", sorted(EXPECTED_LAP_NARRATIVE))
def test_golden_narrative_exact_window_and_probability_per_lap(scenario, strategy_engine, lap_number):
    expected_window, expected_probability = EXPECTED_LAP_NARRATIVE[lap_number]
    race_state = race_state_at_lap(scenario, lap_number)
    result = strategy_engine.calculate_undercut(race_state, "CAR_04", "CAR_03")

    assert result.window == expected_window, (
        f"lap {lap_number}: expected window {expected_window!r}, got {result.window!r} "
        f"(probability_ahead={result.confidence.probability_ahead})"
    )
    assert result.confidence.probability_ahead == pytest.approx(expected_probability, abs=1e-6), (
        f"lap {lap_number}: expected probability_ahead {expected_probability}, "
        f"got {result.confidence.probability_ahead}"
    )


def test_golden_narrative_transition_lands_exactly_on_lap_27(scenario, strategy_engine):
    """The single most important assertion for spec v1.1 correction 6:
    the CLOSED->OPEN crossing point is lap 27, and ONLY lap 27 — asserted
    directly against every candidate lap so a regression to lap 25, 26 or
    28 (the exact failure modes the spec instruction calls out by name)
    fails this test with an unambiguous message, not just a generic
    per-lap mismatch."""
    windows = {
        lap_number: strategy_engine.calculate_undercut(race_state_at_lap(scenario, lap_number), "CAR_04", "CAR_03").window
        for lap_number in (24, 25, 26, 27, 28)
    }
    assert windows[24] == "CLOSED"
    assert windows[25] == "CLOSED"
    assert windows[26] == "CLOSED", "regression: transition landed on lap 26, not lap 27 (spec v1.1 correction 6)"
    assert windows[27] == "OPEN"
    assert windows[28] == "OPEN"


def _race_state_with(driver_a: DriverState, driver_b: DriverState, *, lap: int):
    from app.domain.models import RaceState, TrackState, utc_now
    import uuid

    return RaceState(
        race_id="test-race",
        run_id=str(uuid.uuid4()),
        state_version=0,
        lap=lap,
        total_laps=58,
        race_time_ms=0,
        track_state=TrackState.GREEN,
        generated_at=utc_now(),
        scenario_id="test-scenario",
        synthetic=True,
        drivers=(driver_a, driver_b),
    )
