"""Agent query + "What Changed?" domain models — spec v1.1 repair 9.

Both endpoints share one principle, stated explicitly in the spec: the LLM
(or, for the deterministic Template provider, the narration template)
explains structured evidence computed independently of it. It never
computes the evidence itself.

- `POST /v1/agent/query` (AgentQueryResult): answers a free-text question
  about the race using `evidence` gathered from deterministic sources
  (current race state, the strategy engine, RegulationService) BEFORE any
  narration happens. `status` is SUPPORTED only when at least one evidence
  item backs the answer; otherwise UNSUPPORTED, matching
  RegulationAnswer's found=False philosophy — no evidence means no claim.

- `GET /v1/races/{race_id}/changes` ("What Changed?", RaceChangesResult):
  spec v1.1 repair 9 is explicit that this must "compute structured deltas
  FIRST ... rank by significance, then have the LLM explain only the top
  3" and must NOT "give two complete states to the LLM and ask it to find
  changes." `RaceChangeDelta` is that structured, pre-computed diff.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

EvidenceType = Literal["race_state_field", "strategy_calculation", "regulation"]
AgentAnswerStatus = Literal["SUPPORTED", "UNSUPPORTED"]


class AgentEvidenceItem(BaseModel):
    """One structured fact backing an agent answer — independent of
    prose, per spec v1.1 repair 9. `type` names the kind of source,
    `source_id` identifies which instance of it (a race_id, a
    calculation_id, a regulation document_id), `field` names which
    specific fact, and `value` is that fact's value, already rendered as
    a string (kept simple/uniform rather than a discriminated-union value
    type, since every current caller only displays it)."""

    model_config = ConfigDict(frozen=True)

    type: EvidenceType
    source_id: str
    field: str
    value: str


class AgentQueryResult(BaseModel):
    """Response to POST /v1/agent/query."""

    model_config = ConfigDict(frozen=True)

    query: str
    status: AgentAnswerStatus
    confidence: float  # 0.0-1.0; see agent_service.py for how this is derived
    narrative: str
    evidence: tuple[AgentEvidenceItem, ...]
    created_at: datetime


class RaceChangeDelta(BaseModel):
    """One structured, pre-computed difference between two consecutive
    committed race states for one field of one driver (or the race as a
    whole). `significance` is the ranking key described in the module
    docstring — a plain, deterministic magnitude, not an LLM judgment."""

    model_config = ConfigDict(frozen=True)

    field: str  # e.g. "gap_change_ms", "tire_age_change", "undercut_state_change", "traffic_change", "track_state_change"
    driver_id: str | None  # None for a race-level (not per-driver) change, e.g. track_state_change
    previous_value: str
    current_value: str
    significance: float


class RaceChangesResult(BaseModel):
    """Response to GET /v1/races/{race_id}/changes ("What Changed?")."""

    model_config = ConfigDict(frozen=True)

    race_id: str
    run_id: str
    from_state_version: int | None  # None if no prior state has been recorded yet this run
    to_state_version: int
    changes: tuple[RaceChangeDelta, ...]  # ALL computed deltas, most-significant first
    explained_changes: tuple[RaceChangeDelta, ...]  # top 3 of `changes` — what the narrative below covers
    narrative: str
    created_at: datetime
