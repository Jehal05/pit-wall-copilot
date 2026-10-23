/**
 * Android-Keystore-backed secure storage for the device's paired demo
 * access token — spec v1.1 correction 5: "Store sensitive token material
 * using Android Keystore-backed secure storage (NOT AsyncStorage or
 * plaintext files)."
 *
 * This is a thin wrapper around `react-native-keychain`, which on Android
 * stores generic-password entries in an encrypted keystore-backed store
 * (never plain `SharedPreferences`, never a plaintext file on disk) and on
 * iOS uses the system Keychain — both are the platform-native secure
 * storage primitive, not a reimplementation of one. Kept as its own
 * module (rather than calling `react-native-keychain` directly from
 * `useDeviceAuth.ts`) for two reasons:
 *
 *   1. It gives the rest of this app exactly three operations
 *      (`loadDeviceToken`, `saveDeviceToken`, `clearDeviceToken`) instead
 *      of react-native-keychain's full generic-password API surface,
 *      which is easy to misuse (e.g. accidentally calling
 *      `setInternetCredentials` — a *different* keychain entry — instead
 *      of `setGenericPassword`, silently fragmenting where the token
 *      lives).
 *   2. It gives tests one small module to mock (`jest.doMock`, matching
 *      this repo's existing pattern for `config/authToken.ts` — see
 *      `useDeviceAuth.test.ts`) instead of needing a full
 *      `NativeModules.RNKeychainManager` mock, which is unavailable in
 *      the Node/jsdom test environment this suite runs in regardless (no
 *      Android Keystore exists there — see this repo's honest NOT RUN
 *      notes for anything that needs a real device).
 *
 * `SERVICE_NAME` scopes this app's stored entry away from any other app's
 * keychain/keystore entries on the same device (react-native-keychain
 * namespaces generic passwords by `service`).
 */
import * as Keychain from 'react-native-keychain';

const SERVICE_NAME = 'pit-wall-copilot.device-pairing-token';

export async function loadDeviceToken(): Promise<string | null> {
  const result = await Keychain.getGenericPassword({service: SERVICE_NAME});
  if (!result) {
    return null;
  }
  return result.password;
}

export async function saveDeviceToken(token: string): Promise<void> {
  // The "username" field is unused by this app (there is exactly one
  // credential, not a username/password pair) — react-native-keychain
  // still requires some value there, so a fixed, non-secret placeholder
  // is used. The token itself is the only sensitive value stored.
  await Keychain.setGenericPassword('pit-wall-copilot-device', token, {service: SERVICE_NAME});
}

export async function clearDeviceToken(): Promise<void> {
  await Keychain.resetGenericPassword({service: SERVICE_NAME});
}
