/**
 * Spec v1.1 pass 3, correction 5: "Add unit tests for missing/valid/
 * invalid/expired credentials" against the REWORKED device-pairing flow
 * — a short-lived, one-time pairing CODE exchanged for a device-specific
 * TOKEN, replacing the earlier flow that had a person type the full
 * backend mutation secret directly. `/v1/auth/pair`, `/v1/auth/verify`,
 * and `/v1/auth/revoke` are mocked via `global.fetch` per scenario, and
 * `react-native-keychain` via the manual mock at
 * `__mocks__/react-native-keychain.js`.
 */
import {act, renderHook, waitFor} from '@testing-library/react-native';
import * as Keychain from 'react-native-keychain';
import {useDeviceAuth} from '../hooks/useDeviceAuth';
import {getRuntimeAuthToken, setRuntimeAuthToken} from '../config/runtimeAuthToken';

type VerifyOutcome = 'accept' | 'reject-401' | 'reject-503' | 'network-error';
type PairOutcome = 'accept' | 'reject-401' | 'reject-429' | 'network-error';

function mockAuthEndpoints(options: {
  verify?: VerifyOutcome;
  pair?: PairOutcome;
  issuedDeviceToken?: string;
  issuedDeviceId?: string;
}) {
  const {
    verify = 'accept',
    pair = 'accept',
    issuedDeviceToken = 'issued-device-token',
    issuedDeviceId = 'device-abc123',
  } = options;

  global.fetch = jest.fn((url: string) => {
    if (typeof url === 'string' && url.includes('/v1/auth/verify')) {
      if (verify === 'accept') {
        return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{"valid": true}')});
      }
      if (verify === 'reject-401') {
        return Promise.resolve({
          ok: false,
          status: 401,
          text: () => Promise.resolve('{"error": "unauthorized", "message": "Missing or invalid token."}'),
        });
      }
      if (verify === 'reject-503') {
        return Promise.resolve({
          ok: false,
          status: 503,
          text: () => Promise.resolve('{"error": "auth_not_configured", "message": "No token configured."}'),
        });
      }
      return Promise.reject(new Error('network unreachable'));
    }
    if (typeof url === 'string' && url.includes('/v1/auth/pair')) {
      if (pair === 'accept') {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: () =>
            Promise.resolve(
              JSON.stringify({
                device_id: issuedDeviceId,
                device_token: issuedDeviceToken,
                expires_in_seconds: 2592000,
              }),
            ),
        });
      }
      if (pair === 'reject-401') {
        return Promise.resolve({
          ok: false,
          status: 401,
          text: () =>
            Promise.resolve('{"error": "invalid_pairing_code", "message": "This pairing code is invalid."}'),
        });
      }
      if (pair === 'reject-429') {
        return Promise.resolve({
          ok: false,
          status: 429,
          text: () => Promise.resolve('{"error": "rate_limited", "message": "Too many pairing attempts."}'),
        });
      }
      return Promise.reject(new Error('network unreachable'));
    }
    if (typeof url === 'string' && url.includes('/v1/auth/revoke')) {
      return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{"revoked": true}')});
    }
    return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve('{}')});
  }) as unknown as typeof fetch;
}

