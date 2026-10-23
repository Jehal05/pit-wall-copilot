import React, {useCallback, useState} from 'react';
import {
  BackHandler,
  SafeAreaView,
  ScrollView,
  StatusBar as RNStatusBar,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import {AgentAnswerScreen} from './src/components/AgentAnswerScreen';
import {AlertsPanel} from './src/components/AlertsPanel';
import {EvidenceSourcesScreen} from './src/components/EvidenceSourcesScreen';
import {FocusableButton} from './src/components/FocusableButton';
import {InsightModal} from './src/components/InsightModal';
import {PairingScreen} from './src/components/PairingScreen';
import {PitWallInsightBar} from './src/components/PitWallInsightBar';
import {SimulationComparisonScreen} from './src/components/SimulationComparisonScreen';
import {StandingsTable} from './src/components/StandingsTable';
import {ConnectionBadge, SimulatorBadge, StaleDataBadge} from './src/components/StatusBadges';
import {StrategyBattlePanel} from './src/components/StrategyBattlePanel';
import {StrategyWatchAlertOverlay} from './src/components/StrategyWatchAlertOverlay';
import {TrackPositionList} from './src/components/TrackPositionList';
import {Alert, AgentQueryResult} from './src/api/types';
import {useAlerts} from './src/hooks/useAlerts';
import {useDemoControls} from './src/hooks/useDemoControls';
import {useDeviceAuth} from './src/hooks/useDeviceAuth';
import {useInsightActions} from './src/hooks/useInsightActions';
import {useRaceSocket} from './src/hooks/useRaceSocket';
import {useRaceState} from './src/hooks/useRaceState';
import {useSimulatorStatus} from './src/hooks/useSimulatorStatus';
import {useStrategyBattle} from './src/hooks/useStrategyBattle';

const RACE_ID = 'demo-race-001';
// Spec v1.1's canonical demo narrative (the fixed lap 24-28 scenario) is
// CAR_04 undercutting CAR_03 — the same pair the backend's own tests
// build their canonical alert from (see backend/tests/test_explanation_
// engine.py's _make_real_alert). The Strategy Battle panel and the WHY?/
// SIMULATE/WATCH/SOURCES actions default to this pair for the P0 demo;
// a future iteration could make this driver-selectable via D-pad
// left/right on the standings table, but the spec's fixed 4-car scenario
// doesn't require that for the golden demo to be reachable end to end.
const CANDIDATE_DRIVER_ID = 'CAR_04';
const TARGET_DRIVER_ID = 'CAR_03';

function App(): React.JSX.Element {
  // Spec v1.1 correction 5: gates the entire dashboard behind device
  // pairing. `deviceAuth.status` starts as 'checking' (reading secure
  // storage / verifying a previously-stored token), then settles to
  // either 'unpaired' (show PairingScreen, below) or 'paired' (proceed
  // to the normal dashboard render further down this component).
  const deviceAuth = useDeviceAuth();

  const {raceState, connectionStatus, errorMessage, refreshNow, establishActiveRun, acceptIncomingState} =
    useRaceState(RACE_ID);
  const {
    status: simulatorStatus,
    refreshNow: refreshStatusNow,
    applyAuthoritativeStatus,
  } = useSimulatorStatus();
  const {alerts, alertsErrorMessage, refreshNow: refreshAlertsNow} = useAlerts(RACE_ID);

  // Spec 10.2 #6 "Strategy Watch Alert": the most recently PUSHED
  // transition (UJ-1's "⚠ UNDERCUT WINDOW OPEN" overlay), separate from
  // AlertsPanel's full running history below.
  const [latestWatchAlert, setLatestWatchAlert] = useState<Alert | null>(null);

  // Spec 10.1's "STRATEGY BATTLE" panel + spec 11's D-pad primary actions.
  // Declared before useDemoControls/useRaceSocket below since both of
  // those reset paths need to clear/refresh this run-specific state
  // (spec v1.1 pass 4, correction 4).
  const strategyBattle = useStrategyBattle(RACE_ID, CANDIDATE_DRIVER_ID, TARGET_DRIVER_ID, raceState?.lap);
  const insightActions = useInsightActions(RACE_ID, CANDIDATE_DRIVER_ID, TARGET_DRIVER_ID, () =>
    deviceAuth.signOut(),
  );

  const {isBusy, lastActionError, start, pause, reset} = useDemoControls({
    onChanged: () => {
      refreshNow();
      refreshStatusNow();
    },
    onReset: newState => {
      establishActiveRun(newState);
      refreshStatusNow();
      // Consistent with the WebSocket RACE_RESET handler below (spec
      // v1.1 pass 4, correction 4) — a reset initiated from this Fire
      // TV's own RESET button is just as much a new run as one pushed
      // over the socket, so it clears the same run-specific overlays.
      setLatestWatchAlert(null);
      insightActions.closeScreen();
      strategyBattle.refreshNow();
    },
    // A 401 on an already-paired device's start/pause/reset call means
    // the backend no longer accepts this device's token — return to the
    // pairing screen with a clear explanation rather than leaving a
    // dashboard whose every mutating control silently keeps failing.
    onAuthExpired: () => {
      deviceAuth.signOut();
    },
  });

  // Spec v1.1 repair 3 / pass 4 mandatory correction 4: WebSocket push is
  // the primary delivery path, with useRaceState's/useAlerts's own REST
  // polling (above) continuing to run unconditionally underneath as the
  // mandated fallback — see useRaceSocket.ts's module doc-comment for why
  // both are left running rather than one disabling the other. EVERY
  // event type the backend can emit is now connected to real client
  // state/UI here, not left as an unused optional callback:
  //   RACE_STATE      -> acceptIncomingState (ordinary in-run update)
  //   RACE_RESET      -> establishActiveRun + clear run-specific overlays
  //   WATCH_ALERT     -> refresh the alert list + proactive overlay
  //                      (only when data is fresh — see isStale check)
  //   STRATEGY_UPDATE -> push the result straight into the Strategy
  //                      Battle panel (no extra round-trip)
  //   AGENT_RESPONSE  -> show the pushed answer on the Agent Answer
  //                      screen (only when data is fresh)
  //   DEMO_STATUS     -> push the simulator status directly (this
  //                      backend does not emit this event today, but the
  //                      wiring is real and tested for when it does)
  //   DATA_STALE      -> `isStale` (already surfaced via StaleDataBadge)
  const {isStale} = useRaceSocket(RACE_ID, {
    onRaceState: acceptIncomingState,
    onReset: newState => {
      establishActiveRun(newState);
      refreshStatusNow();
      // A new run invalidates every run-specific overlay/answer from the
      // PREVIOUS run — spec v1.1 pass 4, correction 4: "Clear run-specific
      // overlays, answers and strategy results after reset."
      setLatestWatchAlert(null);
      insightActions.closeScreen();
      strategyBattle.refreshNow();
    },
    onWatchAlert: (incomingAlert, isStaleAtDispatch) => {
      refreshAlertsNow();
      // Spec v1.1 pass 4, correction 4: "show the proactive overlay only
      // when data is fresh" — a WATCH_ALERT that arrives while the client
      // already knows its own data is stale must not pop an overlay the
      // person cannot trust is still current. AWS hackathon-final pass,
      // P0-6: `isStaleAtDispatch` is read synchronously inside
      // useRaceSocket at the moment this event was dispatched (not this
      // component's own possibly-one-render-behind `isStale`), so a
      // DATA_STALE:true immediately followed by a WATCH_ALERT is
      // suppressed correctly even if React has not yet re-rendered
      // between the two.
      if (!isStaleAtDispatch) {
        setLatestWatchAlert(incomingAlert);
      }
    },
    onStrategyUpdate: (payload, isStaleAtDispatch) => {
      // AWS hackathon-final pass, P0-5: `payload` is now a validated
      // discriminated union (useRaceSocket's parseStrategyUpdatePayload)
      // — route each kind to the view it actually belongs to, instead of
      // the previous blanket `payload.result as UndercutResult`, which
      // silently fed a `kind: "simulate"` push's StrategySimulationResult
      // into the Strategy Battle panel's UndercutResult state.
      // P0-6: "suppress strategy/agent recommendations while stale" — a
      // pushed result computed against data this client already knows is
      // stale must not silently become the new authoritative display.
      if (isStaleAtDispatch) {
        return;
      }
      if (payload.kind === 'undercut') {
        strategyBattle.applyAuthoritativeResult(payload.result);
      } else if (payload.kind === 'simulate') {
        insightActions.applyPushedSimulationResult(payload.result);
      }
    },
    onAgentResponse: (payload, isStaleAtDispatch) => {
      // Spec v1.1 pass 4, correction 4: "Prevent stale events from
      // opening a modal or overwriting current state" — an Alexa/remote
      // answer computed against data this client already knows is stale
      // is not surfaced as if it were current. See onWatchAlert's
      // doc-comment above on why `isStaleAtDispatch` (not this
      // component's own `isStale`) is the correct value to check.
      if (!isStaleAtDispatch) {
        insightActions.showRemoteAgentResponse(payload as AgentQueryResult);
      }
    },
    onDemoStatus: payload => {
      const pushedStatus = payload?.status;
      if (
        pushedStatus === 'STOPPED' ||
        pushedStatus === 'RUNNING' ||
        pushedStatus === 'PAUSED' ||
        pushedStatus === 'COMPLETED'
      ) {
        applyAuthoritativeStatus(pushedStatus);
      }
    },
    onReconnect: () => {
      refreshNow();
      refreshStatusNow();
      refreshAlertsNow();
    },
    // AWS hackathon-final pass, P2 (Fire TV polish): a WebSocket 4401
    // means the backend rejected this device's own token (see ADR 0002) —
    // the exact same "expired/invalid auth must produce a clear Fire TV
    // error and return to pairing/auth" contract `useDeviceAuth.ts`
    // already applies to every REST call's 401. Without this, the
    // dashboard would keep rendering with a permanently-dead live-data
    // connection and no indication why.
    onAuthFailure: () => {
      deviceAuth.demoteToUnpairedDueToRejectedCredential(
        'This device’s pairing is no longer valid. Please pair this Fire TV again.',
      );
    },
  });

  const candidateDriver = raceState?.drivers.find(d => d.driver_id === CANDIDATE_DRIVER_ID);
  const targetDriver = raceState?.drivers.find(d => d.driver_id === TARGET_DRIVER_ID);

  const insightText = strategyBattle.isEngineUnavailable
    ? 'STRATEGY MODEL UNAVAILABLE'
    : strategyBattle.result
    ? strategyBattle.result.window === 'OPEN'
      ? 'The undercut window has opened.'
      : 'The undercut window is currently closed.'
    : 'Calculating the current strategy battle…';

  const handleBackPress = useCallback(() => {
    // Gate 0 has a single screen; Back exits the app rather than navigating
    // to an undefined prior screen, per Fire TV Back-button expectations.
    return false;
  }, []);

  React.useEffect(() => {
    const subscription = BackHandler.addEventListener('hardwareBackPress', handleBackPress);
    return () => subscription.remove();
  }, [handleBackPress]);

  // Spec v1.1 correction 5: while this device has no verified token, show
  // ONLY the pairing screen — never the dashboard with mutating controls
  // that would just throw AuthNotConfiguredError the moment they're
  // pressed. 'checking' (still reading secure storage / verifying a
  // stored token at startup) shows a minimal loading state rather than a
  // flash of the pairing form for the common case where a token WAS
  // already stored and verifies successfully.
  if (deviceAuth.status === 'checking') {
    return (
      <SafeAreaView style={styles.safeArea}>
        <RNStatusBar barStyle="light-content" backgroundColor="#0b0d11" />
        <View style={styles.checkingContainer} testID="device-auth-checking">
          <Text style={styles.loadingText}>Checking device pairing…</Text>
        </View>
      </SafeAreaView>
    );
  }
  if (deviceAuth.status === 'unpaired' || deviceAuth.status === 'pairing') {
    return (
      <SafeAreaView style={styles.safeArea}>
        <RNStatusBar barStyle="light-content" backgroundColor="#0b0d11" />
        <PairingScreen
          isPairing={deviceAuth.status === 'pairing'}
          errorMessage={deviceAuth.errorMessage}
          onPair={code => deviceAuth.pair(code)}
        />
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safeArea}>
      <RNStatusBar barStyle="light-content" backgroundColor="#0b0d11" />
      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.headerRow}>
          <View>
            <Text style={styles.title}>PIT WALL COPILOT</Text>
            <Text style={styles.subtitle}>SYNTHETIC DEMO — ALL DATA FICTIONAL</Text>
          </View>
          <View style={styles.badgeRow}>
            <ConnectionBadge status={connectionStatus} />
            {isStale && <StaleDataBadge />}
          </View>
        </View>

        <View style={styles.lapRow}>
          <Text style={styles.lapText}>
            LAP {raceState?.lap ?? '--'} / {raceState?.total_laps ?? '--'}
          </Text>
          {simulatorStatus && <SimulatorBadge status={simulatorStatus} />}
        </View>

        {connectionStatus === 'disconnected' && (
          <Text style={styles.errorBanner} testID="disconnected-banner">
            DISCONNECTED FROM PIT WALL BACKEND — retrying…
          </Text>
        )}
        {connectionStatus === 'error' && errorMessage && (
          <Text style={styles.errorBanner} testID="error-banner">
            {errorMessage}
          </Text>
        )}

        {!raceState ? (
          <Text style={styles.loadingText} testID="loading-text">
            Loading race state…
          </Text>
        ) : (
          <>
            <View style={styles.dashboardRow}>
              <View style={styles.trackMapColumn}>
                <TrackPositionList drivers={raceState.drivers} />
              </View>
              <View style={styles.battleColumn}>
                <StrategyBattlePanel
                  candidate={candidateDriver}
                  target={targetDriver}
                  result={strategyBattle.result}
                  isEngineUnavailable={strategyBattle.isEngineUnavailable}
                  errorMessage={strategyBattle.errorMessage}
                />
              </View>
            </View>
            <StandingsTable drivers={raceState.drivers} />
          </>
        )}

        {lastActionError && (
          <Text style={styles.errorBanner} testID="action-error-banner">
            {lastActionError}
          </Text>
        )}

        <View style={styles.controlsRow}>
          <FocusableButton
            label="START"
            onPress={start}
            disabled={isBusy}
            hasTVPreferredFocus
            testID="start-button"
          />
          <FocusableButton label="PAUSE" onPress={pause} disabled={isBusy} testID="pause-button" />
          <FocusableButton label="RESET" onPress={reset} disabled={isBusy} testID="reset-button" />
          {/* Spec v1.1 correction 5: explicit sign-out/unpair/clear-credential
              action, reachable by D-pad exactly like the other three controls
              above — clears the Keystore-backed stored token and returns to
              the pairing screen. */}
          <FocusableButton label="UNPAIR" onPress={() => deviceAuth.signOut()} testID="unpair-button" />
        </View>

        <PitWallInsightBar
          insightText={insightText}
          onWhy={() => insightActions.openWhy(alerts[0] ?? null)}
          onSimulate={insightActions.openSimulate}
          onWatch={insightActions.openWatch}
          onSources={() => insightActions.openSources()}
          onWhatChanged={insightActions.openWhatChanged}
        />

        <AlertsPanel alerts={alerts} errorMessage={alertsErrorMessage} />
      </ScrollView>

      <StrategyWatchAlertOverlay alert={latestWatchAlert} onDismiss={() => setLatestWatchAlert(null)} />

      <InsightModal
        visible={insightActions.activeScreen === 'why'}
        title="WHY?"
        onClose={insightActions.closeScreen}
        testID="screen-why">
        <AgentAnswerScreen
          loading={insightActions.why.loading}
          errorMessage={insightActions.why.errorMessage}
          narrative={insightActions.why.data?.narrative ?? null}
          degraded={insightActions.why.data?.degraded}
          evidence={insightActions.why.data?.evidence ?? []}
        />
      </InsightModal>

      <InsightModal
        visible={insightActions.activeScreen === 'simulate'}
        title="SIMULATION COMPARISON"
        onClose={insightActions.closeScreen}
        testID="screen-simulate">
        <SimulationComparisonScreen
          loading={insightActions.simulate.loading}
          errorMessage={insightActions.simulate.errorMessage}
          isEngineUnavailable={insightActions.simulate.isEngineUnavailable}
          result={insightActions.simulate.data}
        />
      </InsightModal>

      <InsightModal
        visible={insightActions.activeScreen === 'watch'}
        title="WATCH"
        onClose={insightActions.closeScreen}
        testID="screen-watch">
        {insightActions.watch.loading ? (
          <Text style={styles.modalBodyText} testID="watch-loading">
            Creating watch…
          </Text>
        ) : insightActions.watch.errorMessage ? (
          <Text style={styles.errorBanner} testID="watch-error">
            {insightActions.watch.errorMessage}
          </Text>
        ) : insightActions.watch.data ? (
          <Text style={styles.modalBodyText} testID="watch-confirmation">
            Watching {insightActions.watch.data.candidate} → {insightActions.watch.data.target}. Current
            status: {insightActions.watch.data.status}.
          </Text>
        ) : null}
      </InsightModal>

      <InsightModal
        visible={insightActions.activeScreen === 'sources'}
        title="EVIDENCE / SOURCES"
        onClose={insightActions.closeScreen}
        testID="screen-sources">
        <EvidenceSourcesScreen
          loading={insightActions.sources.loading}
          errorMessage={insightActions.sources.errorMessage}
          answer={insightActions.sources.data}
        />
      </InsightModal>

      <InsightModal
        visible={insightActions.activeScreen === 'whatChanged'}
        title="WHAT CHANGED?"
        onClose={insightActions.closeScreen}
        testID="screen-what-changed">
        <AgentAnswerScreen
          loading={insightActions.whatChanged.loading}
          errorMessage={insightActions.whatChanged.errorMessage}
          narrative={insightActions.whatChanged.data?.narrative ?? null}
          evidence={insightActions.whatChanged.data?.evidence ?? []}
        />
      </InsightModal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: '#0b0d11',
  },
  content: {
    padding: 32,
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 24,
  },
  title: {
    color: '#f2f4f6',
    fontSize: 32,
    fontWeight: '800',
    letterSpacing: 2,
  },
  subtitle: {
    color: '#7d8492',
    fontSize: 14,
    marginTop: 4,
    letterSpacing: 1,
  },
  badgeRow: {
    flexDirection: 'row',
  },
  lapRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 20,
  },
  lapText: {
    color: '#e6e8eb',
    fontSize: 24,
    fontWeight: '700',
    marginRight: 12,
  },
  loadingText: {
    color: '#9aa2b1',
    fontSize: 20,
    marginVertical: 24,
  },
  checkingContainer: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  errorBanner: {
    color: '#e05a5a',
    fontSize: 16,
    marginBottom: 16,
    fontWeight: '600',
  },
  controlsRow: {
    flexDirection: 'row',
    marginTop: 32,
  },
  dashboardRow: {
    flexDirection: 'row',
    marginBottom: 20,
  },
  trackMapColumn: {
    flex: 1,
    marginRight: 16,
  },
  battleColumn: {
    flex: 1,
  },
  modalBodyText: {
    color: '#e6e8eb',
    fontSize: 20,
    lineHeight: 28,
  },
});

export default App;
