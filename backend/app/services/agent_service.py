"""Agent query + "What Changed?" services — spec v1.1 repair 9.

Two independent, deterministic services, sharing one architecture
principle with the rest of this codebase ("explain, don't invent"):
compute structured facts first, from real data, then narrate only those
facts. Neither service here calls a real LLM — like
`app/services/knowledge_base.py`'s retrieval and
`app/services/llm_client.py`'s `TemplateLLMClient`, narration is a fixed,
deterministic template over already-computed facts. This is documented as
a deliberate simplification (see each class's docstring for why), not a
claim that a generative model is unnecessary in general — swapping in a
real LLM for the narrative sentence only, with `evidence`/`changes`
untouched, is a drop-in change at exactly these two functions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from app.domain.agent_models import (
    AgentEvidenceItem,
    AgentQueryResult,
    RaceChangeDelta,
    RaceChangesResult,
)
from app.domain.models import RaceState
from app.services.regulation_service import RegulationService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET /v1/races/{race_id}/changes ("What Changed?")
# ---------------------------------------------------------------------------

# Fixed, documented significance weights used only to RANK which changes
# the narrative covers — not a physical/statistical claim about how much
# lap time each unit is "worth." Chosen so a concrete strategic event (a
# pit stop, a track-status change) always outranks small numeric drift in
# a gap or tire age, which matches how a race engineer would prioritize
# what to mention first.
_SIGNIFICANCE_TRACK_STATE_CHANGE = 9000.0
_SIGNIFICANCE_UNDERCUT_EVENT = 7000.0
_SIGNIFICANCE_TIRE_AGE_PER_LAP = 400.0
_SIGNIFICANCE_TRAFFIC_PER_UNIT = 3000.0
# gap_change_ms significance is the raw millisecond delta itself (already
# on a directly comparable scale to the other weighted deltas above).


class RaceChangeTracker:
    """Holds the last two committed `RaceState`s per race_id in memory, so
    "What Changed?" always has something to diff without the caller
    needing to supply explicit from/to state_versions.

    Wired as an additional `on_state_committed` hook alongside
    `WatchEvaluator.evaluate` (see `app/main.py`'s lifespan) — every
    accepted state write is recorded here too. A `RACE_RESET` (a state
    with a new `run_id`) discards the previous state rather than diffing
    across two different runs, mirroring the run-isolation guard already
    enforced for watches (spec v1.1 repairs 4/6).
    """

    def __init__(self) -> None:
        self._previous: dict[str, RaceState] = {}
        self._current: dict[str, RaceState] = {}

    async def record(self, race_state: RaceState) -> None:
        current = self._current.get(race_state.race_id)
        if current is not None and current.run_id == race_state.run_id:
            if race_state.state_version <= current.state_version:
                return  # duplicate/out-of-order delivery — ignore, same guard as WatchEvaluator
            self._previous[race_state.race_id] = current
        else:
            # First state ever seen for this race, or a new run (reset):
            # nothing valid to diff against yet.
            self._previous.pop(race_state.race_id, None)
        self._current[race_state.race_id] = race_state

    def get_previous_and_current(self, race_id: str) -> tuple[Optional[RaceState], Optional[RaceState]]:
        return self._previous.get(race_id), self._current.get(race_id)


def compute_race_changes(previous: Optional[RaceState], current: RaceState) -> tuple[RaceChangeDelta, ...]:
    """Pure function: structured deltas between two consecutive committed
    states, most-significant first. No narration happens here — this is
    exactly the "compute structured deltas FIRST" step spec v1.1 repair 9
    requires before any LLM/template involvement."""
    if previous is None or previous.run_id != current.run_id:
        return ()

    deltas: list[RaceChangeDelta] = []

    if previous.track_state != current.track_state:
        deltas.append(
            RaceChangeDelta(
                field="track_state_change",
                driver_id=None,
                previous_value=previous.track_state.value,
                current_value=current.track_state.value,
                significance=_SIGNIFICANCE_TRACK_STATE_CHANGE,
            )
        )

    previous_by_id = {d.driver_id: d for d in previous.drivers}
    for driver in current.drivers:
        prior = previous_by_id.get(driver.driver_id)
        if prior is None:
            continue  # driver wasn't present in the prior snapshot — nothing to diff

        gap_delta = driver.gap_to_leader_ms - prior.gap_to_leader_ms
        if gap_delta != 0:
            deltas.append(
                RaceChangeDelta(
                    field="gap_change_ms",
                    driver_id=driver.driver_id,
                    previous_value=str(prior.gap_to_leader_ms),
                    current_value=str(driver.gap_to_leader_ms),
                    significance=float(abs(gap_delta)),
                )
            )

        tire_age_delta = driver.tire_age_laps - prior.tire_age_laps
        if tire_age_delta != 0:
            deltas.append(
                RaceChangeDelta(
                    field="tire_age_change",
                    driver_id=driver.driver_id,
                    previous_value=str(prior.tire_age_laps),
                    current_value=str(driver.tire_age_laps),
                    significance=abs(tire_age_delta) * _SIGNIFICANCE_TIRE_AGE_PER_LAP,
                )
            )

        if driver.pit_stops > prior.pit_stops:
            deltas.append(
                RaceChangeDelta(
                    field="undercut_state_change",
                    driver_id=driver.driver_id,
                    previous_value=f"{prior.pit_stops} stops, {prior.compound.value}",
                    current_value=f"{driver.pit_stops} stops, {driver.compound.value}",
                    significance=_SIGNIFICANCE_UNDERCUT_EVENT,
                )
            )

        traffic_delta = driver.traffic_factor - prior.traffic_factor
        if abs(traffic_delta) > 1e-9:
            deltas.append(
                RaceChangeDelta(
                    field="traffic_change",
                    driver_id=driver.driver_id,
                    previous_value=f"{prior.traffic_factor:.2f}",
                    current_value=f"{driver.traffic_factor:.2f}",
                    significance=abs(traffic_delta) * _SIGNIFICANCE_TRAFFIC_PER_UNIT,
                )
            )

    deltas.sort(key=lambda d: (-d.significance, d.field, d.driver_id or ""))
    return tuple(deltas)


def _narrate_change(delta: RaceChangeDelta) -> str:
    who = delta.driver_id or "The race"
    if delta.field == "track_state_change":
        return f"Track status changed from {delta.previous_value} to {delta.current_value}."
    if delta.field == "gap_change_ms":
        direction = "gained" if int(delta.current_value) < int(delta.previous_value) else "lost"
        change_ms = abs(int(delta.current_value) - int(delta.previous_value))
        return f"{who} {direction} {change_ms}ms to the leader (now {delta.current_value}ms)."
    if delta.field == "tire_age_change":
        return f"{who}'s tire age moved from {delta.previous_value} to {delta.current_value} laps."
    if delta.field == "undercut_state_change":
        return f"{who} pitted: {delta.previous_value} -> {delta.current_value}."
    if delta.field == "traffic_change":
        return f"{who}'s traffic factor moved from {delta.previous_value} to {delta.current_value}."
    return f"{who}: {delta.field} changed from {delta.previous_value} to {delta.current_value}."  # pragma: no cover - exhaustive above


def narrate_race_changes(changes: tuple[RaceChangeDelta, ...]) -> tuple[str, tuple[RaceChangeDelta, ...]]:
    """Returns (narrative, explained_changes) where `explained_changes` is
    the top-3 subset of `changes` the narrative actually describes — spec
    v1.1 repair 9: "rank by significance, then have the LLM explain only
    the top 3." (Here: the deterministic template explains only the top
    3; see module docstring on why this is template-based, not a live
    LLM call, in this environment.)"""
    if not changes:
        return "No changes since the previous recorded state.", ()
    top_3 = changes[:3]
    narrative = " ".join(_narrate_change(d) for d in top_3)
    return narrative, top_3


class AgentQueryService:
    """Answers `POST /v1/agent/query` free-text questions using
    `RaceState` + `RegulationService` as the ONLY sources of truth —
    never inventing a fact. See module docstring.

    This is a small, explicit query router rather than a general NLU
    system, matching this repository's "deterministic and auditable
    over impressive" pattern used throughout (knowledge_base.py,
    regulation_service.py): it recognizes a handful of concrete question
    shapes about the current race state (leader, gap, tire age, compound,
    current lap, track status) by keyword + driver-id pattern matching,
    and otherwise defers to `RegulationService.get_regulation`. A query
    matching neither returns `UNSUPPORTED` rather than a guess — the same
    contract `get_regulation` itself makes.
    """

    _DRIVER_ID_RE = re.compile(r"\bCAR[_\s-]?0*([0-9]{1,2})\b", re.IGNORECASE)

    def __init__(self, regulation_service: RegulationService):
        self._regulation_service = regulation_service

    def _find_driver(self, race_state: RaceState, query: str):
        match = self._DRIVER_ID_RE.search(query)
        if not match:
            return None
        candidate_id = f"CAR_{int(match.group(1)):02d}"
        return race_state.driver(candidate_id)

    def query(self, race_state: RaceState, query: str) -> AgentQueryResult:
        normalized = query.strip().lower()

        if not normalized:
            return AgentQueryResult(
                query=query, status="UNSUPPORTED", confidence=0.0,
                narrative="Please ask a specific question about the race or the rules.",
                evidence=(), created_at=_utc_now(),
            )

        driver = self._find_driver(race_state, query)

        if driver is not None and ("gap" in normalized or "behind" in normalized or "ahead" in normalized):
            evidence = (
                AgentEvidenceItem(
                    type="race_state_field", source_id=race_state.race_id,
                    field=f"drivers.{driver.driver_id}.gap_to_leader_ms", value=str(driver.gap_to_leader_ms),
                ),
            )
            return AgentQueryResult(
                query=query, status="SUPPORTED", confidence=1.0,
                narrative=f"{driver.display_name} is {driver.gap_to_leader_ms}ms behind the leader on lap {race_state.lap}.",
                evidence=evidence, created_at=_utc_now(),
            )

        if driver is not None and ("tire" in normalized or "compound" in normalized):
            evidence = (
                AgentEvidenceItem(
                    type="race_state_field", source_id=race_state.race_id,
                    field=f"drivers.{driver.driver_id}.compound", value=driver.compound.value,
                ),
                AgentEvidenceItem(
                    type="race_state_field", source_id=race_state.race_id,
                    field=f"drivers.{driver.driver_id}.tire_age_laps", value=str(driver.tire_age_laps),
                ),
            )
            return AgentQueryResult(
                query=query, status="SUPPORTED", confidence=1.0,
                narrative=f"{driver.display_name} is on {driver.compound.value} tires, {driver.tire_age_laps} laps old.",
                evidence=evidence, created_at=_utc_now(),
            )

        if "leading" in normalized or "leader" in normalized or "first place" in normalized:
            leader = min(race_state.drivers, key=lambda d: d.position, default=None)
            if leader is not None:
                evidence = (
                    AgentEvidenceItem(
                        type="race_state_field", source_id=race_state.race_id,
                        field="drivers.leader.display_name", value=leader.display_name,
                    ),
                )
                return AgentQueryResult(
                    query=query, status="SUPPORTED", confidence=1.0,
                    narrative=f"{leader.display_name} is leading on lap {race_state.lap}.",
                    evidence=evidence, created_at=_utc_now(),
                )

        if "what lap" in normalized or "current lap" in normalized:
            evidence = (
                AgentEvidenceItem(
                    type="race_state_field", source_id=race_state.race_id, field="lap", value=str(race_state.lap),
                ),
            )
            return AgentQueryResult(
                query=query, status="SUPPORTED", confidence=1.0,
                narrative=f"The race is currently on lap {race_state.lap} of {race_state.total_laps}.",
                evidence=evidence, created_at=_utc_now(),
            )

        if "track status" in normalized or "flag" in normalized or "safety car" in normalized:
            evidence = (
                AgentEvidenceItem(
                    type="race_state_field", source_id=race_state.race_id,
                    field="track_state", value=race_state.track_state.value,
                ),
            )
            return AgentQueryResult(
                query=query, status="SUPPORTED", confidence=1.0,
                narrative=f"Current track status is {race_state.track_state.value}.",
                evidence=evidence, created_at=_utc_now(),
            )

        regulation_answer = self._regulation_service.get_regulation(query)
        if not regulation_answer.available:
            # Spec v1.1 repair 14 / Section 29 "Knowledge Base unavailable":
            # this is a distinct condition from an ordinary unsupported
            # query (regulation_answer.found=False with available=True,
            # handled by the UNSUPPORTED branch below) — the corpus itself
            # could not be reached, so the agent must say so explicitly
            # rather than reusing the generic "I don't have information"
            # wording, and must not silently fall back to guessing.
            return AgentQueryResult(
                query=query, status="UNSUPPORTED", confidence=0.0,
                narrative="Regulation source unavailable. I won't infer the rule.",
                evidence=(), created_at=_utc_now(),
            )
        if regulation_answer.found:
            evidence = tuple(
                AgentEvidenceItem(
                    type="regulation", source_id=item.document_id,
                    field=item.section_id, value=item.excerpt,
                )
                for item in regulation_answer.evidence
            )
            titles = "; ".join(f"{item.title}: {item.excerpt}" for item in regulation_answer.evidence[:1])
            return AgentQueryResult(
                query=query, status="SUPPORTED", confidence=0.9,
                narrative=titles, evidence=evidence, created_at=_utc_now(),
            )

        return AgentQueryResult(
            query=query, status="UNSUPPORTED", confidence=0.0,
            narrative=(
                "I don't have information to answer that. I can answer questions about the "
                "current race state (gaps, tires, track status, the leader) or the sporting "
                "rules I have on file, but I won't guess beyond that."
            ),
            evidence=(), created_at=_utc_now(),
        )


def build_race_changes_result(race_id: str, previous: Optional[RaceState], current: RaceState) -> RaceChangesResult:
    changes = compute_race_changes(previous, current)
    narrative, explained = narrate_race_changes(changes)
    return RaceChangesResult(
        race_id=race_id,
        run_id=current.run_id,
        from_state_version=previous.state_version if previous is not None else None,
        to_state_version=current.state_version,
        changes=changes,
        explained_changes=explained,
        narrative=narrative,
        created_at=_utc_now(),
    )
