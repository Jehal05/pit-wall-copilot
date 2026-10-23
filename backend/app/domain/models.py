"""Canonical, typed domain models for Pit Wall Copilot — Gate 0.

These models are the single source of truth for the shape of race state.
No LLM writes these structures; they are produced only by the deterministic
race simulator from the authored scenario file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp helper (spec 46.1 / 48)."""
    return datetime.now(timezone.utc)


# Spec v1.1 Section 29 "Race data stale": "If current_time - generated_at >
# 5 seconds, display DATA STALE. Suppress new proactive strategy alerts."
# This single constant is shared by every backend consumer of that rule
# (see `is_state_stale` below, used by WatchEvaluator to suppress alert
# generation) and matches Fire TV's own STALE_THRESHOLD_MS=5000 in
# useRaceSocket.ts, so both sides agree on exactly what "stale" means.
DATA_STALE_THRESHOLD_SECONDS = 5.0


def is_state_stale(generated_at: datetime, *, now: datetime | None = None) -> bool:
    """True iff `generated_at` is more than `DATA_STALE_THRESHOLD_SECONDS`
    in the past relative to `now` (defaults to the real current time)."""
    reference = now if now is not None else utc_now()
    return (reference - generated_at).total_seconds() > DATA_STALE_THRESHOLD_SECONDS


class TireCompound(str, Enum):
    SOFT = "SOFT"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class TrackState(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    SAFETY_CAR = "SAFETY_CAR"
    RED = "RED"


class SimulatorStatus(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class DriverState(BaseModel):
    """Authoritative state for a single car. Never written by an LLM."""

    model_config = ConfigDict(frozen=True)

    driver_id: str
    display_name: str
    team_name: str
    position: int = Field(ge=1)
    gap_to_leader_ms: int = Field(ge=0)
    gap_to_ahead_ms: int = Field(ge=0)
    compound: TireCompound
    tire_age_laps: int = Field(ge=0)
    base_pace_ms: int = Field(gt=0)
    current_lap_pace_ms: int = Field(gt=0)
    pit_stops: int = Field(ge=0)
    pit_window: tuple[int, int]
    traffic_factor: float = Field(ge=0.0, le=1.0)


class RaceState(BaseModel):
    """Canonical race state contract (spec section 13 / 46.1 / 48)."""

    model_config = ConfigDict(frozen=True)

    race_id: str
    run_id: str
    state_version: int = Field(ge=0)
    lap: int = Field(ge=0)
    total_laps: int = Field(gt=0)
    race_time_ms: int = Field(ge=0)
    track_state: TrackState
    generated_at: datetime
    scenario_id: str
    synthetic: bool = True
    drivers: tuple[DriverState, ...]

    def driver(self, driver_id: str) -> Optional[DriverState]:
        for d in self.drivers:
            if d.driver_id == driver_id:
                return d
        return None


class DemoControlState(BaseModel):
    """Simulator lifecycle / control-plane state (spec section 48)."""

    model_config = ConfigDict(frozen=True)

    race_id: str
    run_id: str
    status: SimulatorStatus
    scenario_id: str
    current_lap: int
    state_version: int
    updated_at: datetime
    # P0-1 repair pass: a second, independent optimistic-concurrency
    # counter, owned exclusively by `RaceStateRepository
    # .transition_control_from_operator` (repositories/base.py). Kept
    # entirely separate from `state_version` (owned by the simulator tick
    # loop) so an operator command (`/start`, `/pause`) never has to
    # guess or race against the tick loop's own version number. Defaults
    # to 0 for backward compatibility with every existing call site that
    # constructs a `DemoControlState` without it (route responses, tests,
    # `SimulatorService._begin_new_run`'s fresh baseline).
    control_revision: int = 0


class RaceEventType(str, Enum):
    RACE_STATE = "RACE_STATE"
    RACE_RESET = "RACE_RESET"
    DEMO_STATUS = "DEMO_STATUS"
    # Added for spec v1.1 repair 3 (WebSocket events):
    STRATEGY_UPDATE = "STRATEGY_UPDATE"
    WATCH_ALERT = "WATCH_ALERT"
    AGENT_RESPONSE = "AGENT_RESPONSE"
    DATA_STALE = "DATA_STALE"


class RaceEvent(BaseModel):
    """Envelope for a published race event (spec section 24; WebSocket
    push implemented in spec v1.1 repair 3 via
    app/services/event_broadcaster.py and the `/v1/races/{race_id}/ws`
    route). This model is the one shape used by both the WebSocket
    publisher and anything that wants to describe the same event over
    REST — defined once so neither can drift from the other.
    """

    model_config = ConfigDict(frozen=True)

    type: RaceEventType
    race_id: str
    run_id: str
    state_version: int
    generated_at: datetime
    payload: dict
