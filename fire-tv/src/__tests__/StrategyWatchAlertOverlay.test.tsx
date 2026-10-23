import React from 'react';
import {act, render, screen} from '@testing-library/react-native';
import {fireEvent} from '@testing-library/react-native';
import {StrategyWatchAlertOverlay} from '../components/StrategyWatchAlertOverlay';
import {Alert, RaceState} from '../api/types';

function makeRaceState(): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 4,
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
    state_version: 4,
    previous_status: 'CLOSED',
    new_status: 'OPEN',
    triggering_snapshot: makeRaceState(),
    deterministic_result: {
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
    },
    assumptions: [],
    model_version: 'v1',
    idempotency_key: 'run-a:watch-1:4:OPEN',
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('StrategyWatchAlertOverlay', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it('renders nothing when there is no alert', () => {
    render(<StrategyWatchAlertOverlay alert={null} onDismiss={jest.fn()} />);
    expect(screen.queryByTestId('strategy-watch-alert')).toBeNull();
  });

  it('renders the exact UJ-1 mockup shape for an OPEN transition', () => {
    render(<StrategyWatchAlertOverlay alert={makeAlert()} onDismiss={jest.fn()} />);
    expect(screen.getByTestId('strategy-watch-alert')).toBeTruthy();
    expect(screen.getByText('⚠ UNDERCUT WINDOW OPEN')).toBeTruthy();
    expect(screen.getByText('CAR_04 → CAR_03')).toBeTruthy();
    expect(screen.getByText('Projected net advantage: +0.8 sec')).toBeTruthy();
    expect(screen.getByText('Probability: 78%')).toBeTruthy();
  });

  it('shows the frozen evidence lap/state_version on the card (phase 7 judge polish)', () => {
    render(<StrategyWatchAlertOverlay alert={makeAlert()} onDismiss={jest.fn()} />);
    expect(screen.getByTestId('strategy-watch-alert-evidence')).toBeTruthy();
    expect(screen.getByText('Evidence: Lap 27 · v4')).toBeTruthy();
  });

  it('calls onDismiss when the card is pressed', () => {
    const onDismiss = jest.fn();
    render(<StrategyWatchAlertOverlay alert={makeAlert()} onDismiss={onDismiss} />);
    fireEvent.press(screen.getByTestId('strategy-watch-alert-card'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('auto-dismisses after the configured delay', () => {
    const onDismiss = jest.fn();
    render(<StrategyWatchAlertOverlay alert={makeAlert()} onDismiss={onDismiss} autoDismissMs={1000} />);
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(1000));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
