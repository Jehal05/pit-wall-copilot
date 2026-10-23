import {useEffect, useRef, useState} from 'react';
import {api, ApiError} from '../api/client';
import {UndercutResult} from '../api/types';

export interface UseStrategyBattleResult {
  result: UndercutResult | null;
  /** Spec v1.1 repair 14 / Section 29 "Strategy engine failure": true iff
   * the last calculate_undercut call failed with the backend's
   * strategy_engine_unavailable error — the caller SHALL then display the
   * literal "STRATEGY MODEL UNAVAILABLE" text (see errorMessage) rather
   * than substituting an LLM guess or a generic error. */
  isEngineUnavailable: boolean;
  errorMessage: string | null;
  refreshNow: () => void;
  /** Spec v1.1 pass 4, mandatory correction 4: "STRATEGY_UPDATE: update
   * or refresh the Strategy Battle/Simulation UI using the pushed
   * result." A STRATEGY_UPDATE WebSocket event already carries a fresh
   * `UndercutResult` computed server-side (see
   * `useRaceSocket.ts`'s `StrategyUpdatePayload`) — applying it directly
   * updates the panel immediately, without an extra
   * `calculate_undercut` round-trip for data the backend already pushed. */
  applyAuthoritativeResult: (result: UndercutResult) => void;
}

/**
 * Drives the Strategy Battle panel (spec 10.1's main-screen mockup: "CAR 3
 * M 22 laps / CAR 4 M 18 laps / GAP 2.0 sec / UNDERCUT OPEN / PROJECTED
 * GAIN +0.8 sec") by re-running calculate_undercut whenever the race
 * advances a lap — not on a fixed timer, since the result only actually
 * changes when the underlying race state does, and re-running it every lap
 * keeps the panel's "live" feel without hammering the backend on every
 * WebSocket tick within the same lap.
 */
export function useStrategyBattle(
  raceId: string,
  candidateId: string,
  targetId: string,
  lap: number | undefined,
): UseStrategyBattleResult {
  const [result, setResult] = useState<UndercutResult | null>(null);
  const [isEngineUnavailable, setIsEngineUnavailable] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (lap === undefined) {
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const fetched = await api.calculateUndercut(raceId, candidateId, targetId);
        if (cancelled || !isMountedRef.current) {
          return;
        }
        setResult(fetched);
        setIsEngineUnavailable(false);
        setErrorMessage(null);
      } catch (err) {
        if (cancelled || !isMountedRef.current) {
          return;
        }
        if (err instanceof ApiError && err.body?.error === 'strategy_engine_unavailable') {
          setIsEngineUnavailable(true);
          setErrorMessage(err.body.message);
        } else {
          setErrorMessage(err instanceof Error ? err.message : 'Unable to calculate strategy.');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [raceId, candidateId, targetId, lap, refreshToken]);

  return {
    result,
    isEngineUnavailable,
    errorMessage,
    refreshNow: () => setRefreshToken(t => t + 1),
    applyAuthoritativeResult: (pushed: UndercutResult) => {
      setResult(pushed);
      setIsEngineUnavailable(false);
      setErrorMessage(null);
    },
  };
}
