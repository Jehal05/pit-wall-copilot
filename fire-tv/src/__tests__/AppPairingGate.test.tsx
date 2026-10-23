/**
 * Spec v1.1 pass 3, correction 5, end to end at the App level (unlike
 * useDeviceAuth.test.ts, which tests the hook in isolation): an unpaired
 * device sees ONLY the pairing screen, never the dashboard's mutating
 * controls; entering a pairing CODE the backend accepts (never the full
 * master secret) reveals the dashboard; and UNPAIR returns to the
 * pairing screen and revokes the device token server-side.
 */
import React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react-native';
import * as Keychain from 'react-native-keychain';
import App from '../../App';
import {RaceState} from '../api/types';
import {setRuntimeAuthToken} from '../config/runtimeAuthToken';

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
    drivers: [],
  };
}

const ISSUED_DEVICE_TOKEN = 'issued-device-token-for-this-fire-tv';

function installFetchMock(validCode: string) {
  global.fetch = jest.fn((url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/v1/auth/pair')) {
      const body = init?.body ? JSON.parse(init.body as string) : {};
      if (body.code === validCode) {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                device_id: 'device-xyz',
                device_token: ISSUED_DEVICE_TOKEN,
                expires_in_seconds: 2592000,
              }),
            ),
        });
      }
      return Promise.resolve({
        ok: false,
        status: 401,
        text: () => Promise.resolve('{"error": "invalid_pairing_code", "message": "bad code"}'),
      });
    }
    if (typeof url === 'string' && url.includes('/v1/auth/verify')) {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const presented = headers['X-Demo-Access-Token'];
      if (presented === ISSUED_DEVICE_TOKEN) {
        return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{"valid": true}')});
      }
      return Promise.resolve({
        ok: false,
        status: 401,
        text: () => Promise.resolve('{"error": "unauthorized", "message": "bad token"}'),
      });
    }
    if (typeof url === 'string' && url.includes('/v1/auth/revoke')) {
      return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{"revoked": true}')});
    }
    if (typeof url === 'string' && url.includes('/alerts')) {
      return Promise.resolve({ok: true, text: () => Promise.resolve('[]')});
    }
    if (typeof url === 'string' && url.includes('/demo/status')) {
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
    return Promise.resolve({ok: true, text: () => Promise.resolve(JSON.stringify(makeState()))});
  }) as unknown as typeof fetch;
}

describe('App device-pairing gate', () => {
  beforeEach(() => {
    (Keychain as unknown as {__reset: () => void}).__reset();
    setRuntimeAuthToken(null);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the pairing screen, not the dashboard, when unpaired', async () => {
    installFetchMock('482913');
    const {unmount} = render(<App />);

    await screen.findByTestId('pairing-screen');
    expect(screen.queryByTestId('start-button')).toBeNull();
    unmount();
  });

  it('never renders a field that could hold the full backend master secret', async () => {
    installFetchMock('482913');
    const {unmount} = render(<App />);

    await screen.findByTestId('pairing-screen');
    expect(screen.queryByTestId('pairing-token-input')).toBeNull();
    unmount();
  });

  it('reveals the dashboard after entering an accepted pairing CODE', async () => {
    installFetchMock('482913');
    const {unmount} = render(<App />);

    await screen.findByTestId('pairing-screen');
    fireEvent.changeText(screen.getByTestId('pairing-code-input'), '482913');
    await act(async () => {
      fireEvent.press(screen.getByTestId('pair-button'));
    });

    await waitFor(() => expect(screen.queryByTestId('pairing-screen')).toBeNull(), {timeout: 8000});
    await screen.findByTestId('start-button', {}, {timeout: 8000});
    unmount();
  });

  it('shows a pairing-screen error and stays gated when the code is rejected', async () => {
    installFetchMock('482913');
    const {unmount} = render(<App />);

    await screen.findByTestId('pairing-screen');
    fireEvent.changeText(screen.getByTestId('pairing-code-input'), '000000');
    await act(async () => {
      fireEvent.press(screen.getByTestId('pair-button'));
    });

    await screen.findByTestId('pairing-error');
    expect(screen.queryByTestId('start-button')).toBeNull();
    unmount();
  });

  it('UNPAIR returns the dashboard to the pairing screen and revokes the device token server-side', async () => {
    installFetchMock('482913');
    const {unmount} = render(<App />);

    await screen.findByTestId('pairing-screen');
    fireEvent.changeText(screen.getByTestId('pairing-code-input'), '482913');
    await act(async () => {
      fireEvent.press(screen.getByTestId('pair-button'));
    });
    await screen.findByTestId('start-button', {}, {timeout: 8000});

    const fetchSpy = global.fetch as jest.Mock;
    fetchSpy.mockClear();

    await act(async () => {
      fireEvent.press(screen.getByTestId('unpair-button'));
    });

    await screen.findByTestId('pairing-screen');
    const revokeCall = fetchSpy.mock.calls.find(
      ([url]) => typeof url === 'string' && url.includes('/v1/auth/revoke'),
    );
    expect(revokeCall).toBeTruthy();
    unmount();
  });
});
