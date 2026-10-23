import React from 'react';
import {render, screen, waitFor, act} from '@testing-library/react-native';
import App from '../../App';
import {RaceEvent, RaceState} from '../api/types';

/**
 * Full-stack (fetch + WebSocket mocked) integration tests proving spec
 * v1.1 pass 4, mandatory correction 4: "Connect WebSocket events to the
 * real UI" — every backend-emitted event type causes an observable,
 * correct change on the real rendered App, not just an internal hook
 * state update. useRaceSocket.test.ts already covers the hook's own
 * accept/reject logic in isolation; this file is the App.tsx wiring
 * itself.
 */

jest.mock('../hooks/useDeviceAuth', () => ({
  useDeviceAuth: () => ({status: 'paired', errorMessage: null, pair: jest.fn(), signOut: jest.fn()}),
}));

jest.mock('../config/authToken', () => ({
  LOCAL_DEV_AUTH_TOKEN: 'test-fixture-demo-token',
}));

jest.setTimeout(15000);

/** A controllable fake WebSocket, matching useRaceSocket.test.ts's own —
 * this file needs the SAME capability (drive open/message from the
 * outside) but against the REAL App component instead of the bare hook. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: {data: string}) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {}
  send() {}

  simulateOpen() {
    this.onopen?.();
  }
  simulateMessage(event: RaceEvent) {
    this.onmessage?.({data: JSON.stringify(event)});
  }
}

function makeState(overrides: Partial<RaceState> = {}): RaceState {
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
    ...overrides,
  };
}

function makeEvent(overrides: Partial<RaceEvent> = {}): RaceEvent {
  return {
    type: 'RACE_STATE',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 4,
    generated_at: new Date().toISOString(),
    payload: makeState(),
    ...overrides,
  };
}

describe('App.tsx connects every WebSocket event to real, observable UI (spec v1.1 pass 4, correction 4)', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    // @ts-expect-error test-only override, matching useRaceSocket.test.ts
    global.WebSocket = FakeWebSocket;

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
                window: 'CLOSED',
                expected_margin_ms: -200,
                comparison_horizon_lap: 29,
                candidate_tire_age_laps: 18,
                target_tire_age_laps: 22,
                traffic_classification: 'CLEAN',
                assumptions: [],
                confidence: {
                  trials: 200,
                  median_margin_ms: -200,
                  p10_margin_ms: -900,
                  p90_margin_ms: 400,
                  probability_ahead: 0.4,
                  model_seed: 1,
                },
              }),
            ),
        });
      }
      // GET /v1/races/{id}/state
      return Promise.resolve({ok: true, text: () => Promise.resolve(JSON.stringify(makeState()))});
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('STRATEGY_UPDATE pushes the result straight into the Strategy Battle panel', async () => {
    render(<App />);
    await screen.findByTestId('strategy-battle-window', {}, {timeout: 8000});
    expect(screen.getByTestId('strategy-battle-window').props.children).toContain('CLOSED');

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    act(() =>
      socket.simulateMessage(
        makeEvent({
          type: 'STRATEGY_UPDATE',
          state_version: 5,
          payload: {
            kind: 'undercut',
            result: {
              calculation_id: 'calc-pushed',
              model_version: 'v1',
              race_id: 'demo-race-001',
              run_id: 'run-a',
              source_state_version: 5,
              candidate: 'CAR_04',
              target: 'CAR_03',
              window: 'OPEN',
              expected_margin_ms: 900,
              comparison_horizon_lap: 29,
              candidate_tire_age_laps: 18,
              target_tire_age_laps: 22,
              traffic_classification: 'CLEAN',
              assumptions: [],
              confidence: {
                trials: 200,
                median_margin_ms: 900,
                p10_margin_ms: 100,
                p90_margin_ms: 1700,
                probability_ahead: 0.81,
                model_seed: 1,
              },
            },
          },
        }),
      ),
    );

    await waitFor(() =>
      expect(screen.getByTestId('strategy-battle-window').props.children).toContain('OPEN'),
    );
  });

  it('WATCH_ALERT shows the proactive overlay when data is fresh', async () => {
    render(<App />);
    await screen.findByTestId('strategy-battle-window', {}, {timeout: 8000});
    expect(screen.queryByTestId('strategy-watch-alert')).toBeNull();

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    act(() =>
      socket.simulateMessage(
        makeEvent({
          type: 'WATCH_ALERT',
          state_version: 5,
          payload: {
            alert_id: 'alert-live-1',
            evaluation_id: 'eval-1',
            watch_id: 'watch-1',
            race_id: 'demo-race-001',
            run_id: 'run-a',
            state_version: 5,
            previous_status: 'CLOSED',
            new_status: 'OPEN',
            triggering_snapshot: makeState({state_version: 5}),
            deterministic_result: {
              calculation_id: 'calc-alert',
              model_version: 'v1',
              race_id: 'demo-race-001',
              run_id: 'run-a',
              source_state_version: 5,
              candidate: 'CAR_04',
              target: 'CAR_03',
              window: 'OPEN',
              expected_margin_ms: 900,
              comparison_horizon_lap: 29,
              candidate_tire_age_laps: 18,
              target_tire_age_laps: 22,
              traffic_classification: 'CLEAN',
              assumptions: [],
              confidence: {
                trials: 200,
                median_margin_ms: 900,
                p10_margin_ms: 100,
                p90_margin_ms: 1700,
                probability_ahead: 0.81,
                model_seed: 1,
              },
            },
            assumptions: [],
            model_version: 'v1',
            idempotency_key: 'idem-1',
            created_at: new Date().toISOString(),
          },
        }),
      ),
    );

    expect(await screen.findByTestId('strategy-watch-alert')).toBeTruthy();
  });

  it('AGENT_RESPONSE opens the WHY? screen with the pushed narrative (proves an Alexa-triggered answer reaches Fire TV)', async () => {
    render(<App />);
    await screen.findByTestId('strategy-battle-window', {}, {timeout: 8000});
    expect(screen.queryByTestId('screen-why')).toBeNull();

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    act(() =>
      socket.simulateMessage(
        makeEvent({
          type: 'AGENT_RESPONSE',
          state_version: 5,
          payload: {
            query: 'Why is the undercut window between CAR_04 and CAR_03?',
            status: 'SUPPORTED',
            confidence: 0.92,
            narrative: 'Pushed from Alexa+: CAR_04 has a fresher-tire advantage right now.',
            evidence: [{type: 'RACE_STATE', source_id: 'run-a:5', field: 'tire_age_delta', value: '4 laps'}],
            created_at: new Date().toISOString(),
          },
        }),
      ),
    );

    expect(await screen.findByTestId('screen-why')).toBeTruthy();
    await waitFor(() =>
      expect(
        screen.getByText('Pushed from Alexa+: CAR_04 has a fresher-tire advantage right now.'),
      ).toBeTruthy(),
    );
  });

  it("RACE_RESET clears the previous run's watch-alert overlay", async () => {
    render(<App />);
    await screen.findByTestId('strategy-battle-window', {}, {timeout: 8000});

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    act(() =>
      socket.simulateMessage(
        makeEvent({
          type: 'WATCH_ALERT',
          state_version: 5,
          payload: {
            alert_id: 'alert-before-reset',
            evaluation_id: 'eval-1',
            watch_id: 'watch-1',
            race_id: 'demo-race-001',
            run_id: 'run-a',
            state_version: 5,
            previous_status: 'CLOSED',
            new_status: 'OPEN',
            triggering_snapshot: makeState({state_version: 5}),
            deterministic_result: {
              calculation_id: 'calc-alert',
              model_version: 'v1',
              race_id: 'demo-race-001',
              run_id: 'run-a',
              source_state_version: 5,
              candidate: 'CAR_04',
              target: 'CAR_03',
              window: 'OPEN',
              expected_margin_ms: 900,
              comparison_horizon_lap: 29,
              candidate_tire_age_laps: 18,
              target_tire_age_laps: 22,
              traffic_classification: 'CLEAN',
              assumptions: [],
              confidence: {
                trials: 200,
                median_margin_ms: 900,
                p10_margin_ms: 100,
                p90_margin_ms: 1700,
                probability_ahead: 0.81,
                model_seed: 1,
              },
            },
            assumptions: [],
            model_version: 'v1',
            idempotency_key: 'idem-1',
            created_at: new Date().toISOString(),
          },
        }),
      ),
    );
    await screen.findByTestId('strategy-watch-alert');

    act(() =>
      socket.simulateMessage(
        makeEvent({
          type: 'RACE_RESET',
          run_id: 'run-b',
          state_version: 0,
          payload: makeState({run_id: 'run-b', state_version: 0, lap: 24}),
        }),
      ),
    );

    await waitFor(() => expect(screen.queryByTestId('strategy-watch-alert')).toBeNull());
  });
});
