"""Scenario loading and validation.

The scenario JSON file is the single source of the changing demonstration
values (spec: "The scenario file must be the source of the changing
demonstration values. Do not scatter these values through Python or
TypeScript files."). This module only loads, validates and converts it into
typed objects — it introduces no per-car business logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator, ValidationError

from app.domain.models import TireCompound, TrackState
from app.domain.strategy_models import StrategyModelConfig


class ScenarioCarConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    display_name: str
    team_name: str
    compound: TireCompound
    base_pace_ms: int = Field(gt=0)
    starting_tire_age_laps: int = Field(ge=0)
    pit_window: tuple[int, int]
    pit_stops: int = Field(ge=0)


class ScenarioDriverLap(BaseModel):
    model_config = ConfigDict(frozen=True)

    position: int = Field(ge=1)
    gap_to_leader_ms: int = Field(ge=0)
    tire_age_laps: int = Field(ge=0)
    current_lap_pace_ms: int = Field(gt=0)
    traffic_factor: float = Field(ge=0.0, le=1.0)


class ScenarioLap(BaseModel):
    model_config = ConfigDict(frozen=True)

    lap: int = Field(ge=1)
    race_time_ms: int = Field(ge=0)
    gap_car04_to_car03_ms: int = Field(ge=0)
    drivers: dict[str, ScenarioDriverLap]


class Scenario(BaseModel):
    """Validated representation of scenario-demo-01.json."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    scenario_id: str
    race_id: str
    model_seed: int
    start_lap: int = Field(ge=1)
    end_lap: int = Field(ge=1)
    total_laps: int = Field(gt=0)
    update_interval_seconds: float = Field(gt=0)
    track_state: TrackState
    synthetic: bool
    description: str
    cars: dict[str, ScenarioCarConfig]
    laps: tuple[ScenarioLap, ...]
    strategy_model: StrategyModelConfig

    @field_validator("end_lap")
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        start = info.data.get("start_lap")
        if start is not None and v < start:
            raise ValueError("end_lap must be >= start_lap")
        return v

    @field_validator("laps")
    @classmethod
    def laps_cover_range(cls, v: tuple[ScenarioLap, ...], info) -> tuple[ScenarioLap, ...]:
        if not v:
            raise ValueError("scenario must define at least one lap")
        start = info.data.get("start_lap")
        end = info.data.get("end_lap")
        lap_numbers = [lap.lap for lap in v]
        if lap_numbers != sorted(lap_numbers):
            raise ValueError("scenario laps must be strictly ordered")
        if start is not None and lap_numbers[0] != start:
            raise ValueError("first scenario lap must equal start_lap")
        if end is not None and lap_numbers[-1] != end:
            raise ValueError("last scenario lap must equal end_lap")
        for lap in v:
            missing = set(info.data.get("cars", {}).keys()) - set(lap.drivers.keys())
            if missing:
                raise ValueError(f"lap {lap.lap} is missing driver data for: {sorted(missing)}")
        return v

    def lap_by_number(self, lap: int) -> Optional[ScenarioLap]:
        for scenario_lap in self.laps:
            if scenario_lap.lap == lap:
                return scenario_lap
        return None


class ScenarioValidationError(ValueError):
    """Raised when the scenario file fails structural or semantic validation."""


def load_scenario(path: str | Path) -> Scenario:
    """Load and validate the scenario JSON file from disk.

    Raises ScenarioValidationError with a clear message on malformed input,
    rather than letting a raw JSON/Pydantic error leak to the caller.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ScenarioValidationError(f"Scenario file not found: {file_path}")

    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioValidationError(f"Scenario file is not valid JSON: {exc}") from exc

    try:
        return Scenario.model_validate(raw)
    except ValidationError as exc:
        raise ScenarioValidationError(f"Scenario file failed validation: {exc}") from exc
