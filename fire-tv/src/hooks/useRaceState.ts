import {useCallback, useEffect, useRef, useState} from 'react';
import {api, ApiError} from '../api/client';
import {RaceState} from '../api/types';

export type ConnectionStatus = 'loading' | 'connected' | 'disconnected' | 'error';

interface UseRaceStateResult {
  raceState: RaceState | null;
  connectionStatus: ConnectionStatus;
  errorMessage: string | null;
  refreshNow: () => Promise<void>;
  /**
   * Explicitly establishes `state.run_id` as the new active run — the ONLY
   * way the active run is ever allowed to change (spec v1.1 "repair 4").
   * Call this from wherever a new run is authoritatively known to have
   * started: the /v1/demo/reset response (its own returned RaceState IS
   * the new run's baseline) and a future RACE_RESET WebSocket event. An
   * ordinary race-state poll response is NEVER allowed to switch the
   * active run on its own, even if its run_id differs from the currently
   * displayed one — see `acceptIncomingState`'s doc-comment for why.
   */
  establishActiveRun: (state: RaceState) => void;
  /**
   * The same acceptance rule `refreshNow`'s poll loop uses internally,
   * exposed so `useRaceSocket.ts`'s WebSocket message handler can feed
   * incoming RACE_STATE events through the identical run_id/state_version
   * guard rather than re-implementing it — spec v1.1 repair 3's "ignore
   * inactive run_id/lower state_version" requirement is enforced in
   * exactly one place for both transports.
   */
  acceptIncomingState: (incoming: RaceState) => void;
}

const POLL_INTERVAL_MS = 2000;

/**
 * Polls GET /v1/races/{raceId}/state every 2 seconds (Gate 0 requirement;
 * WebSocket push is now the primary path — see useRaceSocket.ts — with
 * this poll loop kept running underneath as the mandated fallback, spec
 * v1.1 "repair 3": "When WebSocket is unavailable, poll race state every
 * two seconds.").
 *
 * *** Active-run protection (spec v1.1 "repair 4") ***
 * The ORIGINAL version of this hook accepted any response whose run_id
 * differed from the currently displayed one, on the theory that "a new
 * run always supersedes an old one." That is true for a GENUINE new run,
 * but it is also exactly the bug: it means a slow, delayed response from
 * the PREVIOUS run — one that was already in flight when Reset fired, and
 * only resolves afterward — would satisfy "run_id differs" and silently
 * replace the just-established new run with stale data from the one that
 * was just reset away from. Lexically comparing UUIDs cannot fix this
 * (there is no ordering to a UUID), so this hook does not attempt to.
 *
 * The fix is an explicit active-run mechanism: `activeRunIdRef` is changed
 * ONLY by `establishActiveRun` (called by the reset flow, and by a future
 * RACE_RESET event handler), never by an ordinary poll/WS state message.
 * `acceptIncomingState` — used by both this hook's own poll loop and by
 * useRaceSocket's RACE_STATE message handler, so both paths enforce
 * exactly the same rule — rejects outright any incoming state whose
 * run_id is not the currently active one. For the active run itself, only
 * a strictly greater state_version may replace what's displayed (the
 * pre-existing out-of-order protection, unchanged).
 *
 * Unmount-safety: `isMountedRef` is checked AFTER every `await`,
 * immediately before each `setState` call, not just at the top of the
 * poll function — checking only at entry does not protect the state
 * updates that happen once the awaited promise actually resolves.
 */
export function useRaceState(raceId: string): UseRaceStateResult {
  const [raceState, setRaceState] = useState<RaceState | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('loading');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Ref mirrors state so the polling closure always compares against the
  // latest accepted value without re-subscribing the interval each render.
  const latestRef = useRef<RaceState | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const isMountedRef = useRef(true);

  /** The single acceptance rule for ANY incoming state, from ANY source
   * (poll or WebSocket). Not exported directly — exposed indirectly via
   * `refreshNow`/the poll loop, and reused identically by useRaceSocket. */
  const acceptIncomingState = useCallback((incoming: RaceState) => {
    if (activeRunIdRef.current === null) {
      // First state this session has ever seen — nothing to compare
      // against yet, so it establishes the initial active run.
      activeRunIdRef.current = incoming.run_id;
      latestRef.current = incoming;
      setRaceState(incoming);
      return;
    }
    if (incoming.run_id !== activeRunIdRef.current) {
      // A response/event from a run that is not the active one — always
      // rejected, whether it's an old run's delayed straggler or (in
      // principle) a not-yet-established future run. Only
      // `establishActiveRun` may change which run is active.
      return;
    }
    if (incoming.state_version > (latestRef.current?.state_version ?? -1)) {
      latestRef.current = incoming;
      setRaceState(incoming);
    }
  }, []);

  const establishActiveRun = useCallback((state: RaceState) => {
    activeRunIdRef.current = state.run_id;
    latestRef.current = state;
    setRaceState(state);
  }, []);

  const fetchOnce = useCallback(async () => {
    try {
      const state = await api.getRaceState(raceId);
      if (!isMountedRef.current) {
        return;
      }
      acceptIncomingState(state);
      setConnectionStatus('connected');
      setErrorMessage(null);
    } catch (err) {
      if (!isMountedRef.current) {
        return;
      }
      if (err instanceof ApiError) {
        setConnectionStatus('error');
        setErrorMessage(err.message);
      } else {
        setConnectionStatus('disconnected');
        setErrorMessage('Unable to reach Pit Wall backend.');
      }
    }
  }, [raceId, acceptIncomingState]);

  useEffect(() => {
    isMountedRef.current = true;
    const intervalId = setInterval(fetchOnce, POLL_INTERVAL_MS);
    fetchOnce();

    return () => {
      isMountedRef.current = false;
      clearInterval(intervalId);
    };
  }, [fetchOnce]);

  return {
    raceState,
    connectionStatus,
    errorMessage,
    refreshNow: fetchOnce,
    establishActiveRun,
    acceptIncomingState,
  };
}
