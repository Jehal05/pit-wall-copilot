import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {SimulationComparisonScreen} from '../components/SimulationComparisonScreen';
import {StrategySimulationResult} from '../api/types';

function makeResult(overrides: Partial<StrategySimulationResult> = {}): StrategySimulationResult {
  return {
    calculation_id: 'calc-2',
    model_version: 'v1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    source_state_version: 4,
    driver_id: 'CAR_04',
    pit_now_lap: 27,
    wait_lap: 30,
    wait_laps: 3,
    comparison_horizon_lap: 32,
    compound_after_pit: 'MEDIUM',
    delta_ms: 800,
    assumptions: ['clean pit-exit traffic'],
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

describe('SimulationComparisonScreen', () => {
  it('shows a loading state', () => {
    render(
      <SimulationComparisonScreen loading errorMessage={null} isEngineUnavailable={false} result={null} />,
    );
    expect(screen.getByTestId('simulation-loading')).toBeTruthy();
  });

  it('shows the exact "STRATEGY MODEL UNAVAILABLE" text on engine failure', () => {
    render(
      <SimulationComparisonScreen loading={false} errorMessage={null} isEngineUnavailable result={null} />,
    );
    expect(screen.getByText('STRATEGY MODEL UNAVAILABLE')).toBeTruthy();
  });

  it('renders PIT NOW and WAIT N LAPS columns with a positive net margin favoring pitting now', () => {
    render(
      <SimulationComparisonScreen
        loading={false}
        errorMessage={null}
        isEngineUnavailable={false}
        result={makeResult()}
      />,
    );
    expect(screen.getByText('PIT NOW')).toBeTruthy();
    expect(screen.getByText('WAIT 3 LAPS')).toBeTruthy();
    expect(screen.getByText('Pit on lap 27')).toBeTruthy();
    expect(screen.getByText('Pit on lap 30')).toBeTruthy();
    expect(screen.getByText('+0.8 s')).toBeTruthy();
    expect(screen.getByText('Pitting now is projected faster.')).toBeTruthy();
  });

  it('labels a negative delta as favoring waiting', () => {
    render(
      <SimulationComparisonScreen
        loading={false}
        errorMessage={null}
        isEngineUnavailable={false}
        result={makeResult({delta_ms: -1500})}
      />,
    );
    expect(screen.getByText('-1.5 s')).toBeTruthy();
    expect(screen.getByText('Waiting is projected faster.')).toBeTruthy();
  });

  it('shows a generic error message when the result is missing for another reason', () => {
    render(
      <SimulationComparisonScreen
        loading={false}
        errorMessage="Unknown driver_id"
        isEngineUnavailable={false}
        result={null}
      />,
    );
    expect(screen.getByTestId('simulation-error')).toBeTruthy();
    expect(screen.getByText('Unknown driver_id')).toBeTruthy();
  });
});
