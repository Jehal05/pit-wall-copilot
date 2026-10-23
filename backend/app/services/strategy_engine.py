"""Deterministic strategy engine — Gate 1.

Implements spec v1.1 sections 15-17 (Strategy Engine / Undercut Calculation
/ Uncertainty) and the precise contract added in section 46 (Deterministic
Strategy Calculation Contract) and section 47 (Mandatory Seeded Confidence
Model).

Hard requirements this module upholds:
  * All calculation is plain, deterministic application code. No LLM is
    involved anywhere in this file (spec 15: "They SHALL NOT be calculated
    by Bedrock.").
  * All internal time values are integer milliseconds; display/seconds
    formatting is left to the API/UI boundary (spec 46.1).
  * The Monte Carlo confidence model is mandatory (not optional) and uses
    a fixed, seeded pseudo-random sequence so a demo run is repeatable
    (spec 47, spec 17).
"""

from __future__ import annotations

import random
import statistics
import uuid
from typing import Optional

from app.domain.models import DriverState, RaceState, TireCompound, TrackState
from app.domain.strategy_models import (
    STRATEGY_MODEL_VERSION,
    ConfidenceResult,
    PitSchedule,
    StrategyModelConfig,
    StrategySimulationResult,
    UndercutResult,
    WatchThresholdConfig,
)

DEFAULT_TARGET_RESPONSE_DELAY_LAPS = 1  # "the target responds on lap N+1" (spec 46.2)
DEFAULT_WAIT_LAPS = 3  # matches the canonical UJ-3 example: "waits 3 laps"
COMPARISON_HORIZON_PADDING_LAPS = 2  # comparison_horizon_lap = max(pit laps) + 2 (spec 46.2)


class StrategyValidationError(ValueError):
    """Raised when a strategy calculation is requested with invalid or
    out-of-domain inputs (unknown driver, candidate already ahead, etc.).
    This is a deterministic, explicit rejection — never a guess."""


