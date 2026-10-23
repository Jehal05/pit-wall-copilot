"""Deterministic strategy-engine domain models — Gate 1.

These types implement the calculation contract from spec v1.1 sections
46 ("Deterministic Strategy Calculation Contract") and 47 ("Mandatory
Seeded Confidence Model"). Nothing here is written or read by an LLM;
every value is produced by plain, testable application code in
app/services/strategy_engine.py.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import TireCompound, TrackState

STRATEGY_MODEL_VERSION = "strategy-engine-v1"


class TireCurveConfig(BaseModel):
    """Synthetic, authored tire-curve parameters (spec 15.1).

    base_delta_ms: compound performance offset relative to MEDIUM at zero
    degradation (negative = faster than medium, positive = slower).
    degradation_ms_per_lap: additional lap time added per lap of tire age.
    These are demo-model parameters, not claims about real tire physics.
    """

    model_config = ConfigDict(frozen=True)

    base_delta_ms: int
    degradation_ms_per_lap: int = Field(ge=0)


class PitLossConfig(BaseModel):
    """Pit-loss components (spec 46.1): "Pit loss includes pit entry, lane
    transit, stationary time and pit exit and is added once on the pit
    lap." """

    model_config = ConfigDict(frozen=True)

    pit_entry_ms: int = Field(ge=0)
    lane_transit_ms: int = Field(ge=0)
    stationary_time_ms: int = Field(ge=0)
    pit_exit_ms: int = Field(ge=0)

    @property
    def total_ms(self) -> int:
        return self.pit_entry_ms + self.lane_transit_ms + self.stationary_time_ms + self.pit_exit_ms


class MonteCarloConfig(BaseModel):
    """Noise distribution parameters for the mandatory seeded confidence
    model (spec 47). Values are standard deviations, in milliseconds, of a
    zero-mean Gaussian perturbation applied per simulated lap/pit stop."""

    model_config = ConfigDict(frozen=True)

    trials: int = Field(gt=0)
    lap_pace_noise_std_ms: float = Field(ge=0)
    pit_duration_noise_std_ms: float = Field(ge=0)
    degradation_noise_std_ms: float = Field(ge=0)
    traffic_loss_noise_std_ms: float = Field(ge=0)


class WatchThresholdConfig(BaseModel):
    """Hysteresis thresholds for undercut-window open/close (spec 47 / 18)."""

    model_config = ConfigDict(frozen=True)

    open_probability: float = Field(ge=0.0, le=1.0)
    close_probability: float = Field(ge=0.0, le=1.0)


class StrategyModelConfig(BaseModel):
    """The full versioned strategy-model configuration, read from the
    scenario file (spec 46.1: "The engine SHALL read parameters from the
    versioned scenario/model configuration.")."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_version: str
    tire_curves: dict[TireCompound, TireCurveConfig]
    pit_loss: PitLossConfig
    warmup_loss_ms: int = Field(ge=0)
    traffic_penalty_ms_per_unit: int = Field(ge=0)
    environmental_adjustment_ms: dict[TrackState, int]
    monte_carlo: MonteCarloConfig
    watch_thresholds: WatchThresholdConfig


class PitSchedule(BaseModel):
    """One driver's projected pit lap and resulting compound in a scenario."""

    model_config = ConfigDict(frozen=True)

    driver_id: str
    pit_lap: Optional[int]
    compound_after_pit: Optional[TireCompound]


class ConfidenceResult(BaseModel):
    """Output of the mandatory seeded Monte Carlo confidence model (spec 47)."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    trials: int
    median_margin_ms: int
    p10_margin_ms: int
    p90_margin_ms: int
    probability_ahead: float = Field(ge=0.0, le=1.0)
    model_seed: int


class UndercutResult(BaseModel):
    """Output of calculate_undercut (spec 16 / 46.2 / 47)."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    calculation_id: str
    model_version: str
    race_id: str
    run_id: str
    source_state_version: int
    candidate: str
    target: str
    window: str  # "OPEN" | "CLOSED"
    expected_margin_ms: int
    comparison_horizon_lap: int
    pit_schedules: tuple[PitSchedule, PitSchedule]
    candidate_tire_age_laps: int
    target_tire_age_laps: int
    traffic_classification: str
    assumptions: tuple[str, ...]
    confidence: ConfidenceResult


class StrategySimulationResult(BaseModel):
    """Output of simulate_strategy — a single-driver PIT NOW vs WAIT
    counterfactual (spec 22 simulate_strategy tool / UJ-3 "what if the
    driver stays out three more laps")."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    calculation_id: str
    model_version: str
    race_id: str
    run_id: str
    source_state_version: int
    driver_id: str
    pit_now_lap: int
    wait_lap: int
    wait_laps: int
    comparison_horizon_lap: int
    compound_after_pit: TireCompound
    delta_ms: int  # positive => pitting now is faster by this margin
    assumptions: tuple[str, ...]
    confidence: ConfidenceResult
