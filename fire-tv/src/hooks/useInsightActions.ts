import {useCallback, useState} from 'react';
import {api, ApiError, AuthExpiredError} from '../api/client';
import {EvidenceLine} from '../components/AgentAnswerScreen';
import {
  AgentQueryResult,
  Alert,
  ExplanationResult,
  RaceChangesResult,
  RegulationAnswer,
  StrategySimulationResult,
  Watch,
} from '../api/types';

export type InsightScreen = 'simulate' | 'why' | 'watch' | 'sources' | 'whatChanged' | null;

interface AsyncSlice<T> {
  loading: boolean;
  errorMessage: string | null;
  data: T | null;
}

const IDLE_SLICE = {loading: false, errorMessage: null, data: null};

function explanationToEvidence(result: ExplanationResult): EvidenceLine[] {
  // Spec 50: structured evidence only, never parsed from prose — these
  // come straight off ExplanationResult.grounded_facts, the same object
  // the grounding guard checks the narrative against.
  return Object.entries(result.grounded_facts).map(([field, value]) => ({
    label: field.replace(/_/g, ' ').toUpperCase(),
    value,
  }));
}

function agentQueryToEvidence(result: AgentQueryResult): EvidenceLine[] {
  return result.evidence.map(item => ({
    label: item.field.replace(/_/g, ' ').toUpperCase(),
    value: item.value,
  }));
}

function changesToEvidence(result: RaceChangesResult): EvidenceLine[] {
  return result.explained_changes.map(change => ({
    label: `${change.driver_id ?? 'RACE'} ${change.field}`.replace(/_/g, ' ').toUpperCase(),
    value: `${change.previous_value} -> ${change.current_value}`,
  }));
}

function messageFromError(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.body?.message) {
    return err.body.message;
  }
  return err instanceof Error ? err.message : fallback;
}

/**
 * Backs the five D-pad primary actions (spec 11 / the PIT WALL INSIGHT
 * bar, spec 10.1's mockup) — WHY?, SIMULATE, WATCH, SOURCES, WHAT
 * CHANGED? — each opening its corresponding P0 screen (spec 10.2) and
 * fetching that screen's data on demand (not pre-fetched/polled, since
 * these are user-initiated drill-downs, unlike the always-live Strategy
 * Battle panel).
 */
