/**
 * Types mirror backend/app/domain/models.py exactly. Kept in one file so a
 * future contract change is easy to spot as a single diff against the
 * backend models.
 */

export type TireCompound = 'SOFT' | 'MEDIUM' | 'HARD';
export type TrackState = 'GREEN' | 'YELLOW' | 'SAFETY_CAR' | 'RED';
export type SimulatorStatus = 'STOPPED' | 'RUNNING' | 'PAUSED' | 'COMPLETED';

export interface DriverState {
  driver_id: string;
  display_name: string;
  team_name: string;
  position: number;
  gap_to_leader_ms: number;
  gap_to_ahead_ms: number;
  compound: TireCompound;
  tire_age_laps: number;
  base_pace_ms: number;
  current_lap_pace_ms: number;
  pit_stops: number;
  pit_window: [number, number];
  traffic_factor: number;
}

export interface RaceState {
  race_id: string;
  run_id: string;
  state_version: number;
  lap: number;
  total_laps: number;
  race_time_ms: number;
  track_state: TrackState;
  generated_at: string;
  scenario_id: string;
  synthetic: boolean;
  drivers: DriverState[];
}

export interface DemoStatus {
  status: SimulatorStatus;
  race_id: string;
  run_id: string;
  current_lap: number;
  state_version: number;
}

export interface ApiErrorBody {
  error: string;
  message: string;
}

export type WatchStatus = 'OPEN' | 'CLOSED';

export interface ConfidenceResult {
  trials: number;
  median_margin_ms: number;
  p10_margin_ms: number;
  p90_margin_ms: number;
  probability_ahead: number;
  model_seed: number;
}

export interface UndercutResult {
  calculation_id: string;
  model_version: string;
  race_id: string;
  run_id: string;
  source_state_version: number;
  candidate: string;
  target: string;
  window: string;
  expected_margin_ms: number;
  comparison_horizon_lap: number;
  candidate_tire_age_laps: number;
  target_tire_age_laps: number;
  traffic_classification: string;
  assumptions: string[];
  confidence: ConfidenceResult;
}

/**
 * Mirrors backend/app/domain/watch_models.py's Alert exactly (see
 * types.ts's module doc-comment above). Only the fields the dashboard
 * actually renders are given precise shapes; `triggering_snapshot` is
 * typed loosely since AlertsPanel never reads into it (the alert's own
 * `deterministic_result` already carries everything the card needs —
 * see GET /v1/races/{race_id}/alerts/{alert_id}'s docstring on why the
 * triggering snapshot, not live state, is the source of truth for "why
 * now?").
 */
export interface Alert {
  alert_id: string;
  evaluation_id: string;
  watch_id: string;
  race_id: string;
  run_id: string;
  state_version: number;
  previous_status: WatchStatus;
  new_status: WatchStatus;
  triggering_snapshot: RaceState;
  deterministic_result: UndercutResult;
  assumptions: string[];
  model_version: string;
  idempotency_key: string;
  created_at: string;
}

/**
 * Spec v1.1 repair 3: mirrors backend/app/domain/models.py's RaceEvent —
 * the envelope for every message pushed over `/v1/races/{race_id}/ws`.
 * `payload`'s shape depends on `type`: RaceState for RACE_STATE/
 * RACE_RESET, `{kind, result: UndercutResult}` for STRATEGY_UPDATE, Alert
 * for WATCH_ALERT, and the agent query result shape for AGENT_RESPONSE —
 * useRaceSocket.ts narrows it per-branch rather than this type doing so,
 * since the union isn't load-bearing for its own logic (it only inspects
 * `type`/`race_id`/`run_id`/`state_version` before dispatching).
 */
export type RaceEventType =
  | 'RACE_STATE'
  | 'RACE_RESET'
  | 'DEMO_STATUS'
  | 'STRATEGY_UPDATE'
  | 'WATCH_ALERT'
  | 'AGENT_RESPONSE'
  | 'DATA_STALE';

export interface RaceEvent {
  type: RaceEventType;
  race_id: string;
  run_id: string;
  state_version: number;
  generated_at: string;
  payload: unknown;
}

