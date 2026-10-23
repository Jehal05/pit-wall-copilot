import {useCallback, useEffect, useState} from 'react';
import {ApiError, pairDevice, revokeOwnDeviceToken, verifyAuthToken} from '../api/client';
import {LOCAL_DEV_AUTH_TOKEN} from '../config/authToken';
import {clearDeviceToken, loadDeviceToken, saveDeviceToken} from '../config/secureTokenStore';
import {getRuntimeAuthToken, setRuntimeAuthToken} from '../config/runtimeAuthToken';

export type DeviceAuthStatus =
  | 'checking' // still reading secure storage / verifying at startup
  | 'unpaired' // no token stored yet — show the pairing screen
  | 'paired' // a token is stored and (as of the last check) accepted
  | 'pairing'; // a pairing attempt is in flight

export interface UseDeviceAuthResult {
  status: DeviceAuthStatus;
  /** Set only while `status === 'unpaired'` after a failed pairing
   * attempt, or after an in-use token was found expired/invalid — see
   * this hook's module docstring. Cleared on the next successful pair(). */
  errorMessage: string | null;
  /** Spec v1.1 pass 3, correction 5: exchanges the SHORT pairing `code` a
   * person just typed (never the master backend secret — this device
   * never sees or stores that) for a device-specific token via
   * `POST /v1/auth/pair`, and only on success stores the returned token
   * (Android-Keystore-backed secure storage) and marks the device
   * paired. Returns true on success. */
  pair: (code: string, deviceName?: string) => Promise<boolean>;
  /** Explicit sign-out/unpair action (spec v1.1 correction 5: "explicit
   * sign-out/unpair/clear-credential action") — clears both the secure
   * store and the in-memory runtime token, and returns to 'unpaired'. */
  signOut: () => Promise<void>;
  /** AWS hackathon-final pass, P2 (Fire TV polish): applies the exact
   * same demote-on-rejected-credential handling `bootstrap()`'s own 401
   * check uses (see this hook's module docstring), but callable from
   * OUTSIDE this hook — specifically, wired as
   * `useRaceSocket`'s `onAuthFailure` in App.tsx, so a WebSocket 4401
   * (the backend rejecting this same device token — see ADR 0002)
   * returns the app to pairing exactly like a REST 401 already does,
   * instead of leaving the person on a dashboard whose live data can
   * never arrive again. Deliberately does NOT attempt a server-side
   * revoke first (unlike `signOut`, an intentional user action): the
   * credential is already known-rejected by the backend, so a revoke
   * call would be redundant at best and would itself likely just 401. */
  demoteToUnpairedDueToRejectedCredential: (message: string) => Promise<void>;
}

/**
 * Spec v1.1 correction 5: Fire TV release-build device pairing.
 *
 * On mount, checks whether this device already has a token in secure
 * storage. If so, it is verified against the backend (`/v1/auth/verify`)
 * before being trusted — a token that a previous pairing stored but that
 * the backend no longer accepts (rotated/revoked server-side, or this
 * device was paired against a different backend instance than the one
 * it's now pointed at) must not be silently reused; the device is
 * demoted back to 'unpaired' with a clear explanation, per "expired/
 * invalid auth must produce a clear Fire TV error and return to pairing/
 * auth."
 *
 * A verification failure that is NOT a rejected credential (network
 * error, backend temporarily unreachable) does NOT clear the stored
 * token or force re-pairing — that would make normal connectivity blips
 * indistinguishable from an actually-revoked credential, and would
 * defeat the point of persisting the token at all. Only an explicit 401
 * (ApiError with status 401) from the verify call is treated as
 * "invalid"; every other failure leaves the stored token in place and
 * optimistically treats the device as still paired (mutating requests
 * will surface their own error if the token turns out not to work after
 * all — see `AuthExpiredError` handling below, which applies the same
 * demote-on-401 rule uniformly to every subsequent mutating call, not
 * just this startup check).
 */
export function useDeviceAuth(): UseDeviceAuthResult {
  const [status, setStatus] = useState<DeviceAuthStatus>('checking');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap(): Promise<void> {
      // A dev build with LOCAL_DEV_AUTH_TOKEN set skips the pairing UI
      // entirely, preserving the existing "paste a token into
      // authToken.ts" local-dev workflow (spec v1.1 repair 11) untouched
      // — correction 5 is specifically about RELEASE builds having no
      // usable auth path at all, not about replacing local dev's
      // simpler existing option.
      if (typeof __DEV__ !== 'undefined' && __DEV__ && LOCAL_DEV_AUTH_TOKEN) {
        if (!cancelled) {
          setStatus('paired');
        }
        return;
      }

      const storedToken = await loadDeviceToken();
      if (cancelled) {
        return;
      }
      if (!storedToken) {
        setStatus('unpaired');
        return;
      }

      try {
        await verifyAuthToken(storedToken);
        if (cancelled) {
          return;
        }
        setRuntimeAuthToken(storedToken);
        setStatus('paired');
      } catch (error) {
        if (cancelled) {
          return;
        }
        if (error instanceof ApiError && error.status === 401) {
          await clearDeviceToken();
          setRuntimeAuthToken(null);
          setErrorMessage('This device’s pairing is no longer valid. Please pair this Fire TV again.');
          setStatus('unpaired');
        } else {
          // Network/backend-unreachable/503-unconfigured: keep the
          // stored token and proceed as paired — see docstring above.
          setRuntimeAuthToken(storedToken);
          setStatus('paired');
        }
      }
    }

    bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  const pair = useCallback(async (code: string, deviceName = 'Fire TV'): Promise<boolean> => {
    const trimmed = code.trim();
    if (!trimmed) {
      setErrorMessage('Enter the pairing code shown by your administrator.');
      return false;
    }
    setStatus('pairing');
    setErrorMessage(null);
    try {
      const result = await pairDevice(trimmed, deviceName);
      await saveDeviceToken(result.device_token);
      setRuntimeAuthToken(result.device_token);
      setStatus('paired');
      return true;
    } catch (error) {
      let message = 'Could not pair with this code. Check the value and try again.';
      if (error instanceof ApiError) {
        if (error.status === 401) {
          message =
            'This pairing code is invalid, expired, or already used. Ask your administrator for a new one.';
        } else if (error.status === 429) {
          message = 'Too many pairing attempts. Wait a moment and try again.';
        }
      } else if (error instanceof Error && error.message.includes('release build')) {
        // InsecureReleaseConfigurationError from assertSecureTransportForRelease
        message = error.message;
      }
      setErrorMessage(message);
      setStatus('unpaired');
      return false;
    }
  }, []);

  const signOut = useCallback(async (): Promise<void> => {
    // Spec v1.1 pass 3, correction 5: "unpair must revoke/discard the
    // device token" — best-effort server-side revoke (see
    // revokeOwnDeviceToken's own doc-comment for why a network failure
    // here must not block the local unpair) BEFORE clearing local
    // storage, so a token that fails to revoke server-side is still at
    // least gone from this device either way.
    const storedToken = await loadDeviceToken();
    if (storedToken) {
      await revokeOwnDeviceToken(storedToken);
    }
    await clearDeviceToken();
    setRuntimeAuthToken(null);
    setErrorMessage(null);
    setStatus('unpaired');
  }, []);

  const demoteToUnpairedDueToRejectedCredential = useCallback(async (message: string): Promise<void> => {
    await clearDeviceToken();
    setRuntimeAuthToken(null);
    setErrorMessage(message);
    setStatus('unpaired');
  }, []);

  return {status, errorMessage, pair, signOut, demoteToUnpairedDueToRejectedCredential};
}

export {getRuntimeAuthToken};
