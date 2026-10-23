import React from 'react';
import {fireEvent, render, screen, waitFor} from '@testing-library/react-native';
import App from '../../App';
import {API_BASE_URL} from '../api/client';
import {RaceState} from '../api/types';

// See AppFocus.test.tsx's matching comment: spec v1.1 correction 5 gates
// the dashboard behind pairing; this suite is about the RESET control's
// own behavior, not pairing, so the device is mocked as already paired.
jest.mock('../hooks/useDeviceAuth', () => ({
  useDeviceAuth: () => ({status: 'paired', errorMessage: null, pair: jest.fn(), signOut: jest.fn()}),
}));

// Spec v1.1 repair 11: "Local test credentials must be generated or
// supplied only within test fixtures" — there is no real default token
// anywhere in src/config/authToken.ts (it ships as `null`), so this test
// supplies its own fixture value via a module mock rather than depending
// on any bundled constant.
const TEST_FIXTURE_AUTH_TOKEN = 'test-fixture-demo-token';
jest.mock('../config/authToken', () => ({
  LOCAL_DEV_AUTH_TOKEN: 'test-fixture-demo-token',
}));

function makeState(overrides: Partial<RaceState> = {}): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 3,
    lap: 27,
    total_laps: 58,
    race_time_ms: 0,
    track_state: 'GREEN',
    generated_at: new Date().toISOString(),
    scenario_id: 'scenario-demo-01',
    synthetic: true,
    drivers: [],
    ...overrides,
  };
}

describe('Reset control', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('calls POST /v1/demo/reset with the demo access token when pressed', async () => {
    const fetchMock = jest.fn((url: string, _options?: RequestInit) => {
      if (typeof url === 'string' && url.endsWith('/v1/demo/reset')) {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(makeState({state_version: 0, lap: 24}))),
        });
      }
      if (typeof url === 'string' && url.includes('/alerts')) {
        // useAlerts polls this too; it returns a JSON array, not a
        // RaceState, so it needs its own branch (see AppFocus.test.tsx's
        // matching comment for why a catch-all mock would break it).
        return Promise.resolve({ok: true, text: () => Promise.resolve('[]')});
      }
      if (typeof url === 'string' && url.includes('/demo/status')) {
        // useSimulatorStatus polls this too; it returns a DemoStatus
        // object with a different shape from RaceState.
        return Promise.resolve({
          ok: true,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                status: 'STOPPED',
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
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<App />);
    const resetButton = await screen.findByTestId('reset-button');

    fireEvent(resetButton, 'press');

    // Wait for the FULL press -> fetch -> response.text() -> JSON.parse ->
    // onChanged -> setIsBusy(false) chain to actually settle, rather than
    // guessing how many microtask ticks it needs (the previous two blind
    // `await Promise.resolve()` calls didn't flush enough of them, so
    // useDemoControls' finally-block `setIsBusy(false)` landed after this
    // test function had already returned — outside any act(...) scope —
    // printing a benign but real "not wrapped in act(...)" warning).
    // `waitFor` polls with real timers, each flush wrapped in act(), until
    // the assertion passes or it times out; watching the button's own
    // `disabled` state go true (busy) then back to false (settled) is a
    // direct signal of that chain completing, not just a proxy for it.
    // React Native's `Pressable` surfaces `disabled` to the host node as
    // `accessibilityState.disabled`, not a literal `disabled` prop.
    await waitFor(() => {
      expect(screen.getByTestId('reset-button').props.accessibilityState.disabled).toBe(true);
    });
    await waitFor(() => {
      expect(screen.getByTestId('reset-button').props.accessibilityState.disabled).toBe(false);
    });

    const resetCall = fetchMock.mock.calls.find(
      ([url]) => typeof url === 'string' && url === `${API_BASE_URL}/v1/demo/reset`,
    );
    expect(resetCall).toBeDefined();
    const [, options] = resetCall as [string, RequestInit];
    expect(options.method).toBe('POST');
    expect((options.headers as Record<string, string>)['X-Demo-Access-Token']).toBe(TEST_FIXTURE_AUTH_TOKEN);
  });
});
