import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {AlertsPanel} from '../components/AlertsPanel';
import {Alert, RaceState} from '../api/types';

function makeRaceState(): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 5,
    lap: 27,
    total_laps: 58,
    race_time_ms: 0,
    track_state: 'GREEN',
    generated_at: new Date().toISOString(),
    scenario_id: 'scenario-demo-01',
    synthetic: true,
    drivers: [],
  };
}

function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    alert_id: 'alert-1',
    evaluation_id: 'eval-1',
    watch_id: 'watch-1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 5,
    previous_status: 'CLOSED',
    new_status: 'OPEN',
    triggering_snapshot: makeRaceState(),
    deterministic_result: {
      calculation_id: 'calc-1',
      model_version: 'v1',
      race_id: 'demo-race-001',
      run_id: 'run-a',
      source_state_version: 5,
      candidate: 'CAR_03',
      target: 'CAR_04',
      window: 'OPEN',
      expected_margin_ms: 1200,
      comparison_horizon_lap: 30,
      candidate_tire_age_laps: 22,
      target_tire_age_laps: 18,
      traffic_classification: 'CLEAR_AIR',
      assumptions: ['deterministic pace model'],
      confidence: {
        trials: 500,
        median_margin_ms: 1150,
        p10_margin_ms: 400,
        p90_margin_ms: 1900,
        probability_ahead: 0.82,
        model_seed: 42,
      },
    },
    assumptions: ['deterministic pace model'],
    model_version: 'v1',
    idempotency_key: 'idem-1',
    created_at: '2026-01-01T12:00:00.000Z',
    ...overrides,
  };
}

describe('AlertsPanel', () => {
  it('shows an empty-state message when there are no alerts', () => {
    render(<AlertsPanel alerts={[]} errorMessage={null} />);
    expect(screen.getByTestId('alerts-empty')).toBeTruthy();
    expect(screen.queryByTestId('alerts-count')).toBeNull();
  });

  it('renders one card per alert with the candidate/target and expected margin', () => {
    const alerts = [
      makeAlert({alert_id: 'alert-1', new_status: 'OPEN'}),
      makeAlert({alert_id: 'alert-2', new_status: 'CLOSED'}),
    ];
    render(<AlertsPanel alerts={alerts} errorMessage={null} />);

    expect(screen.getByTestId('alerts-count').props.children).toBe(2);
    expect(screen.getByTestId('alert-card-alert-1')).toBeTruthy();
    expect(screen.getByTestId('alert-card-alert-2')).toBeTruthy();
    expect(screen.getAllByText('CAR_03 vs CAR_04')).toHaveLength(2);
    expect(screen.getByText('WINDOW OPEN')).toBeTruthy();
    expect(screen.getByText('WINDOW CLOSED')).toBeTruthy();
  });

  it('shows the error message alongside whatever alerts are already known', () => {
    const alerts = [makeAlert()];
    render(<AlertsPanel alerts={alerts} errorMessage="Unable to reach Pit Wall backend for alerts." />);

    expect(screen.getByTestId('alerts-error').props.children).toBe(
      'Unable to reach Pit Wall backend for alerts.',
    );
    // The already-known alert must still render alongside the error.
    expect(screen.getByTestId('alert-card-alert-1')).toBeTruthy();
    expect(screen.queryByTestId('alerts-empty')).toBeNull();
  });
});
