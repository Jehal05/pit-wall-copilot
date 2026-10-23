import React from 'react';
import {act, render, screen, waitFor, within} from '@testing-library/react-native';
import App from '../../App';
import {RaceState} from '../api/types';

// Spec v1.1 correction 5 gates the whole dashboard behind device pairing
// (see App.tsx / useDeviceAuth.ts) — this suite is about D-pad focus on
// the DASHBOARD's own controls, a separate concern already covered by
// its own tests (useDeviceAuth.test.ts, PairingScreen.test.tsx), so it
// mocks the device as already paired rather than re-deriving that
// through the real pairing flow in every test here.
jest.mock('../hooks/useDeviceAuth', () => ({
  useDeviceAuth: () => ({status: 'paired', errorMessage: null, pair: jest.fn(), signOut: jest.fn()}),
}));

function makeState(): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 0,
    lap: 24,
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

describe('Fire TV dashboard D-pad focus', () => {
  beforeEach(() => {
    // Branch on URL: App now also polls GET /v1/races/{id}/alerts (useAlerts),
    // which returns a JSON array, not a RaceState object — a single
    // catch-all mock would hand that endpoint a RaceState and break
    // AlertsPanel's `alerts.map`/`alerts.length` calls.
    global.fetch = jest.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/alerts')) {
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
                current_lap: 24,
                state_version: 0,
              }),
            ),
        });
      }
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeState())),
      });
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders Start, Pause and Reset as focusable controls reachable via D-pad', async () => {
    const {unmount} = render(<App />);

    const startButton = await screen.findByTestId('start-button', {}, {timeout: 8000});
    const pauseButton = await screen.findByTestId('pause-button', {}, {timeout: 8000});
    const resetButton = await screen.findByTestId('reset-button', {}, {timeout: 8000});

    // Each control must be focusable so Fire TV's D-pad focus engine can
    // reach it (Android TV maps D-pad navigation to standard RN focus).
    expect(startButton.props.focusable).toBe(true);
    expect(pauseButton.props.focusable).toBe(true);
    expect(resetButton.props.focusable).toBe(true);

    // Start carries initial TV focus per the Gate 0 UX requirement.
    expect(startButton.props.hasTVPreferredFocus).toBe(true);

    // Let the mount-triggered initial fetch actually settle (it resolves
    // asynchronously, independent of when the buttons themselves become
    // queryable) before the test returns — otherwise its state update
    // lands after this test function's promise resolves, outside any
    // act(...) scope, and prints a benign but real "not wrapped in
    // act(...)" warning. Asserting on the badge's real rendered text
    // (scoped with `within` since "LIVE" is ambiguous — the simulator
    // badge can show the same label) makes this a genuine assertion,
    // not just a wait.
    await waitFor(() => {
      expect(within(screen.getByTestId('connection-badge')).getByText('LIVE')).toBeTruthy();
    });

    // Deterministic cleanup: this test uses REAL timers (it isn't testing
    // timer/staleness behavior — see the DATA STALE describe block below
    // for that, which correctly uses fake timers). useRaceState's and
    // useAlerts's poll loops both schedule real setInterval/setTimeout
    // calls on mount; without an explicit unmount here, those keep firing
    // with real timers for the rest of this test FILE's --runInBand
    // process, contending for real CPU time against whatever test runs
    // next and making ITS otherwise-generous timeouts flaky — this is the
    // root cause of this test's own observed one-off timeout (a genuinely
    // deterministic effect, not "the environment was slow this time").
    unmount();
  }, 10000);
});

/**
 * Spec v1.1 repair 14 / Section 29 "Race data stale": wires the same
 * `isStale` flag `useRaceSocket.test.ts` already unit-tests in isolation
 * into the actual dashboard screen — this is the one line in App.tsx
 * (`{isStale && <StaleDataBadge />}`) that connects the two, so it gets
 * its own end-to-end check rather than trusting the wiring by inspection.
 */
describe('Fire TV dashboard DATA STALE indicator', () => {
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
    simulateMessage(payload: unknown) {
      this.onmessage?.({data: JSON.stringify(payload)});
    }
  }

  beforeEach(() => {
    jest.useFakeTimers();
    FakeWebSocket.instances = [];
    // @ts-expect-error test-only override of jest.setup.js's default InertWebSocket
    global.WebSocket = FakeWebSocket;

    global.fetch = jest.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/alerts')) {
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
                current_lap: 24,
                state_version: 0,
              }),
            ),
        });
      }
      return Promise.resolve({ok: true, text: () => Promise.resolve(JSON.stringify(makeState()))});
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('shows DATA STALE after 5s of socket silence and hides it again once a fresh message arrives', async () => {
    const {unmount} = render(<App />);

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0));
    expect(screen.queryByTestId('stale-data-badge')).toBeNull();

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());

    // Past the 5000ms staleness threshold, on the next 1000ms check tick
    // (see useRaceSocket.ts's own STALE_CHECK_INTERVAL_MS/STALE_THRESHOLD_MS
    // and useRaceSocket.test.ts's identical 6000ms margin). The 2000ms/
    // 4000ms REST-poll intervals (useRaceState/useAlerts) also elapse
    // during this advance and resolve asynchronously, so this is wrapped
    // in `waitFor` (which itself act()-wraps each poll) rather than a
    // bare synchronous assertion straight after a synchronous `act`.
    act(() => jest.advanceTimersByTime(6000));
    await waitFor(() => {
      expect(screen.getByTestId('stale-data-badge')).toBeTruthy();
    });
    expect(screen.getByText('DATA STALE')).toBeTruthy();

    act(() =>
      socket.simulateMessage({
        type: 'RACE_STATE',
        race_id: 'demo-race-001',
        run_id: 'run-a',
        state_version: 1,
        generated_at: new Date().toISOString(),
        payload: makeState(),
      }),
    );
    act(() => jest.advanceTimersByTime(1000));

    await waitFor(() => {
      expect(screen.queryByTestId('stale-data-badge')).toBeNull();
    });

    // Unmount before the test ends so every effect's cleanup runs (socket
    // close, clearInterval on both REST poll loops, clearTimeout on any
    // pending reconnect backoff) — otherwise those keep firing against
    // fake timers that are about to be torn down in afterEach, which can
    // leave stray scheduled callbacks behind for whatever test file runs
    // next in the same --runInBand process.
    unmount();
  });
});