export function useInsightActions(
  raceId: string,
  candidateId: string,
  targetId: string,
  /** Spec v1.1 correction 5: called when `openWatch` (the only mutating
   * action in this hook — creating a strategy watch requires the device
   * access token, same as start/pause/reset) fails because the paired
   * token was rejected (401). See `useDemoControls`'s matching
   * `onAuthExpired` option for the full rationale — wire both to the
   * same `useDeviceAuth().signOut`. */
  onAuthExpired?: () => void,
) {
  const [activeScreen, setActiveScreen] = useState<InsightScreen>(null);

  const [simulate, setSimulate] = useState<
    AsyncSlice<StrategySimulationResult> & {isEngineUnavailable: boolean}
  >({
    ...IDLE_SLICE,
    isEngineUnavailable: false,
  });
  const [why, setWhy] =
    useState<AsyncSlice<{narrative: string; degraded: boolean; evidence: EvidenceLine[]}>>(IDLE_SLICE);
  const [watch, setWatch] = useState<AsyncSlice<Watch>>(IDLE_SLICE);
  const [sources, setSources] = useState<AsyncSlice<RegulationAnswer>>(IDLE_SLICE);
  const [whatChanged, setWhatChanged] =
    useState<AsyncSlice<{narrative: string; evidence: EvidenceLine[]}>>(IDLE_SLICE);

  const closeScreen = useCallback(() => setActiveScreen(null), []);

  const openWhy = useCallback(
    (latestAlert: Alert | null) => {
      setActiveScreen('why');
      setWhy({loading: true, errorMessage: null, data: null});
      (async () => {
        try {
          if (latestAlert) {
            const result = await api.explainAlert(raceId, latestAlert.alert_id);
            setWhy({
              loading: false,
              errorMessage: null,
              data: {
                narrative: result.narrative,
                degraded: result.degraded,
                evidence: explanationToEvidence(result),
              },
            });
          } else {
            const result = await api.queryAgent(
              raceId,
              `Why is the undercut window between ${candidateId} and ${targetId}?`,
            );
            setWhy({
              loading: false,
              errorMessage: null,
              data: {narrative: result.narrative, degraded: false, evidence: agentQueryToEvidence(result)},
            });
          }
        } catch (err) {
          setWhy({
            loading: false,
            errorMessage: messageFromError(err, 'Unable to explain right now.'),
            data: null,
          });
        }
      })();
    },
    [raceId, candidateId, targetId],
  );

  const openSimulate = useCallback(() => {
    setActiveScreen('simulate');
    setSimulate({loading: true, errorMessage: null, data: null, isEngineUnavailable: false});
    (async () => {
      try {
        const result = await api.simulateStrategy(raceId, candidateId);
        setSimulate({loading: false, errorMessage: null, data: result, isEngineUnavailable: false});
      } catch (err) {
        const isEngineUnavailable =
          err instanceof ApiError && err.body?.error === 'strategy_engine_unavailable';
        setSimulate({
          loading: false,
          errorMessage: messageFromError(err, 'Unable to simulate right now.'),
          data: null,
          isEngineUnavailable,
        });
      }
    })();
  }, [raceId, candidateId]);

  const openWatch = useCallback(() => {
    setActiveScreen('watch');
    setWatch({loading: true, errorMessage: null, data: null});
    (async () => {
      try {
        const result = await api.createWatch(raceId, candidateId, targetId);
        setWatch({loading: false, errorMessage: null, data: result});
      } catch (err) {
        setWatch({
          loading: false,
          errorMessage: messageFromError(err, 'Unable to create a watch right now.'),
          data: null,
        });
        if (err instanceof AuthExpiredError) {
          onAuthExpired?.();
        }
      }
    })();
  }, [raceId, candidateId, targetId, onAuthExpired]);

  const openSources = useCallback((query: string = 'What is an undercut?') => {
    setActiveScreen('sources');
    setSources({loading: true, errorMessage: null, data: null});
    (async () => {
      try {
        const result = await api.getRegulation(query);
        setSources({loading: false, errorMessage: null, data: result});
      } catch (err) {
        setSources({
          loading: false,
          errorMessage: messageFromError(err, 'Unable to look up sources right now.'),
          data: null,
        });
      }
    })();
  }, []);

  /** Spec v1.1 pass 4, mandatory correction 4: "AGENT_RESPONSE: display
   * the remotely or Alexa-triggered answer on the Fire TV Agent Answer
   * screen." A backend AGENT_RESPONSE WebSocket event carries exactly the
   * same `AgentQueryResult` shape `POST /v1/agent/query` already returns
   * (see backend/app/api/routes.py's `agent_query` — it broadcasts the
   * SAME `result.model_dump()` it returns), so this reuses the existing
   * `why` screen/AgentAnswerScreen and `agentQueryToEvidence` mapping
   * rather than fetching anything again — the answer already arrived. */
  const showRemoteAgentResponse = useCallback((result: AgentQueryResult) => {
    setActiveScreen('why');
    setWhy({
      loading: false,
      errorMessage: null,
      data: {narrative: result.narrative, degraded: false, evidence: agentQueryToEvidence(result)},
    });
  }, []);

  const openWhatChanged = useCallback(() => {
    setActiveScreen('whatChanged');
    setWhatChanged({loading: true, errorMessage: null, data: null});
    (async () => {
      try {
        const result = await api.getRaceChanges(raceId);
        setWhatChanged({
          loading: false,
          errorMessage: null,
          data: {narrative: result.narrative, evidence: changesToEvidence(result)},
        });
      } catch (err) {
        setWhatChanged({
          loading: false,
          errorMessage: messageFromError(err, 'Unable to check what changed right now.'),
          data: null,
        });
      }
    })();
  }, [raceId]);

  // AWS hackathon-final pass, P0-5: a server-pushed STRATEGY_UPDATE with
  // `kind: "simulate"` now reaches here (see App.tsx's onStrategyUpdate,
  // which was previously feeding EVERY STRATEGY_UPDATE — including this
  // kind — into the Strategy Battle panel's UndercutResult state,
  // corrupting it with a StrategySimulationResult's fields). This keeps
  // the Simulation Comparison screen live-updating from a push, the same
  // way the Strategy Battle panel already did for undercut results,
  // without ever touching `simulate`'s loading/error state (a push isn't
  // a new user-initiated fetch).
  const applyPushedSimulationResult = useCallback((result: StrategySimulationResult) => {
    setSimulate(current => ({...current, loading: false, errorMessage: null, data: result}));
  }, []);

  return {
    activeScreen,
    closeScreen,
    simulate,
    why,
    watch,
    sources,
    whatChanged,
    openWhy,
    openSimulate,
    openWatch,
    openSources,
    openWhatChanged,
    showRemoteAgentResponse,
    applyPushedSimulationResult,
  };
}
