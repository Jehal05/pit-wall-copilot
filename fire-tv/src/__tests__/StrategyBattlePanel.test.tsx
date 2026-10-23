import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {StrategyBattlePanel} from '../components/StrategyBattlePanel';
import {DriverState, UndercutResult} from '../api/types';

function makeDriver(overrides: Partial<DriverState> = {}): DriverState {
  return {
    driver_id: 'CAR_04',
    display_name: 'J. Okafor',
    team_name: 'Velocity GP',
    position: 4,
    gap_to_leader_ms: 17600,
    gap_to_ahead_ms: 3400,
    compound: 'MEDIUM',
    tire_age_laps: 18,
    base_pace_ms: 91650,
    current_lap_pace_ms: 91700,
    pit_stops: 1,
    pit_window: [25, 30],
    traffic_factor: 0.1,
    ...overrides,
  };
}

function makeUndercutResult(overrides: Partial<UndercutResult> = {}): UndercutResult {
  return {
    calculation_id: 'calc-1',
    model_version: 'v1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    source_state_version: 4,
    candidate: 'CAR_04',
    target: 'CAR_03',
    window: 'OPEN',
    expected_margin_ms: 800,
    comparison_horizon_lap: 29,
    candidate_tire_age_laps: 18,
    target_tire_age_laps: 22,
    traffic_classification: 'CLEAN',
    assumptions: [],
    confidence: {
      trials: 200,
      median_margin_ms: 800,
      p10_margin_ms: -200,
      p90_margin_ms: 1800,
      probability_ahead: 0.78,
      model_seed: 1,
    },
    ...overrides,
  };
}

describe('StrategyBattlePanel', () => {
  it('renders both drivers, the gap, an OPEN window and the projected gain', () => {
    const candidate = makeDriver({driver_id: 'CAR_04', gap_to_leader_ms: 17600});
    const target = makeDriver({driver_id: 'CAR_03', gap_to_leader_ms: 14200, tire_age_laps: 22});

    render(
      <StrategyBattlePanel
        candidate={candidate}
        target={target}
        result={makeUndercutResult()}
        isEngineUnavailable={false}
        errorMessage={null}
      />,
    );

    expect(screen.getByTestId('strategy-battle-candidate').props.children.join('')).toContain('18 laps');
    expect(screen.getByTestId('strategy-battle-target').props.children.join('')).toContain('22 laps');
    expect(screen.getByText('UNDERCUT OPEN')).toBeTruthy();
    expect(screen.getByText('PROJECTED GAIN +0.8 sec')).toBeTruthy();
  });

  it('shows the exact "STRATEGY MODEL UNAVAILABLE" text when the engine is unavailable', () => {
    render(
      <StrategyBattlePanel
        candidate={makeDriver()}
        target={makeDriver({driver_id: 'CAR_03'})}
        result={null}
        isEngineUnavailable
        errorMessage="STRATEGY MODEL UNAVAILABLE"
      />,
    );
    expect(screen.getByTestId('strategy-battle-unavailable')).toBeTruthy();
    expect(screen.getByText('STRATEGY MODEL UNAVAILABLE')).toBeTruthy();
  });

  it('shows a loading state before any result has arrived', () => {
    render(
      <StrategyBattlePanel
        candidate={makeDriver()}
        target={makeDriver({driver_id: 'CAR_03'})}
        result={null}
        isEngineUnavailable={false}
        errorMessage={null}
      />,
    );
    expect(screen.getByTestId('strategy-battle-loading')).toBeTruthy();
  });

  it('renders CLOSED windows without the OPEN styling text', () => {
    render(
      <StrategyBattlePanel
        candidate={makeDriver()}
        target={makeDriver({driver_id: 'CAR_03'})}
        result={makeUndercutResult({window: 'CLOSED', expected_margin_ms: -300})}
        isEngineUnavailable={false}
        errorMessage={null}
      />,
    );
    expect(screen.getByText('UNDERCUT CLOSED')).toBeTruthy();
    expect(screen.getByText('PROJECTED GAIN -0.3 sec')).toBeTruthy();
  });
});
