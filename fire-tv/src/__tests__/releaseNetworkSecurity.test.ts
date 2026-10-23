import * as fs from 'fs';
import * as path from 'path';

/**
 * Spec v1.1 pass 3, correction 8: "Verify release network security."
 * Covers both halves of the requirement — the JS-level guard
 * (`assertSecureTransportForRelease` in `api/client.ts`) and the Android
 * manifest configuration (no cleartext permission in a release build).
 */
describe('release network security (spec v1.1 pass 3, correction 8)', () => {
  const originalDev = (global as {__DEV__?: boolean}).__DEV__;

  afterEach(() => {
    (global as {__DEV__?: boolean}).__DEV__ = originalDev;
    jest.resetModules();
  });

  function loadClientWithDevFlag(isDev: boolean) {
    (global as {__DEV__?: boolean}).__DEV__ = isDev;
    jest.resetModules();

    return require('../api/client');
  }

  it('a dev/debug bundle may use a plaintext http:// base URL (local emulator/device)', () => {
    const {assertSecureTransportForRelease} = loadClientWithDevFlag(true);
    expect(() => assertSecureTransportForRelease('http://10.0.2.2:8000')).not.toThrow();
  });

  it('a dev/debug bundle may use a plaintext ws:// URL', () => {
    const {assertSecureTransportForRelease} = loadClientWithDevFlag(true);
    expect(() => assertSecureTransportForRelease('ws://10.0.2.2:8000/v1/races/demo/ws')).not.toThrow();
  });

  it('a release bundle rejects a plaintext http:// base URL', () => {
    const {assertSecureTransportForRelease, InsecureReleaseConfigurationError} = loadClientWithDevFlag(false);
    expect(() => assertSecureTransportForRelease('http://api.example.com')).toThrow(
      InsecureReleaseConfigurationError,
    );
  });

  it('a release bundle rejects a plaintext ws:// URL', () => {
    const {assertSecureTransportForRelease, InsecureReleaseConfigurationError} = loadClientWithDevFlag(false);
    expect(() => assertSecureTransportForRelease('ws://api.example.com/v1/races/demo/ws')).toThrow(
      InsecureReleaseConfigurationError,
    );
  });

  it('a release bundle accepts an https:// base URL', () => {
    const {assertSecureTransportForRelease} = loadClientWithDevFlag(false);
    expect(() => assertSecureTransportForRelease('https://api.example.com')).not.toThrow();
  });

  it('a release bundle accepts a wss:// URL', () => {
    const {assertSecureTransportForRelease} = loadClientWithDevFlag(false);
    expect(() => assertSecureTransportForRelease('wss://api.example.com/v1/races/demo/ws')).not.toThrow();
  });

  it('the release configuration error message never includes any credential value', () => {
    const {assertSecureTransportForRelease} = loadClientWithDevFlag(false);
    try {
      assertSecureTransportForRelease('http://api.example.com');
      fail('expected assertSecureTransportForRelease to throw');
    } catch (err) {
      const message = (err as Error).message;
      expect(message).not.toMatch(/token|secret|password/i);
    }
  });

  describe('Android manifest cleartext configuration', () => {
    const mainManifestPath = path.resolve(__dirname, '../../android/app/src/main/AndroidManifest.xml');
    const debugManifestPath = path.resolve(__dirname, '../../android/app/src/debug/AndroidManifest.xml');

    it('the main (release-applicable) manifest explicitly disallows cleartext traffic', () => {
      const manifest = fs.readFileSync(mainManifestPath, 'utf8');
      expect(manifest).toMatch(/android:usesCleartextTraffic="false"/);
    });

    it('no release-specific manifest override re-enables cleartext traffic', () => {
      const releaseManifestPath = path.resolve(
        __dirname,
        '../../android/app/src/release/AndroidManifest.xml',
      );
      expect(fs.existsSync(releaseManifestPath)).toBe(false);
    });

    it('only the debug-variant manifest overrides cleartext traffic back to true (local dev only)', () => {
      const debugManifest = fs.readFileSync(debugManifestPath, 'utf8');
      expect(debugManifest).toMatch(/android:usesCleartextTraffic="true"/);
    });
  });

  // Spec v1.1 pass 4, mandatory correction 5: "Ensure every REST and
  // WebSocket operation invokes the central release transport
  // validation ... Specifically test pairing, verification, unpair/
  // revoke, ordinary reads, mutations." Before this pass,
  // `revokeOwnDeviceToken` called the raw `fetch()` directly and skipped
  // the guard entirely — these tests would have failed against that
  // code (a real regression test, not just new coverage), and now pass
  // because every one of these functions routes through the shared
  // `secureFetch` wrapper (see client.ts's own doc-comment on it).
  describe('every network call site invokes the release transport guard (spec v1.1 pass 4, correction 5)', () => {
    function loadClientAsReleaseWithFetchSpy() {
      const client = loadClientWithDevFlag(false);
      const fetchSpy = jest.fn();
      (global as {fetch?: typeof fetch}).fetch = fetchSpy as unknown as typeof fetch;
      return {client, fetchSpy};
    }

    it('an ordinary read (api.getRaceState) is rejected before any request is sent', async () => {
      const {client, fetchSpy} = loadClientAsReleaseWithFetchSpy();
      await expect(client.api.getRaceState('demo-race-001')).rejects.toThrow(
        client.InsecureReleaseConfigurationError,
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('a mutation (api.startDemo) never reaches the network in a release build with no https:// base URL configured', async () => {
      // In this fixture's release build, `startDemo` can be rejected by
      // EITHER guard — no runtime auth token has been paired yet
      // (AuthNotConfiguredError) or the insecure base URL
      // (InsecureReleaseConfigurationError) — and which one fires first
      // is an implementation detail of `request()`'s ordering, not the
      // property this test cares about. What matters, and what a
      // regression here would break, is that NEITHER path ever reaches
      // the network.
      const {client, fetchSpy} = loadClientAsReleaseWithFetchSpy();
      await expect(client.api.startDemo()).rejects.toThrow();
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('verifyAuthToken is rejected before any request is sent', async () => {
      const {client, fetchSpy} = loadClientAsReleaseWithFetchSpy();
      await expect(client.verifyAuthToken('some-candidate-token')).rejects.toThrow(
        client.InsecureReleaseConfigurationError,
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('pairDevice (the pairing exchange) is rejected before any request is sent', async () => {
      const {client, fetchSpy} = loadClientAsReleaseWithFetchSpy();
      await expect(client.pairDevice('123456', 'Living Room Fire TV')).rejects.toThrow(
        client.InsecureReleaseConfigurationError,
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('revokeOwnDeviceToken (unpair) never sends the device token over plaintext, even though it swallows the resulting error', async () => {
      // This is the exact regression this correction fixes: before pass
      // 4, this specific call site skipped the guard and would have let
      // `fetchSpy` below be called with the real device token in a
      // header, in a misconfigured release build.
      const {client, fetchSpy} = loadClientAsReleaseWithFetchSpy();
      await expect(client.revokeOwnDeviceToken('a-real-device-token')).resolves.toBeUndefined();
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('the same secureFetch-routed call DOES reach fetch once the transport check passes (a dev/debug build)', async () => {
      // Sanity check that the guard — not a general breakage of
      // `secureFetch`/`revokeOwnDeviceToken` — is what blocked every
      // case above. `@env`'s `API_BASE_URL` is a Babel-plugin virtual
      // import that cannot be overridden via `jest.doMock` (there is no
      // real module behind it to mock), so this exercises the OTHER
      // side of the same guard instead: a dev/debug build, where
      // `assertSecureTransportForRelease` is a deliberate no-op — see
      // that function's own doc-comment — and the plaintext `.env` URL
      // this test run already uses is allowed through.
      const client = loadClientWithDevFlag(true);
      const fetchSpy = jest.fn(() => Promise.resolve({ok: true, text: () => Promise.resolve('{}')}));
      (global as {fetch?: typeof fetch}).fetch = fetchSpy as unknown as typeof fetch;

      await client.revokeOwnDeviceToken('a-real-device-token');
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
  });
});
