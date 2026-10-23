/**
 * Spec v1.1 correction 5: "Store sensitive token material using
 * Android-Keystore-backed secure storage. Do not store it in AsyncStorage
 * or plaintext files." These tests exercise `secureTokenStore.ts` against
 * the jest manual mock at `__mocks__/react-native-keychain.js` (a plain
 * in-memory stand-in for the real Keystore-backed native module, which
 * this Node/jsdom test environment has no access to — see that mock
 * file's own docstring). They prove `secureTokenStore`'s own
 * save/load/clear contract; they cannot and do not prove the REAL
 * Android Keystore encrypts anything at rest (that needs a real device —
 * see REMAINING_EXTERNAL_VERIFICATION.md).
 */
import * as Keychain from 'react-native-keychain';
import {clearDeviceToken, loadDeviceToken, saveDeviceToken} from '../config/secureTokenStore';

describe('secureTokenStore', () => {
  beforeEach(() => {
    (Keychain as unknown as {__reset: () => void}).__reset();
  });

  it('returns null when no token has been saved yet', async () => {
    await expect(loadDeviceToken()).resolves.toBeNull();
  });

  it('round-trips a saved token', async () => {
    await saveDeviceToken('a-device-token-123');
    await expect(loadDeviceToken()).resolves.toBe('a-device-token-123');
  });

  it('overwrites a previously saved token', async () => {
    await saveDeviceToken('first-token');
    await saveDeviceToken('second-token');
    await expect(loadDeviceToken()).resolves.toBe('second-token');
  });

  it('clears a saved token', async () => {
    await saveDeviceToken('a-device-token-123');
    await clearDeviceToken();
    await expect(loadDeviceToken()).resolves.toBeNull();
  });

  it('clearing when nothing was saved resolves without error', async () => {
    await expect(clearDeviceToken()).resolves.toBeUndefined();
  });
});
