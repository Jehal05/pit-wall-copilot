"""Strategy Watch Engine domain models — Gate 2.

Implements spec v1.1 section 18 ("Strategy Watch Engine") and section 49
("Watch Evaluation, Alert Snapshot and Idempotency"). Nothing here involves
an LLM: a Watch is evaluated by re-running the Gate 1 deterministic
strategy engine, and an Alert is only ever written by the Watch Evaluator
in app/services/watch_evaluator.py.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import RaceState
from app.domain.strategy_models import UndercutResult


class WatchType(str, Enum):
    UNDERCUT = "UNDERCUT"


class WatchStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Watch(BaseModel):
    """A persistent strategy watch (spec 18).

    `run_id` records which simulator run the watch was created under. A
    watch is only ever evaluated against race state from the SAME run_id
    (spec 48: "Reset SHALL invalidate active watches ... associated with
    the previous run"); once the run changes, this watch is implicitly
    excluded from evaluation and is not resurrected by a new run.
    """

    model_config = ConfigDict(frozen=True)

    watch_id: str
    race_id: str
    run_id: str
    type: WatchType
    candidate: str
    target: str
    open_threshold: float = Field(ge=0.0, le=1.0)
    close_threshold: float = Field(ge=0.0, le=1.0)
    status: WatchStatus
    last_probability_ahead: float = Field(ge=0.0, le=1.0)
    last_evaluated_state_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class Alert(BaseModel):
    """A persisted watch-state transition (spec 49).

    "When a watch changes state, the stored alert SHALL contain alert_id,
    evaluation_id, watch_id, run_id, state_version, triggering race-state
    snapshot, deterministic result, uncertainty result, assumptions, model
    version and created_at." The uncertainty (confidence) result already
    lives inside `deterministic_result.confidence`, so it is not
    duplicated as a separate field.

    A stored Alert's `triggering_snapshot` is immutable and SHALL NOT be
    replaced by a newer race state, even if one exists by the time the
    alert is read (spec 49: "'Why now?' SHALL explain the stored alert
    snapshot ... and must not replace the triggering evidence.").
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    alert_id: str
    evaluation_id: str
    watch_id: str
    race_id: str
    run_id: str
    state_version: int = Field(ge=0)
    previous_status: WatchStatus
    new_status: WatchStatus
    triggering_snapshot: RaceState
    deterministic_result: UndercutResult
    assumptions: tuple[str, ...]
    model_version: str
    idempotency_key: str
    created_at: datetime
