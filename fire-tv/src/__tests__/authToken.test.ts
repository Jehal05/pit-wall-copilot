/**
 * Spec v1.1 repair 11 — client-side half: "Release builds must fail or
 * refuse mutation requests when secure authorization is not configured."
 *
 * These tests exercise api/client.ts's `request()` auth handling directly,
 * mocking `src/config/authToken` per-test so each scenario controls
 * exactly what LOCAL_DEV_AUTH_TOKEN resolves to, without depending on
 * that file's real (git-tracked, always-null) default.
 *
 * Uses `jest.resetModules()` + `require()` (rather than dynamic `import()`)
 * so each test gets a fresh, differently-mocked copy of api/client.ts —
 * the module resolves its token at call time via a local function, but
 * `jest.doMock` only affects modules required *after* it runs, so a fresh
 * require is needed per scenario regardless.
 */

type ClientModule = typeof import('../api/client');

function loadClientWithMockedToken(token: string | null): ClientModule {
  jest.doMock('../config/authToken', () => ({LOCAL_DEV_AUTH_TOKEN: token}));

  return require('../api/client');
}

describe('client auth token resolution', () => {
  beforeEach(() => {
    jest.resetModules();
  });

  afterEach(() => {
    jest.restoreAllMocks();
    // @ts-expect-error test-only override of the RN global
    global.__DEV__ = true;
  });

  it('throws AuthNotConfiguredError before sending a request when no token is configured', async () => {
    const fetchMock = jest.fn();

    (global as any).fetch = fetchMock;

    const {api, AuthNotConfiguredError} = loadClientWithMockedToken(null);

    await expect(api.startDemo()).rejects.toBeInstanceOf(AuthNotConfiguredError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('never consults LOCAL_DEV_AUTH_TOKEN when __DEV__ is false (release build), and never sends a request', async () => {
    // @ts-expect-error test-only override of the RN global
    global.__DEV__ = false;
    const fetchMock = jest.fn();

    (global as any).fetch = fetchMock;

    const {api} = loadClientWithMockedToken('a-real-looking-token');

    // As of spec v1.1 pass 3, correction 8, a release build also rejects
    // this .env-configured http:// API base URL outright (before auth
    // token resolution is even reached) — see
    // releaseNetworkSecurity.test.ts for that check in isolation. Either
    // way, the security property this test exists to prove — a release
    // build never sends LOCAL_DEV_AUTH_TOKEN over the wire — holds: no
    // request is ever made, regardless of which guard fires first.
    await expect(api.startDemo()).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sends the configured token when LOCAL_DEV_AUTH_TOKEN is set in a dev build', async () => {
    const fetchMock = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve('{"status":"RUNNING","current_lap":24}'),
      } as Response),
    );

    (global as any).fetch = fetchMock;

    const {api} = loadClientWithMockedToken('dev-fixture-token');
    await api.startDemo();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((options.headers as Record<string, string>)['X-Demo-Access-Token']).toBe('dev-fixture-token');
  });
});