class StrategyEngine:
    """Stateless deterministic calculator, constructed once per scenario.

    An instance is safe to reuse across many calculations: it holds only
    immutable configuration (the scenario's strategy_model block and
    model_seed), never race state.
    """

    def __init__(self, config: StrategyModelConfig, model_seed: int) -> None:
        self._config = config
        self._model_seed = model_seed

    def watch_thresholds(self) -> WatchThresholdConfig:
        """Expose the configured hysteresis thresholds (spec 47/18) so the
        Watch Evaluator can use the same, single source of truth rather
        than duplicating these numbers."""
        return self._config.watch_thresholds

    # ------------------------------------------------------------------
    # Lap-time model (spec 15.1 / 46.1)
    # ------------------------------------------------------------------

    def predicted_lap_time_ms(
        self,
        *,
        driver_base_pace_ms: int,
        compound: TireCompound,
        tire_age_laps: int,
        traffic_factor: float,
        track_state: TrackState,
    ) -> int:
        """PredictedLapTime_ms = BaseTrackTime_ms + DriverDelta_ms
        + CompoundDelta_ms + TireDegradation_ms(age_before_lap)
        + TrafficPenalty_ms + EnvironmentalAdjustment_ms  (spec 46.1)

        `driver_base_pace_ms` is the race state's per-driver base_pace_ms,
        which already bundles BaseTrackTime_ms and DriverDelta_ms (that
        split is not separately observable in the canonical race-state
        contract — see README "Strategy Engine" section for this
        documented modeling simplification).
        """
        tire_curve = self._config.tire_curves[compound]
        compound_delta_ms = tire_curve.base_delta_ms
        tire_degradation_ms = tire_curve.degradation_ms_per_lap * tire_age_laps
        traffic_penalty_ms = round(self._config.traffic_penalty_ms_per_unit * traffic_factor)
        environmental_adjustment_ms = self._config.environmental_adjustment_ms[track_state]

        return (
            driver_base_pace_ms
            + compound_delta_ms
            + tire_degradation_ms
            + traffic_penalty_ms
            + environmental_adjustment_ms
        )

    # ------------------------------------------------------------------
    # Driver timeline projection shared by undercut + simulate_strategy
    # ------------------------------------------------------------------

    def _project_driver_time_ms(
        self,
        driver: DriverState,
        *,
        start_lap: int,
        horizon_lap: int,
        pit_lap: Optional[int],
        compound_after_pit: Optional[TireCompound],
        track_state: TrackState,
        pace_noise_ms: float = 0.0,
        pit_duration_noise_ms: float = 0.0,
        degradation_noise_ms: float = 0.0,
        traffic_loss_noise_ms: float = 0.0,
    ) -> int:
        """Cumulative projected time (ms) for one driver from start_lap+1
        through horizon_lap inclusive, on the given pit plan.

        Semantics (spec 46.1):
          * Tire age is evaluated at the start of the projected lap.
          * A pit stop resets tire age to zero for the first completed lap
            on the new compound.
          * Warm-up loss applies to that first completed lap.
          * Pit loss (entry + lane transit + stationary + exit) is added
            once, on the pit lap itself.
        """
        total_ms = 0
        compound = driver.compound
        base_pace_ms = driver.base_pace_ms
        original_age = driver.tire_age_laps

        for lap in range(start_lap + 1, horizon_lap + 1):
            if pit_lap is not None and lap > pit_lap:
                lap_compound = compound_after_pit or compound
                age_before_lap = lap - pit_lap - 1
            else:
                lap_compound = compound
                age_before_lap = original_age + (lap - start_lap)

            lap_time_ms = self.predicted_lap_time_ms(
                driver_base_pace_ms=base_pace_ms,
                compound=lap_compound,
                tire_age_laps=max(age_before_lap, 0),
                traffic_factor=driver.traffic_factor,
                track_state=track_state,
            )
            lap_time_ms += round(pace_noise_ms + degradation_noise_ms * max(age_before_lap, 0))

            if pit_lap is not None and lap == pit_lap:
                lap_time_ms += self._config.pit_loss.total_ms
                lap_time_ms += round(pit_duration_noise_ms)
            if pit_lap is not None and lap == pit_lap + 1:
                lap_time_ms += self._config.warmup_loss_ms

            lap_time_ms += round(traffic_loss_noise_ms)
            total_ms += lap_time_ms

        return total_ms

    # ------------------------------------------------------------------
    # calculate_undercut (spec 16 / 46.2)
    # ------------------------------------------------------------------

    def calculate_undercut(
        self,
        race_state: RaceState,
        candidate_id: str,
        target_id: str,
    ) -> UndercutResult:
        candidate = race_state.driver(candidate_id)
        target = race_state.driver(target_id)
        if candidate is None:
            raise StrategyValidationError(f"Unknown candidate driver: {candidate_id}")
        if target is None:
            raise StrategyValidationError(f"Unknown target driver: {target_id}")
        if candidate_id == target_id:
            raise StrategyValidationError("candidate_driver and target_driver must differ")

        current_gap_to_target_ms = candidate.gap_to_leader_ms - target.gap_to_leader_ms
        if current_gap_to_target_ms < 0:
            raise StrategyValidationError(
                f"{candidate_id} is currently ahead of {target_id}; an undercut calculation "
                "requires the candidate to currently trail the target."
            )

        # Scenario A — PIT NOW: candidate pits lap N, target responds N+1 (spec 46.2).
        candidate_pit_lap = race_state.lap + 1
        target_pit_lap = candidate_pit_lap + DEFAULT_TARGET_RESPONSE_DELAY_LAPS
        horizon_lap = max(candidate_pit_lap, target_pit_lap) + COMPARISON_HORIZON_PADDING_LAPS

        candidate_compound_after_pit = self._next_compound(candidate.compound)
        target_compound_after_pit = self._next_compound(target.compound)

        expected_margin_ms, _, _ = self._undercut_margin(
            race_state=race_state,
            candidate=candidate,
            target=target,
            current_gap_to_target_ms=current_gap_to_target_ms,
            candidate_pit_lap=candidate_pit_lap,
            target_pit_lap=target_pit_lap,
            horizon_lap=horizon_lap,
            candidate_compound_after_pit=candidate_compound_after_pit,
            target_compound_after_pit=target_compound_after_pit,
        )

        confidence = self._run_undercut_monte_carlo(
            race_state=race_state,
            candidate=candidate,
            target=target,
            current_gap_to_target_ms=current_gap_to_target_ms,
            candidate_pit_lap=candidate_pit_lap,
            target_pit_lap=target_pit_lap,
            horizon_lap=horizon_lap,
            candidate_compound_after_pit=candidate_compound_after_pit,
            target_compound_after_pit=target_compound_after_pit,
        )

        window = "OPEN" if confidence.probability_ahead >= self._config.watch_thresholds.open_probability else "CLOSED"

        traffic_classification = self._classify_traffic(candidate.traffic_factor, target.traffic_factor)

        return UndercutResult(
            calculation_id=f"undercut-{uuid.uuid4()}",
            model_version=STRATEGY_MODEL_VERSION,
            race_id=race_state.race_id,
            run_id=race_state.run_id,
            source_state_version=race_state.state_version,
            candidate=candidate_id,
            target=target_id,
            window=window,
            expected_margin_ms=expected_margin_ms,
            comparison_horizon_lap=horizon_lap,
            pit_schedules=(
                PitSchedule(driver_id=candidate_id, pit_lap=candidate_pit_lap, compound_after_pit=candidate_compound_after_pit),
                PitSchedule(driver_id=target_id, pit_lap=target_pit_lap, compound_after_pit=target_compound_after_pit),
            ),
            candidate_tire_age_laps=candidate.tire_age_laps,
            target_tire_age_laps=target.tire_age_laps,
            traffic_classification=traffic_classification,
            assumptions=(
                "green_flag" if race_state.track_state == TrackState.GREEN else race_state.track_state.value.lower(),
                "normal_pit_loss",
                "clean_exit",
                f"target_responds_after_{DEFAULT_TARGET_RESPONSE_DELAY_LAPS}_lap(s)",
            ),
            confidence=confidence,
        )

    def _undercut_margin(
        self,
        *,
        race_state: RaceState,
        candidate: DriverState,
        target: DriverState,
        current_gap_to_target_ms: int,
        candidate_pit_lap: int,
        target_pit_lap: int,
        horizon_lap: int,
        candidate_compound_after_pit: TireCompound,
        target_compound_after_pit: TireCompound,
        pace_noise_fn=None,
        pit_duration_noise_fn=None,
        degradation_noise_fn=None,
        traffic_loss_noise_fn=None,
    ) -> tuple[int, int, int]:
        """Returns (expected_margin_ms, candidate_relative_time_ms, target_relative_time_ms).

        Relative time starts with target = 0 and candidate = current
        gap_to_target_ms. expected_margin_ms = target_relative_time_ms -
        candidate_relative_time_ms at the common horizon. A positive value
        means the candidate is projected ahead (spec 46.2).
        """

        def _noise(fn):
            return fn() if fn is not None else 0.0

        candidate_cumulative_ms = self._project_driver_time_ms(
            candidate,
            start_lap=race_state.lap,
            horizon_lap=horizon_lap,
            pit_lap=candidate_pit_lap,
            compound_after_pit=candidate_compound_after_pit,
            track_state=race_state.track_state,
            pace_noise_ms=_noise(pace_noise_fn),
            pit_duration_noise_ms=_noise(pit_duration_noise_fn),
            degradation_noise_ms=_noise(degradation_noise_fn),
            traffic_loss_noise_ms=_noise(traffic_loss_noise_fn),
        )
        target_cumulative_ms = self._project_driver_time_ms(
            target,
            start_lap=race_state.lap,
            horizon_lap=horizon_lap,
            pit_lap=target_pit_lap,
            compound_after_pit=target_compound_after_pit,
            track_state=race_state.track_state,
            pace_noise_ms=_noise(pace_noise_fn),
            pit_duration_noise_ms=_noise(pit_duration_noise_fn),
            degradation_noise_ms=_noise(degradation_noise_fn),
            traffic_loss_noise_ms=_noise(traffic_loss_noise_fn),
        )

        candidate_relative_time_ms = current_gap_to_target_ms + candidate_cumulative_ms
        target_relative_time_ms = 0 + target_cumulative_ms
        expected_margin_ms = target_relative_time_ms - candidate_relative_time_ms
        return expected_margin_ms, candidate_relative_time_ms, target_relative_time_ms

    def _run_undercut_monte_carlo(
        self,
        *,
        race_state: RaceState,
        candidate: DriverState,
        target: DriverState,
        current_gap_to_target_ms: int,
        candidate_pit_lap: int,
        target_pit_lap: int,
        horizon_lap: int,
        candidate_compound_after_pit: TireCompound,
        target_compound_after_pit: TireCompound,
    ) -> ConfidenceResult:
        # Deterministic Monte-Carlo strategy simulation seeded from
        # self._model_seed; not used for any security/cryptographic purpose.
        rng = random.Random(self._model_seed)  # nosec B311
        mc = self._config.monte_carlo
        margins: list[int] = []

        for _ in range(mc.trials):
            margin_ms, _, _ = self._undercut_margin(
                race_state=race_state,
                candidate=candidate,
                target=target,
                current_gap_to_target_ms=current_gap_to_target_ms,
                candidate_pit_lap=candidate_pit_lap,
                target_pit_lap=target_pit_lap,
                horizon_lap=horizon_lap,
                candidate_compound_after_pit=candidate_compound_after_pit,
                target_compound_after_pit=target_compound_after_pit,
                pace_noise_fn=lambda: rng.gauss(0, mc.lap_pace_noise_std_ms),
                pit_duration_noise_fn=lambda: rng.gauss(0, mc.pit_duration_noise_std_ms),
                degradation_noise_fn=lambda: rng.gauss(0, mc.degradation_noise_std_ms),
                traffic_loss_noise_fn=lambda: rng.gauss(0, mc.traffic_loss_noise_std_ms),
            )
            margins.append(margin_ms)

        return self._summarize_trials(margins, mc.trials)

    def _summarize_trials(self, margins: list[int], trials: int) -> ConfidenceResult:
        sorted_margins = sorted(margins)
        probability_ahead = sum(1 for m in margins if m > 0) / trials
        return ConfidenceResult(
            trials=trials,
            median_margin_ms=round(statistics.median(sorted_margins)),
            p10_margin_ms=_percentile(sorted_margins, 0.10),
            p90_margin_ms=_percentile(sorted_margins, 0.90),
            probability_ahead=probability_ahead,
            model_seed=self._model_seed,
        )

    # ------------------------------------------------------------------
    # simulate_strategy — single-driver PIT NOW vs WAIT k laps (spec 22 / UJ-3)
    # ------------------------------------------------------------------

    def simulate_strategy(
        self,
        race_state: RaceState,
        driver_id: str,
        wait_laps: int = DEFAULT_WAIT_LAPS,
        compound_override: Optional[TireCompound] = None,
    ) -> StrategySimulationResult:
        driver = race_state.driver(driver_id)
        if driver is None:
            raise StrategyValidationError(f"Unknown driver: {driver_id}")
        if wait_laps < 1:
            raise StrategyValidationError("wait_laps must be at least 1")

        pit_now_lap = race_state.lap + 1
        wait_lap = pit_now_lap + wait_laps
        horizon_lap = max(pit_now_lap, wait_lap) + COMPARISON_HORIZON_PADDING_LAPS
        compound_after_pit = compound_override or self._next_compound(driver.compound)

        def _run(pit_lap: int, pace_noise_fn=None, pit_duration_noise_fn=None, degradation_noise_fn=None, traffic_loss_noise_fn=None) -> int:
            def _noise(fn):
                return fn() if fn is not None else 0.0

            return self._project_driver_time_ms(
                driver,
                start_lap=race_state.lap,
                horizon_lap=horizon_lap,
                pit_lap=pit_lap,
                compound_after_pit=compound_after_pit,
                track_state=race_state.track_state,
                pace_noise_ms=_noise(pace_noise_fn),
                pit_duration_noise_ms=_noise(pit_duration_noise_fn),
                degradation_noise_ms=_noise(degradation_noise_fn),
                traffic_loss_noise_ms=_noise(traffic_loss_noise_fn),
            )

        pit_now_time_ms = _run(pit_now_lap)
        wait_time_ms = _run(wait_lap)
        # Positive delta => pitting now is faster (costs less time) than waiting.
        delta_ms = wait_time_ms - pit_now_time_ms

        # Deterministic Monte-Carlo strategy simulation seeded from
        # self._model_seed; not used for any security/cryptographic purpose.
        rng = random.Random(self._model_seed)  # nosec B311
        mc = self._config.monte_carlo
        deltas: list[int] = []
        for _ in range(mc.trials):
            pit_now_trial_ms = _run(
                pit_now_lap,
                pace_noise_fn=lambda: rng.gauss(0, mc.lap_pace_noise_std_ms),
                pit_duration_noise_fn=lambda: rng.gauss(0, mc.pit_duration_noise_std_ms),
                degradation_noise_fn=lambda: rng.gauss(0, mc.degradation_noise_std_ms),
                traffic_loss_noise_fn=lambda: rng.gauss(0, mc.traffic_loss_noise_std_ms),
            )
            wait_trial_ms = _run(
                wait_lap,
                pace_noise_fn=lambda: rng.gauss(0, mc.lap_pace_noise_std_ms),
                pit_duration_noise_fn=lambda: rng.gauss(0, mc.pit_duration_noise_std_ms),
                degradation_noise_fn=lambda: rng.gauss(0, mc.degradation_noise_std_ms),
                traffic_loss_noise_fn=lambda: rng.gauss(0, mc.traffic_loss_noise_std_ms),
            )
            deltas.append(wait_trial_ms - pit_now_trial_ms)

        confidence = self._summarize_trials(deltas, mc.trials)

        return StrategySimulationResult(
            calculation_id=f"simulate-{uuid.uuid4()}",
            model_version=STRATEGY_MODEL_VERSION,
            race_id=race_state.race_id,
            run_id=race_state.run_id,
            source_state_version=race_state.state_version,
            driver_id=driver_id,
            pit_now_lap=pit_now_lap,
            wait_lap=wait_lap,
            wait_laps=wait_laps,
            comparison_horizon_lap=horizon_lap,
            compound_after_pit=compound_after_pit,
            delta_ms=delta_ms,
            assumptions=(
                "green_flag" if race_state.track_state == TrackState.GREEN else race_state.track_state.value.lower(),
                "normal_pit_loss",
                "clean_exit",
                "no_competing_car_modeled",
            ),
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _next_compound(current: TireCompound) -> TireCompound:
        """Demo-model default fresh-tire choice after a pit stop: switch onto
        SOFT (the fastest available compound, consistent with how an
        undercut is normally illustrated — a fresh, faster tire is what
        creates the gap-closing pace advantage) unless already on SOFT, in
        which case fall back to MEDIUM. This is an authored demo
        simplification, not a strategy recommendation, and can always be
        overridden by callers via simulate_strategy's compound_override."""
        if current == TireCompound.SOFT:
            return TireCompound.MEDIUM
        return TireCompound.SOFT

    @staticmethod
    def _classify_traffic(candidate_traffic_factor: float, target_traffic_factor: float) -> str:
        combined = max(candidate_traffic_factor, target_traffic_factor)
        if combined >= 0.5:
            return "HEAVY"
        if combined > 0.0:
            return "LIGHT"
        return "CLEAR"


def _percentile(sorted_values: list[int], fraction: float) -> int:
    """Nearest-rank percentile over an already-sorted list (deterministic,
    no interpolation ambiguity)."""
    if not sorted_values:
        return 0
    index = max(0, min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]