// ---------------------------------------------------------------------------
// Spec v1.1 repair 2: types for the P0 screens beyond the Gate 0 dashboard
// (Strategy Battle, Simulation Comparison, Agent Answer, Evidence/Sources).
// Each mirrors its backend Pydantic model exactly, per this file's own
// module doc-comment convention.
// ---------------------------------------------------------------------------

export type WatchType = 'UNDERCUT';

/** Mirrors backend/app/domain/watch_models.py's Watch. */
export interface Watch {
  watch_id: string;
  race_id: string;
  run_id: string;
  type: WatchType;
  candidate: string;
  target: string;
  open_threshold: number;
  close_threshold: number;
  status: WatchStatus;
  last_probability_ahead: number;
  last_evaluated_state_version: number;
  created_at: string;
  updated_at: string;
}

/** Mirrors backend/app/domain/strategy_models.py's StrategySimulationResult
 * (the simulate_strategy / "PIT NOW vs WAIT N LAPS" counterfactual, UJ-3).
 * `delta_ms` is positive when pitting now is projected faster. */
export interface StrategySimulationResult {
  calculation_id: string;
  model_version: string;
  race_id: string;
  run_id: string;
  source_state_version: number;
  driver_id: string;
  pit_now_lap: number;
  wait_lap: number;
  wait_laps: number;
  comparison_horizon_lap: number;
  compound_after_pit: TireCompound;
  delta_ms: number;
  assumptions: string[];
  confidence: ConfidenceResult;
}

/** Mirrors backend/app/domain/explanation_models.py's KBCitation. */
export interface KBCitation {
  doc_id: string;
  title: string;
  source_path: string;
  excerpt: string;
  score: number;
}

/** Mirrors backend/app/domain/explanation_models.py's ExplanationResult
 * (the Agent Answer screen's data source for "Why?", UJ-2). `degraded`
 * is spec v1.1 repair 14's Bedrock-unavailable signal — see
 * BEDROCK_UNAVAILABLE_NARRATIVE in explanation_engine.py. */
export interface ExplanationResult {
  explanation_id: string;
  alert_id: string;
  watch_id: string;
  race_id: string;
  run_id: string;
  narrative: string;
  citations: KBCitation[];
  grounded_facts: Record<string, string>;
  llm_provider: string;
  model_id: string;
  grounding_warnings: string[];
  created_at: string;
  degraded: boolean;
}

/** Mirrors backend/app/domain/regulation_models.py's RegulationEvidence. */
export interface RegulationEvidence {
  document_id: string;
  section_id: string;
  title: string;
  version: string;
  support_status: 'SUPPORTED';
  excerpt: string;
  source_path: string;
}

/** Mirrors backend/app/domain/regulation_models.py's RegulationAnswer —
 * the Evidence/Sources screen's data source (UJ-4/UJ-5). `available=false`
 * (spec v1.1 repair 14) is a distinct degraded condition from an ordinary
 * `found=false` — see that model's own docstring. */
export interface RegulationAnswer {
  query: string;
  found: boolean;
  evidence: RegulationEvidence[];
  available: boolean;
}

/** Mirrors backend/app/domain/agent_models.py's AgentEvidenceItem. */
export interface AgentEvidenceItem {
  type: string;
  source_id: string;
  field: string;
  value: string;
}

export type AgentQueryStatus = 'SUPPORTED' | 'UNSUPPORTED';

/** Mirrors backend/app/domain/agent_models.py's AgentQueryResult — the
 * POST /v1/agent/query and AGENT_RESPONSE WebSocket-event payload shape. */
export interface AgentQueryResult {
  query: string;
  status: AgentQueryStatus;
  confidence: number;
  narrative: string;
  evidence: AgentEvidenceItem[];
  created_at: string;
}

/** Mirrors backend/app/domain/agent_models.py's RaceChangeDelta. */
export interface RaceChangeDelta {
  field: string;
  driver_id: string | null;
  previous_value: string;
  current_value: string;
  significance: number;
}

/** Mirrors backend/app/domain/agent_models.py's RaceChangesResult — the
 * "What Changed?" screen's data source (UJ-6). */
export interface RaceChangesResult {
  race_id: string;
  run_id: string;
  from_state_version: number | null;
  to_state_version: number;
  changes: RaceChangeDelta[];
  explained_changes: RaceChangeDelta[];
  narrative: string;
  created_at: string;
}