describe('useDeviceAuth', () => {
  beforeEach(() => {
    (Keychain as unknown as {__reset: () => void}).__reset();
    setRuntimeAuthToken(null);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  // --- missing credential -------------------------------------------
  it('reports unpaired when no token has ever been stored', async () => {
    mockAuthEndpoints({});
    const {result} = renderHook(() => useDeviceAuth());
    expect(result.current.status).toBe('checking');
    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(getRuntimeAuthToken()).toBeNull();
  });

  // --- valid credential (fresh pairing via a real code exchange) -----
  it('pair() with a code the backend accepts stores the RETURNED DEVICE TOKEN and reports paired', async () => {
    mockAuthEndpoints({pair: 'accept', issuedDeviceToken: 'brand-new-device-token'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    let succeeded: boolean = false;
    await act(async () => {
      succeeded = await result.current.pair('482913');
    });

    await waitFor(() => expect(result.current.status).toBe('paired'));
    expect(succeeded).toBe(true);
    // The stored/runtime credential is the DEVICE token the backend
    // issued, never the short code the person typed.
    expect(getRuntimeAuthToken()).toBe('brand-new-device-token');
    await expect(
      Keychain.getGenericPassword({service: 'pit-wall-copilot.device-pairing-token'}),
    ).resolves.toMatchObject({password: 'brand-new-device-token'});
  });

  it('pair() sends the pairing code to /v1/auth/pair, not /v1/auth/verify', async () => {
    mockAuthEndpoints({pair: 'accept'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    const fetchSpy = global.fetch as jest.Mock;
    fetchSpy.mockClear();

    await act(async () => {
      await result.current.pair('482913');
    });

    const pairCall = fetchSpy.mock.calls.find(
      ([url]) => typeof url === 'string' && url.includes('/v1/auth/pair'),
    );
    expect(pairCall).toBeTruthy();
    const [, options] = pairCall as [string, RequestInit];
    expect(JSON.parse(options.body as string)).toMatchObject({code: '482913'});
  });

  // --- invalid credential (rejected at pairing time) ------------------
  it('pair() with a code the backend rejects (401 — invalid/expired/reused) stays unpaired with an error message', async () => {
    mockAuthEndpoints({pair: 'reject-401'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    let succeeded: boolean = true;
    await act(async () => {
      succeeded = await result.current.pair('000000');
    });

    expect(succeeded).toBe(false);
    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(result.current.errorMessage).toMatch(/invalid, expired, or already used/i);
    expect(getRuntimeAuthToken()).toBeNull();
  });

  it('pair() rate-limited (429) surfaces a distinct message', async () => {
    mockAuthEndpoints({pair: 'reject-429'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    await act(async () => {
      await result.current.pair('482913');
    });

    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(result.current.errorMessage).toMatch(/too many pairing attempts/i);
  });

  it('pair() with a blank code is rejected client-side without a network call', async () => {
    mockAuthEndpoints({});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    const fetchSpy = global.fetch as jest.Mock;
    fetchSpy.mockClear();

    let succeeded: boolean = true;
    await act(async () => {
      succeeded = await result.current.pair('   ');
    });

    expect(succeeded).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.errorMessage).toMatch(/enter the pairing code/i));
  });

  // --- expired/invalid device token found at startup -------------------
  it('demotes an already-stored but now-rejected device token back to unpaired at startup', async () => {
    await Keychain.setGenericPassword('device', 'a-stale-device-token', {
      service: 'pit-wall-copilot.device-pairing-token',
    });
    mockAuthEndpoints({verify: 'reject-401'});

    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(result.current.errorMessage).toMatch(/no longer valid/i);
    expect(getRuntimeAuthToken()).toBeNull();

    // The stale token must actually have been cleared from secure
    // storage too, not just from the in-memory runtime token — otherwise
    // the next app launch would hit the exact same rejected credential
    // again instead of prompting for a fresh pairing.
    await expect(
      Keychain.getGenericPassword({service: 'pit-wall-copilot.device-pairing-token'}),
    ).resolves.toBe(false);
  });

  it('accepts an already-stored, still-valid device token at startup', async () => {
    await Keychain.setGenericPassword('device', 'a-good-stored-device-token', {
      service: 'pit-wall-copilot.device-pairing-token',
    });
    mockAuthEndpoints({verify: 'accept'});

    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('paired'));
    expect(getRuntimeAuthToken()).toBe('a-good-stored-device-token');
  });

  it('keeps a stored device token and stays paired through a transient network failure at startup', async () => {
    await Keychain.setGenericPassword('device', 'a-token-that-cannot-be-checked-right-now', {
      service: 'pit-wall-copilot.device-pairing-token',
    });
    mockAuthEndpoints({verify: 'network-error'});

    const {result} = renderHook(() => useDeviceAuth());
    // A network failure must NOT be treated the same as an explicit 401
    // rejection — see the hook's own docstring for why (otherwise a
    // normal connectivity blip would force re-pairing every time).
    await waitFor(() => expect(result.current.status).toBe('paired'));
    expect(getRuntimeAuthToken()).toBe('a-token-that-cannot-be-checked-right-now');
  });

  // --- sign-out / unpair -----------------------------------------------
  it('signOut() revokes the device token server-side, clears secure storage and the runtime token, and returns to unpaired', async () => {
    mockAuthEndpoints({pair: 'accept', issuedDeviceToken: 'a-good-device-token'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    await act(async () => {
      await result.current.pair('482913');
    });
    await waitFor(() => expect(result.current.status).toBe('paired'));

    const fetchSpy = global.fetch as jest.Mock;
    fetchSpy.mockClear();

    await act(async () => {
      await result.current.signOut();
    });

    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(getRuntimeAuthToken()).toBeNull();
    await expect(
      Keychain.getGenericPassword({service: 'pit-wall-copilot.device-pairing-token'}),
    ).resolves.toBe(false);

    // The server-side revoke call was made with the device's OWN token —
    // never the master secret, which this client never holds.
    const revokeCall = fetchSpy.mock.calls.find(
      ([url]) => typeof url === 'string' && url.includes('/v1/auth/revoke'),
    );
    expect(revokeCall).toBeTruthy();
    const [, options] = revokeCall as [string, RequestInit];
    expect((options.headers as Record<string, string>)['X-Demo-Access-Token']).toBe('a-good-device-token');
  });

  it('signOut() still clears local storage even if the server-side revoke request fails (best-effort)', async () => {
    mockAuthEndpoints({pair: 'accept', issuedDeviceToken: 'a-good-device-token'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    await act(async () => {
      await result.current.pair('482913');
    });
    await waitFor(() => expect(result.current.status).toBe('paired'));

    // Now make every subsequent fetch (including the revoke call) fail.
    global.fetch = jest.fn(() => Promise.reject(new Error('network unreachable'))) as unknown as typeof fetch;

    await act(async () => {
      await result.current.signOut();
    });

    await waitFor(() => expect(result.current.status).toBe('unpaired'));
    expect(getRuntimeAuthToken()).toBeNull();
    await expect(
      Keychain.getGenericPassword({service: 'pit-wall-copilot.device-pairing-token'}),
    ).resolves.toBe(false);
  });

  // --- never the master secret ------------------------------------------
  it('never sends the pairing code to /v1/auth/verify, and never uses a verify-style token field', async () => {
    mockAuthEndpoints({pair: 'accept'});
    const {result} = renderHook(() => useDeviceAuth());
    await waitFor(() => expect(result.current.status).toBe('unpaired'));

    const fetchSpy = global.fetch as jest.Mock;
    fetchSpy.mockClear();

    await act(async () => {
      await result.current.pair('482913');
    });

    const verifyCallDuringPairing = fetchSpy.mock.calls.find(
      ([url]) => typeof url === 'string' && url.includes('/v1/auth/verify'),
    );
    expect(verifyCallDuringPairing).toBeUndefined();
  });
});
