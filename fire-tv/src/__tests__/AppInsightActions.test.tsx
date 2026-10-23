import React from 'react';
import {fireEvent, render, screen, waitFor} from '@testing-library/react-native';
import App from '../../App';
import {RaceState} from '../api/types';

// See AppFocus.test.tsx's matching comment: spec v1.1 correction 5 gates
// the dashboard behind pairing; this suite is about WHY?/SIMULATE modal
// behavior, not pairing, so the device is mocked as already paired.
jest.mock('../hooks/useDeviceAuth', () => ({
  useDeviceAuth: () => ({status: 'paired', errorMessage: null, pair: jest.fn(), signOut: jest.fn()}),
}));

// Spec v1.1 repair 11: no real default token in src/config/authToken.ts —
// the WATCH action is mutating (auth: true), so this test fixture supplies
// its own token via a module mock rather than depending on a bundled one.
jest.mock('../config/authToken', () => ({
  LOCAL_DEV_AUTH_TOKEN: 'test-fixture-demo-token',
}));

jest.setTimeout(15000);

function makeState(): RaceState {
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
    drivers: [
      {
        driver_id: 'CAR_03',
        display_name: 'M. Renaud',
        team_name: 'Northstar Racing',
        position: 3,
        gap_to_leader_ms: 14200,
        gap_to_ahead_ms: 0,
        compound: 'MEDIUM',
        tire_age_laps: 22,
        base_pace_ms: 91800,
        current_lap_pace_ms: 92050,
        pit_stops: 1,
        pit_window: [26, 31],
        traffic_factor: 0.05,
      },
      {
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
      },
    ],
  };
}

/**
 * Full-stack (fetch-mocked) integration test for the WHY? and SIMULATE
 * D-pad primary actions: press the button on the real rendered App,
 * assert the matching InsightModal opens, and assert the fetched data
 * actually reaches the screen — not just that the hook state updated in
 * isolation (useInsightActions.test.ts/useStrategyBattle.test.ts already
 * cover the hooks alone).
 */
describe('Fire TV WHY?/SIMULATE D-pad actions open their modal with fetched data', () => {
  beforeEach(() => {
    global.fetch = jest.fn((url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';

      if (typeof url === 'string' && url.includes('/alerts') && method === 'GET') {
        return Promise.resolve({ok: true, text: () => Promise.resolve('[]')});
      }
      if (typeof url === 'string' && url.includes('/demo/status')) {
        return Promise.resolve({
          ok: true,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                status: 'RUNNING',
                race_id: 'demo-race-001',
                run_id: 'run-a',
                current_lap: 27,
                state_version: 4,
              }),
            ),
        });
      }
      if (typeof url === 'string' && url.includes('/strategy/undercut')) {
        return Promise.resolve({
          ok: true,
          text: () =>
            Promise.resolve(
              JSON.stringify({
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
              }),
            ),
        });
      }
      if (typeof url === 'string' && url.includes('/strategy/simulate')) {
        return Promise.resolve({
          ok: true,
          text: () =>
            Promise.resolve(
              JSON.stringify({
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
              }),
            ),
        });
      }
      if (typeof url === 'string' && url.includes('/v1/agent/query')) {
        return Promise.resolve({
          ok: true,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                query: 'Why is the undercut window between CAR_04 and CAR_03?',
                status: 'SUPPORTED',
                confidence: 0.9,
                narrative: 'CAR_04 is closing on CAR_03 with fresher tires.',
                evidence: [
                  {type: 'RACE_STATE', source_id: 'run-a:4', field: 'gap_to_leader', value: '2.0 s'},
                ],
                created_at: new Date().toISOString(),
              }),
            ),
        });
      }
      // GET /v1/races/{id}/state
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeState())),
      });
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('WHY? opens the WHY? modal and renders the agent narrative + structured evidence (no stored alert yet)', async () => {
    render(<App />);

    const whyButton = await screen.findByTestId('action-why', {}, {timeout: 8000});
    fireEvent.press(whyButton);

    expect(await screen.findByTestId('screen-why', {}, {timeout: 8000})).toBeTruthy();
    await waitFor(
      () => expect(screen.getByText('CAR_04 is closing on CAR_03 with fresher tires.')).toBeTruthy(),
      {timeout: 8000},
    );
    expect(screen.getByText('GAP TO LEADER')).toBeTruthy();
    expect(screen.getByText('2.0 s')).toBeTruthy();
  });

  it('SIMULATE opens the SIMULATION COMPARISON modal and renders PIT NOW vs WAIT N LAPS', async () => {
    render(<App />);

    const simulateButton = await screen.findByTestId('action-simulate', {}, {timeout: 8000});
    fireEvent.press(simulateButton);

    expect(await screen.findByText('SIMULATION COMPARISON', {}, {timeout: 8000})).toBeTruthy();
    await waitFor(() => expect(screen.getByText('PIT NOW')).toBeTruthy(), {timeout: 8000});
    expect(screen.getByText('WAIT 3 LAPS')).toBeTruthy();
    expect(screen.getByText('Pit on lap 27')).toBeTruthy();
    expect(screen.getByText('Pit on lap 30')).toBeTruthy();
    expect(screen.getByText('Pitting now is projected faster.')).toBeTruthy();
  });

  it('CLOSE dismisses the modal (Back is covered by InsightModal.test.tsx)', async () => {
    render(<App />);

    const simulateButton = await screen.findByTestId('action-simulate', {}, {timeout: 8000});
    fireEvent.press(simulateButton);
    await screen.findByTestId('screen-simulate-close', {}, {timeout: 8000});

    fireEvent.press(screen.getByTestId('screen-simulate-close'));

    await waitFor(() => expect(screen.queryByText('SIMULATION COMPARISON')).toBeNull());
  });
});
