import {useCallback, useEffect, useRef, useState} from 'react';
import {api, AuthExpiredError} from '../api/client';
import {RaceState} from '../api/types';

interface UseDemoControlsOptions {
  /** Called after start/pause succeed, so the caller can trigger an
   * immediate refetch rather than waiting for the next poll tick. Not
   * used for reset — see `onReset` below. */
  onChanged?: () => void;
  /**
   * Called after reset succeeds, with the NEW RaceState the reset
   * response itself returned. This is how the new run gets established
   * as active (spec v1.1 "repair 4": "The reset response establishes the
   * new active run_id") — callers should wire this to
   * `useRaceState`'s `establishActiveRun`, not to a plain refetch:
   * `/v1/demo/reset`'s response already IS the new run's baseline state,
   * so establishing it directly is both correct (no other in-flight
   * response can race it) and one HTTP round-trip cheaper than
   * discarding it and re-fetching.
   */
  onReset?: (newState: RaceState) => void;
  /** Spec v1.1 correction 5: called (in addition to `lastActionError`
   * being set to a human-readable message) when start/pause/reset fails
   * because the device's paired token was rejected (401) rather than any
   * other failure — wire this to `useDeviceAuth`'s `signOut()` so the app
   * returns to the pairing screen instead of leaving a permanently-broken
   * set of controls behind a stale credential. */
  onAuthExpired?: () => void;
}

interface UseDemoControlsResult {
  isBusy: boolean;
  lastActionError: string | null;
  start: () => Promise<void>;
  pause: () => Promise<void>;
  reset: () => Promise<void>;
}

/**
 * Wraps the three mutating demo endpoints (start/pause/reset).
 *
 * `isMountedRef` guards every `setState` call that happens after an
 * `await` (i.e. once the action's promise actually settles), not just the
 * synchronous ones at the top of `run` — a press that's still in flight
 * when the component unmounts (e.g. the person backs out right after
 * tapping RESET) would otherwise resolve into a `setIsBusy`/
 * `setLastActionError` call on an unmounted component: harmless in
 * React 18, but a real "not wrapped in act(...)" warning in tests, since
 * it happens outside the test's synchronous render/assert scope.
 */
export function useDemoControls(options: UseDemoControlsOptions = {}): UseDemoControlsResult {
  const {onChanged, onReset, onAuthExpired} = options;
  const [isBusy, setIsBusy] = useState(false);
  const [lastActionError, setLastActionError] = useState<string | null>(null);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const run = useCallback(
    async <T>(action: () => Promise<T>, onSuccess?: (result: T) => void) => {
      setIsBusy(true);
      setLastActionError(null);
      try {
        const result = await action();
        if (isMountedRef.current) {
          onSuccess?.(result);
        }
      } catch (err) {
        if (isMountedRef.current) {
          setLastActionError(err instanceof Error ? err.message : 'Action failed.');
          if (err instanceof AuthExpiredError) {
            onAuthExpired?.();
          }
        }
      } finally {
        if (isMountedRef.current) {
          setIsBusy(false);
        }
      }
      // `onAuthExpired` is the only option value `run` itself reads
      // directly (onChanged/onReset are instead read by the fresh
      // per-render closures passed as `run`'s `action`/`onSuccess`
      // arguments below, so they never need to be in this dependency
      // list) — it must be listed here or a caller passing a new
      // `onAuthExpired` closure on a later render (e.g. one that closes
      // over a just-updated `useDeviceAuth().signOut`) would silently keep
      // invoking a stale one captured at mount.
    },
    [onAuthExpired],
  );

  return {
    isBusy,
    lastActionError,
    start: () => run(api.startDemo, () => onChanged?.()),
    pause: () => run(api.pauseDemo, () => onChanged?.()),
    reset: () =>
      run(api.resetDemo, newState => {
        if (onReset) {
          onReset(newState);
        } else {
          onChanged?.();
        }
      }),
  };
}
