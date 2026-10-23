import {useCallback, useEffect, useRef, useState} from 'react';
import {api} from '../api/client';
import {SimulatorStatus} from '../api/types';

interface UseSimulatorStatusResult {
  status: SimulatorStatus | null;
  refreshNow: () => Promise<void>;
  /** WebSocket control events (RACE_STATE/STRATEGY_UPDATE carry no status
   * field today, but a future control-plane event can call this to push
   * an authoritative status update immediately, the same way
   * `establishActiveRun` short-circuits useRaceState's poll loop). */
  applyAuthoritativeStatus: (status: SimulatorStatus) => void;
}

const POLL_INTERVAL_MS = 2000;

/**
 * Spec v1.1 "repair 5": the Fire TV dashboard previously INFERRED
 * RUNNING/STOPPED from the lap number alone (`lap >= 28 ? 'COMPLETED' :
 * 'RUNNING'` in the old App.tsx), which meant it could display RUNNING
 * while the simulator was actually STOPPED or PAUSED at that same lap —
 * lap number and run/pause state are simply independent facts, and one
 * cannot be reconstructed from the other.
 *
 * This hook polls `GET /v1/demo/status` — the backend's own authoritative
 * STOPPED/RUNNING/PAUSED/COMPLETED record (app/services/simulator.py's
 * `SimulatorStatus`, unrelated to lap number) — instead. It follows the
 * same isMountedRef-after-every-await pattern as useRaceState/useAlerts
 * for the same reason (see those hooks' doc-comments).
 */
export function useSimulatorStatus(): UseSimulatorStatusResult {
  const [status, setStatus] = useState<SimulatorStatus | null>(null);
  const isMountedRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    try {
      const demoStatus = await api.getDemoStatus();
      if (!isMountedRef.current) {
        return;
      }
      setStatus(demoStatus.status);
    } catch {
      // Best-effort: keep showing the last known status rather than
      // guessing a new one on a transient poll failure. The dashboard's
      // own connection badge (useRaceState) already surfaces connectivity
      // problems; this hook doesn't duplicate that.
    }
  }, []);

  const applyAuthoritativeStatus = useCallback((newStatus: SimulatorStatus) => {
    setStatus(newStatus);
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    const intervalId = setInterval(fetchOnce, POLL_INTERVAL_MS);
    fetchOnce();
    return () => {
      isMountedRef.current = false;
      clearInterval(intervalId);
    };
  }, [fetchOnce]);

  return {status, refreshNow: fetchOnce, applyAuthoritativeStatus};
}
