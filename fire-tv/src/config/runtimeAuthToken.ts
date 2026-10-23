/**
 * Spec v1.1 correction 5: "token acquired/entered at runtime... prefer
 * short-lived auth or a device-pairing flow" and "release builds must
 * never silently fall back to local demo credentials."
 *
 * This is the ONE place a token obtained through the runtime device-
 * pairing flow (`useDeviceAuth.ts`) lives in memory for `api/client.ts`
 * to read from when signing a mutating request. It is populated only:
 *   - after `secureTokenStore.loadDeviceToken()` finds a previously-paired
 *     token at app startup, or
 *   - immediately after a successful new pairing (`useDeviceAuth.pair`).
 *
 * This is deliberately NOT the same variable/constant as
 * `config/authToken.ts`'s `LOCAL_DEV_AUTH_TOKEN` (a separate, git-tracked,
 * always-`null`, `__DEV__`-only constant for local development without
 * going through the pairing UI at all) — `api/client.ts`'s
 * `resolveAuthToken()` consults this module for BOTH dev and release
 * builds (a dev build MAY also pair through the same flow a release build
 * uses, e.g. to test the pairing screen itself), but only ever consults
 * `LOCAL_DEV_AUTH_TOKEN` in a `__DEV__` build, never in release — see
 * that function's own docstring.
 */
let currentToken: string | null = null;

export function getRuntimeAuthToken(): string | null {
  return currentToken;
}

export function setRuntimeAuthToken(token: string | null): void {
  currentToken = token;
}
