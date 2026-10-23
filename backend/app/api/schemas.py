from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.domain.models import SimulatorStatus, TireCompound
from app.domain.watch_models import WatchType
from app.services.strategy_engine import DEFAULT_WAIT_LAPS


class HealthResponse(BaseModel):
    status: str
    app_version: str
    timestamp: datetime
    # AWS hackathon-final pass, phase 4/5: surfaces whether the configured
    # cross-instance event broker is actually connected, so `status`
    # itself can reflect "not ready for multi-instance traffic" rather
    # than a required broker being silently degraded forever behind an
    # always-"ok" health check — see routes.py's `health()` for exactly
    # when this flips `status` to "degraded" (and the ALB/ECS health
    # check to unhealthy) vs. only reporting `event_broker_connected`.
    event_broker: str
    event_broker_connected: bool


class DemoStatusResponse(BaseModel):
    status: SimulatorStatus
    race_id: str
    run_id: str
    current_lap: int
    state_version: int


class ErrorResponse(BaseModel):
    error: str
    message: str


class UndercutRequest(BaseModel):
    """calculate_undercut inputs (spec 22 MCP tool contract, exposed here as
    a plain REST request body since MCP itself is out of Gate 1 scope)."""

    candidate_driver: str
    target_driver: str


class SimulateStrategyRequest(BaseModel):
    """simulate_strategy inputs (spec 22)."""

    driver_id: str
    wait_laps: int = Field(default=DEFAULT_WAIT_LAPS, ge=1, le=20)
    compound: Optional[TireCompound] = None


class CreateWatchRequest(BaseModel):
    """create_strategy_watch inputs (spec 22)."""

    candidate_driver: str
    target_driver: str
    watch_type: WatchType = WatchType.UNDERCUT


class AgentQueryRequest(BaseModel):
    """POST /v1/agent/query request body — spec v1.1 repair 9."""

    race_id: str
    query: str = Field(min_length=1)
