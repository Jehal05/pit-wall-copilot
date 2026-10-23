import {useCallback, useEffect, useRef, useState} from 'react';
import {api, ApiError} from '../api/client';
import {Alert} from '../api/types';

interface UseAlertsResult {
  alerts: Alert[];
  alertsErrorMessage: string | null;
  /** Spec v1.1 repair 3: lets useRaceSocket's WATCH_ALERT handler force
   * an immediate re-fetch instead of waiting up to POLL_INTERVAL_MS for
   * the next scheduled poll — the WS event is a low-latency nudge to
   * refresh via the already-tested REST path, not a second source of
   * alert data to merge. */
  refreshNow: () => Promise<void>;
}

const POLL_INTERVAL_MS = 4000;

/**
 * Polls GET /v1/races/{raceId}/alerts (spec 49; see that route's own
 * docstring in routes.py: "Fire TV polls this to surface proactive
 * alerts"). Mirrors useRaceState's polling pattern deliberately, including
 * its unmount-safety approach: `isMountedRef` is checked immediately after
 * every `await`, right before each `setState` call, not just once at the
 * top of the poll function — see useRaceState.ts's doc-comment for why
 * that distinction matters (an in-flight request that resolves just after
 * unmount must not update state, and checking only at entry does not
 * catch that).
 *
 * A slower interval than race-state polling (4s vs 2s) is deliberate:
 * alerts are lower-frequency, "why did something change" events, not a
 * per-lap live value, so there is no need to hit the backend at the same
 * cadence as the state poll.
 *
 * Alerts already come back newest-first from the backend (its own
 * docstring says so), so this hook does not re-sort; it only replaces the
 * whole list with each fresh poll, since alerts are immutable once
 * created (no partial/optimistic merge is needed or attempted).
 */
export function useAlerts(raceId: string): UseAlertsResult {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [alertsErrorMessage, setAlertsErrorMessage] = useState<string | null>(null);
  const isMountedRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    try {
      const fetched = await api.listAlerts(raceId);
      if (!isMountedRef.current) {
        return;
      }
      setAlerts(fetched);
      setAlertsErrorMessage(null);
    } catch (err) {
      if (!isMountedRef.current) {
        return;
      }
      // Alerts are a secondary, best-effort surface: a failed poll does
      // not blank out the last-known alert list (unlike race state, there
      // is no "loading…" placeholder to fall back to), it just surfaces a
      // quiet error message alongside whatever was last shown.
      setAlertsErrorMessage(
        err instanceof ApiError ? err.message : 'Unable to reach Pit Wall backend for alerts.',
      );
    }
  }, [raceId]);

  useEffect(() => {
    isMountedRef.current = true;
    const intervalId = setInterval(fetchOnce, POLL_INTERVAL_MS);
    fetchOnce();

    return () => {
      isMountedRef.current = false;
      clearInterval(intervalId);
    };
  }, [fetchOnce]);

  return {alerts, alertsErrorMessage, refreshNow: fetchOnce};
}
