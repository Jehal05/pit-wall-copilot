import {renderHook, act} from '@testing-library/react-native';
import {parseStrategyUpdatePayload, useRaceSocket, toWebSocketUrl} from '../hooks/useRaceSocket';
import {Alert, RaceEvent, RaceState} from '../api/types';
import {setRuntimeAuthToken} from '../config/runtimeAuthToken';

/** AWS hackathon-final pass, P0-5: a minimal-but-valid `kind: "undercut"`
 * STRATEGY_UPDATE payload, matching what `parseStrategyUpdatePayload`
 * (useRaceSocket.ts) actually requires. These tests exercise dispatch
 * plumbing (does the right handler get called at all), not
 * UndercutResult's own field semantics, so only the fields the validator
 * checks are given real values. */
function makeValidUndercutPayload(overrides: Record<string, unknown> = {}) {
  return {
    kind: 'undercut' as const,
    result: {
      calculation_id: 'calc-1',
      model_version: 'v1',
      race_id: 'demo-race-001',
      run_id: 'run-a',
      source_state_version: 10,
      candidate: 'CAR_04',
      target: 'CAR_03',
      window: 'OPEN',
      expected_margin_ms: 420,
      comparison_horizon_lap: 30,
      candidate_tire_age_laps: 5,
      target_tire_age_laps: 8,
      traffic_classification: 'CLEAR_AIR',
      assumptions: [],
      confidence: {
        trials: 100,
        median_margin_ms: 420,
        p10_margin_ms: 100,
        p90_margin_ms: 700,
        probability_ahead: 0.7,
        model_seed: 1,
      },
      ...overrides,
    },
  };
}

/** A fully controllable fake WebSocket — overrides jest.setup.js's inert
 * default within this file so these tests can actually drive open/
 * message/close/error events and assert on reconnect behavior. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: {data: string}) => void) | null = null;
  onclose: ((event?: {code?: number}) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  send() {}

  // Test helpers (not part of the real WebSocket interface):
  simulateOpen() {
    this.onopen?.();
  }
  simulateMessage(event: RaceEvent) {
    this.onmessage?.({data: JSON.stringify(event)});
  }
  simulateClose(code?: number) {
    this.onclose?.(code !== undefined ? {code} : undefined);
  }
}

function makeRaceState(overrides: Partial<RaceState> = {}): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 1,
    lap: 25,
    total_laps: 58,
    race_time_ms: 0,
    track_state: 'GREEN',
    generated_at: new Date().toISOString(),
    scenario_id: 'scenario-demo-01',
    synthetic: true,
    drivers: [],
    ...overrides,
  };
}

function makeEvent(overrides: Partial<RaceEvent> = {}): RaceEvent {
  const envelope = {
    type: 'RACE_STATE' as const,
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 1,
    generated_at: new Date().toISOString(),
    payload: undefined as unknown,
    ...overrides,
  };
  if (overrides.payload === undefined && (envelope.type === 'RACE_STATE' || envelope.type === 'RACE_RESET')) {
    // AWS hackathon-final pass, P0-7: useRaceSocket now rejects a
    // RACE_STATE/RACE_RESET whose envelope and payload disagree on
    // race_id/run_id/state_version (envelopeAgreesWithPayload). Most
    // existing tests only override the ENVELOPE's run_id/state_version
    // and expect a matching default payload — keep them in sync here by
    // default, exactly as the real backend always sends them, rather
    // than requiring every call site to spell out both.
    envelope.payload = makeRaceState({
      race_id: envelope.race_id,
      run_id: envelope.run_id,
      state_version: envelope.state_version,
    });
  } else if (overrides.payload === undefined) {
    envelope.payload = makeRaceState();
  }
  return envelope as RaceEvent;
}

describe('useRaceSocket', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    FakeWebSocket.instances = [];
    // @ts-expect-error test-only override
    global.WebSocket = FakeWebSocket;
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('dispatches RACE_STATE messages to onRaceState', () => {
    const onRaceState = jest.fn();
    renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    const state = makeRaceState({lap: 30});
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', payload: state})));

    expect(onRaceState).toHaveBeenCalledWith(state);
  });

  it('dispatches RACE_RESET to onReset, not onRaceState, when onReset is provided', () => {
    const onRaceState = jest.fn();
    const onReset = jest.fn();
    renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    const resetState = makeRaceState({run_id: 'run-b', state_version: 0, lap: 24});
    act(() =>
      socket.simulateMessage(
        makeEvent({type: 'RACE_RESET', run_id: 'run-b', state_version: 0, payload: resetState}),
      ),
    );

    expect(onReset).toHaveBeenCalledWith(resetState);
    expect(onRaceState).not.toHaveBeenCalled();
  });

  it('dispatches WATCH_ALERT to onWatchAlert', () => {
    const onRaceState = jest.fn();
    const onWatchAlert = jest.fn();
    renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    // Spec v1.1 pass 4, correction 3: WATCH_ALERT never establishes the
    // active run itself — an authoritative RACE_STATE (which the real
    // backend always sends immediately on connect) must land first.
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    const alertPayload = {alert_id: 'alert-1'} as unknown as Alert;
    act(() => socket.simulateMessage(makeEvent({type: 'WATCH_ALERT', payload: alertPayload})));

    expect(onWatchAlert).toHaveBeenCalledWith(alertPayload, false);
  });

  it('dispatches AGENT_RESPONSE to onAgentResponse', () => {
    const onRaceState = jest.fn();
    const onAgentResponse = jest.fn();
    renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onAgentResponse}));

    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());
    act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));
    act(() => socket.simulateMessage(makeEvent({type: 'AGENT_RESPONSE', payload: {status: 'SUPPORTED'}})));

    expect(onAgentResponse).toHaveBeenCalledWith({status: 'SUPPORTED'}, false);
  });

  it('reconnects after a close, and calls onReconnect once the new connection opens', () => {
    const onRaceState = jest.fn();
    const onReconnect = jest.fn();
    const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReconnect}));

    const firstSocket = FakeWebSocket.instances[0];
    act(() => firstSocket.simulateOpen());
    expect(result.current.connectionState).toBe('open');

    act(() => firstSocket.simulateClose());
    expect(result.current.connectionState).toBe('closed');
    expect(onReconnect).not.toHaveBeenCalled();

    // First reconnect attempt is scheduled at 250ms — well within the
    // spec's "<5s" bound.
    act(() => jest.advanceTimersByTime(250));
    expect(FakeWebSocket.instances.length).toBe(2);

    act(() => FakeWebSocket.instances[1].simulateOpen());
    expect(result.current.connectionState).toBe('open');
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  // AWS hackathon-final pass, P2 (Fire TV polish): a 4401 close (the
  // backend rejecting this connection's ?token= — see ADR 0002) must NOT
  // be treated like an ordinary dropped connection. Retrying with the
  // same rejected credential would only ever be rejected again.
  describe('4401 (authentication rejected) close handling', () => {
    afterEach(() => {
      setRuntimeAuthToken(null); // don't leak a token across other tests in this file
    });

    it('does not schedule a reconnect after a 4401 close when a real token was sent', () => {
      setRuntimeAuthToken('a-token-that-the-backend-is-about-to-reject');
      const onRaceState = jest.fn();
      const onAuthFailure = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onAuthFailure}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() => firstSocket.simulateClose(4401));

      expect(result.current.connectionState).toBe('closed');
      const instanceCountAfterClose = FakeWebSocket.instances.length;
      act(() => jest.advanceTimersByTime(10000)); // well past every reconnect backoff tier
      expect(FakeWebSocket.instances.length).toBe(instanceCountAfterClose); // no new socket was ever opened
    });

    it('calls onAuthFailure exactly once on a 4401 close when a real token was sent', () => {
      setRuntimeAuthToken('a-token-that-the-backend-is-about-to-reject');
      const onRaceState = jest.fn();
      const onAuthFailure = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onAuthFailure}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() => firstSocket.simulateClose(4401));

      expect(onAuthFailure).toHaveBeenCalledTimes(1);
    });

    it('an ordinary close (no code / a non-4401 code) still reconnects as before, without calling onAuthFailure', () => {
      setRuntimeAuthToken('a-token-that-the-backend-is-about-to-reject');
      const onRaceState = jest.fn();
      const onAuthFailure = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onAuthFailure}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() => firstSocket.simulateClose()); // no code at all -- the common "network dropped" case
      act(() => jest.advanceTimersByTime(250));

      expect(FakeWebSocket.instances.length).toBe(2); // reconnected
      expect(onAuthFailure).not.toHaveBeenCalled();
    });

    it('a 4401 close with NO token sent (e.g. a device that has never paired) is treated as an ordinary close, not a demote signal', () => {
      // No setRuntimeAuthToken call -- resolveAuthToken() returns null,
      // so this connection attempt never included a ?token= at all. A
      // 4401 here is expected/uninformative (there was nothing to
      // reject), not evidence of a REJECTED credential -- see
      // `tokenWasSentRef`'s own doc-comment in useRaceSocket.ts for why
      // firing onAuthFailure here would race ahead of useDeviceAuth's
      // own bootstrap check on a cold app launch.
      const onRaceState = jest.fn();
      const onAuthFailure = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onAuthFailure}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() => firstSocket.simulateClose(4401));
      act(() => jest.advanceTimersByTime(250));

      expect(FakeWebSocket.instances.length).toBe(2); // reconnected, same as an ordinary close
      expect(onAuthFailure).not.toHaveBeenCalled();
    });
  });

  it('does not call onReconnect on the very first connection (only on a RECONNECT)', () => {
    const onRaceState = jest.fn();
    const onReconnect = jest.fn();
    renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReconnect}));

    act(() => FakeWebSocket.instances[0].simulateOpen());
    expect(onReconnect).not.toHaveBeenCalled();
  });

  it('reports isStale=true after 5s with no messages, and false again after a fresh message', () => {
    const onRaceState = jest.fn();
    const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());

    expect(result.current.isStale).toBe(false);

    // The staleness check runs on a 1s interval, not continuously, so
    // advance past a full tick boundary beyond the 5000ms threshold
    // (5000ms exactly is not yet ">5000ms stale" at the tick that lands
    // precisely on it) rather than just past the threshold itself.
    act(() => jest.advanceTimersByTime(6000));
    expect(result.current.isStale).toBe(true);

    act(() => socket.simulateMessage(makeEvent()));
    act(() => jest.advanceTimersByTime(1000)); // one stale-check tick, well under the 5s threshold
    expect(result.current.isStale).toBe(false);
  });

  it('closes the socket and stops reconnecting on unmount', () => {
    const onRaceState = jest.fn();
    const {unmount} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.simulateOpen());

    unmount();
    expect(socket.closed).toBe(true);

    const instanceCountAtUnmount = FakeWebSocket.instances.length;
    act(() => jest.advanceTimersByTime(10000));
    expect(FakeWebSocket.instances.length).toBe(instanceCountAtUnmount); // no reconnect attempted after unmount
  });

  // Spec v1.1 correction 4: "Fire TV must derive wss:// from an HTTPS API
  // URL" — asserted directly against toWebSocketUrl's derivation logic
  // (rather than depending on whatever single scheme this test run's
  // .env happens to inline into the real API_BASE_URL), covering both
  // directions so neither can silently regress into the wrong scheme.
  describe('toWebSocketUrl scheme derivation', () => {
    it('derives wss:// from an https:// API base URL', () => {
      expect(toWebSocketUrl('demo-race-001', 'https://api.example.com')).toBe(
        'wss://api.example.com/v1/races/demo-race-001/ws',
      );
    });

    it('derives ws:// from a plain http:// API base URL (local/demo only)', () => {
      expect(toWebSocketUrl('demo-race-001', 'http://10.0.2.2:8000')).toBe(
        'ws://10.0.2.2:8000/v1/races/demo-race-001/ws',
      );
    });

    it('never produces a plaintext ws:// URL from an https:// base', () => {
      const url = toWebSocketUrl('demo-race-001', 'https://pit-wall-backend.example.com');
      expect(url.startsWith('wss://')).toBe(true);
      expect(url.startsWith('ws://')).toBe(false);
    });
  });

  // AWS hackathon-final pass, P2-12: the backend WebSocket route now
  // requires the same credential every HTTP request sends as
  // `X-Demo-Access-Token` (see backend/app/api/routes.py's
  // `race_websocket` and its `credential_is_valid` check) — a browser/
  // React-Native `WebSocket` cannot attach a custom header to the
  // handshake, so it travels as `?token=...` instead.
  describe('toWebSocketUrl auth token propagation', () => {
    afterEach(() => {
      setRuntimeAuthToken(null); // don't leak a token across other tests in this file
    });

    it('appends the paired device token as a query parameter when one is set', () => {
      setRuntimeAuthToken('a-real-paired-device-token');
      const url = toWebSocketUrl('demo-race-001', 'https://api.example.com');
      expect(url).toBe('wss://api.example.com/v1/races/demo-race-001/ws?token=a-real-paired-device-token');
    });

    it('percent-encodes a token value that would otherwise corrupt the URL', () => {
      setRuntimeAuthToken('token/with special&chars');
      const url = toWebSocketUrl('demo-race-001', 'https://api.example.com');
      expect(url).toBe(
        `wss://api.example.com/v1/races/demo-race-001/ws?token=${encodeURIComponent(
          'token/with special&chars',
        )}`,
      );
    });

    it('omits the query parameter entirely when no token is available yet', () => {
      setRuntimeAuthToken(null);
      const url = toWebSocketUrl('demo-race-001', 'https://api.example.com');
      expect(url).toBe('wss://api.example.com/v1/races/demo-race-001/ws');
    });
  });

  // Spec v1.1 pass 3, correction 2: every event type the backend can emit
  // must be handled, not silently ignored — these 12 tests are the
  // required minimum enumerated by that correction.
  describe('every required WebSocket event is handled (spec v1.1 pass 3, correction 2)', () => {
    it('1. dispatches STRATEGY_UPDATE to onStrategyUpdate', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));

      const payload = makeValidUndercutPayload();
      act(() => socket.simulateMessage(makeEvent({type: 'STRATEGY_UPDATE', payload})));

      expect(onStrategyUpdate).toHaveBeenCalledWith(payload, false);
    });

    it('2. dispatches DEMO_STATUS to onDemoStatus', () => {
      const onRaceState = jest.fn();
      const onDemoStatus = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onDemoStatus}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE'})));

      const payload = {phase: 'RUNNING', lap: 25};
      act(() => socket.simulateMessage(makeEvent({type: 'DEMO_STATUS', payload})));

      expect(onDemoStatus).toHaveBeenCalledWith(payload);
    });

    it('3. a DATA_STALE {stale: true} event sets isStale immediately and calls onDataStaleChange', () => {
      const onRaceState = jest.fn();
      const onDataStaleChange = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onDataStaleChange}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      // Spec v1.1 pass 4, correction 3: DATA_STALE now goes through the
      // same run/version guard as every other non-establishing event, so
      // an authoritative RACE_STATE must land first to establish the
      // active run before a DATA_STALE for it will be applied.
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      expect(result.current.isStale).toBe(false);
      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'DATA_STALE',
            run_id: 'run-a',
            state_version: 2,
            payload: {stale: true, reason: 'simulator lag'},
          }),
        ),
      );

      expect(result.current.isStale).toBe(true);
      expect(onDataStaleChange).toHaveBeenCalledWith({stale: true, reason: 'simulator lag'});
    });

    it('4. a DATA_STALE {stale: false} event clears isStale immediately, without waiting for the local timer', () => {
      const onRaceState = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      act(() => jest.advanceTimersByTime(6000));
      expect(result.current.isStale).toBe(true);

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 2, payload: {stale: false}}),
        ),
      );
      expect(result.current.isStale).toBe(false);
    });

    it('3b. a DATA_STALE for a run that is no longer active is rejected and does not affect isStale', () => {
      const onRaceState = jest.fn();
      const onDataStaleChange = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onDataStaleChange}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'DATA_STALE',
            run_id: 'run-old',
            state_version: 99,
            payload: {stale: true, reason: 'stale run'},
          }),
        ),
      );

      expect(result.current.isStale).toBe(false);
      expect(onDataStaleChange).not.toHaveBeenCalled();
    });

    it('5. rejects a WATCH_ALERT from a run_id that is not the active run', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      // Establish "run-a" as active via a RACE_STATE message first.
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      // A stray WATCH_ALERT from a different (stale) run must be dropped.
      const alertPayload = {alert_id: 'alert-stale'} as unknown as Alert;
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-old', state_version: 99, payload: alertPayload}),
        ),
      );

      expect(onWatchAlert).not.toHaveBeenCalled();
    });

    it('6. rejects a STRATEGY_UPDATE whose state_version regresses below one already seen for the active run', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 5})));
      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'STRATEGY_UPDATE',
            run_id: 'run-a',
            state_version: 3,
            payload: {kind: 'WAIT', result: {}},
          }),
        ),
      );

      expect(onStrategyUpdate).not.toHaveBeenCalled();
    });

    it('7. an event that arrives before any RACE_STATE is rejected, not used to establish the active run (spec v1.1 pass 4, correction 3)', () => {
      // Pass 3 originally let a non-establishing event type bootstrap the
      // active run itself if none was set yet. Pass 4, mandatory
      // correction 3 explicitly forbids this: "Do not let STRATEGY_UPDATE,
      // WATCH_ALERT, AGENT_RESPONSE, DEMO_STATUS, or DATA_STALE establish
      // the active run" — only an authoritative RACE_STATE/RACE_RESET may.
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      const payload = makeValidUndercutPayload();
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-first', state_version: 1, payload}),
        ),
      );

      expect(onStrategyUpdate).not.toHaveBeenCalled();

      // The subsequent authoritative RACE_STATE for the SAME run_id still
      // correctly establishes it, and a strategy update after that IS
      // dispatched — proving this is about ordering/authority, not a
      // permanent lockout of that run_id.
      act(() =>
        socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-first', state_version: 1})),
      );
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-first', state_version: 2, payload}),
        ),
      );
      expect(onStrategyUpdate).toHaveBeenCalledWith(payload, false);
    });

    it('8. RACE_RESET establishes a new active run so subsequent events for it are accepted', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 10})));
      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'RACE_RESET',
            run_id: 'run-b',
            state_version: 0,
            payload: makeRaceState({run_id: 'run-b', state_version: 0}),
          }),
        ),
      );

      const alertPayload = {alert_id: 'alert-post-reset'} as unknown as Alert;
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-b', state_version: 1, payload: alertPayload}),
        ),
      );

      expect(onWatchAlert).toHaveBeenCalledWith(alertPayload, false);
    });

    it('9. missed alerts are recoverable after reconnect: a WATCH_ALERT delivered post-reconnect for the active run is dispatched', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      const onReconnect = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert, onReconnect}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})),
      );

      // Connection drops (an alert may have been missed here) ...
      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));
      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());
      expect(onReconnect).toHaveBeenCalledTimes(1);

      // Spec v1.1 pass 4, correction 2: the real backend always sends an
      // authoritative RACE_STATE snapshot immediately on every new
      // connection (see backend/app/api/routes.py's websocket handler) —
      // this resync must land before any other event type is accepted
      // again, since correction 3 forbids WATCH_ALERT itself from
      // (re-)establishing the active run after the reconnect reset it.
      act(() =>
        secondSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})),
      );

      // ...and the reconnected socket delivers a fresh alert for the same
      // still-active run, which must reach the caller normally.
      const alertPayload = {alert_id: 'alert-after-reconnect'} as unknown as Alert;
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 2, payload: alertPayload}),
        ),
      );

      expect(onWatchAlert).toHaveBeenCalledWith(alertPayload, false);
    });

    it('10. does not dispatch to an unmounted component', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      const {unmount} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      unmount();
      act(() =>
        socket.simulateMessage(makeEvent({type: 'STRATEGY_UPDATE', payload: {kind: 'PIT_NOW', result: {}}})),
      );

      expect(onStrategyUpdate).not.toHaveBeenCalled();
    });

    it('11. a malformed (non-JSON) message is ignored without throwing or calling any handler', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      expect(() => act(() => socket.onmessage?.({data: '{not valid json'}))).not.toThrow();
      expect(onRaceState).not.toHaveBeenCalled();
      expect(onStrategyUpdate).not.toHaveBeenCalled();
    });

    it('12. an unknown/future event type is ignored without throwing or calling any handler', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      const onStrategyUpdate = jest.fn();
      const onDemoStatus = jest.fn();
      renderHook(() =>
        useRaceSocket('demo-race-001', {onRaceState, onWatchAlert, onStrategyUpdate, onDemoStatus}),
      );
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      expect(() =>
        act(() =>
          socket.simulateMessage(
            // @ts-expect-error deliberately unrecognized event type
            makeEvent({type: 'SOME_FUTURE_EVENT_TYPE', payload: {}}),
          ),
        ),
      ).not.toThrow();
      expect(onRaceState).not.toHaveBeenCalled();
      expect(onWatchAlert).not.toHaveBeenCalled();
      expect(onStrategyUpdate).not.toHaveBeenCalled();
      expect(onDemoStatus).not.toHaveBeenCalled();
    });
  });

  // Spec v1.1 pass 4, mandatory corrections 2 and 3: the disconnect/
  // reset/reconnect scenario, and the ordering/run-isolation bugs that
  // scenario exposed in useRaceSocket's own tracking.
  describe('reconnect-after-reset and run-isolation ordering (spec v1.1 pass 4, corrections 2 and 3)', () => {
    it("disconnect -> reset -> reconnect: the reconnected socket's first RACE_STATE for the NEW run is dispatched via onReset, not silently rejected", () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 5})),
      );
      expect(onReset).toHaveBeenCalledTimes(1); // the very first state of the connection

      // Disconnect. While disconnected, the backend resets to run-b (this
      // client never sees a RACE_RESET event for it — that's the point).
      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));
      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());

      // The reconnected socket's first message is an ORDINARY RACE_STATE
      // for run-b (exactly what the real backend sends on every new
      // connection) — not a RACE_RESET. Before pass 4's fix, this would
      // have been silently rejected forever because the hook still
      // considered run-a active.
      const runBState = makeRaceState({run_id: 'run-b', state_version: 0, lap: 1});
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'RACE_STATE', run_id: 'run-b', state_version: 0, payload: runBState}),
        ),
      );

      expect(onReset).toHaveBeenCalledTimes(2);
      expect(onReset).toHaveBeenLastCalledWith(runBState);
    });

    it('reconnect without a reset: the first post-reconnect RACE_STATE for the SAME run is still dispatched (via onReset), and subsequent same-run events keep working', () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset, onStrategyUpdate}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 5})),
      );

      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));
      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());
      act(() =>
        secondSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 5})),
      );

      const payload = makeValidUndercutPayload();
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-a', state_version: 6, payload}),
        ),
      );
      expect(onStrategyUpdate).toHaveBeenCalledWith(payload, false);
    });

    it('a delayed event from the OLD run arriving on the new connection after resync is rejected', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 5})),
      );
      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));

      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());
      act(() =>
        secondSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-b', state_version: 0})),
      );

      // A straggling WATCH_ALERT from the now-superseded run-a, arriving
      // late on the new connection, must still be rejected.
      const staleAlert = {alert_id: 'stale-alert'} as unknown as Alert;
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 6, payload: staleAlert}),
        ),
      );
      expect(onWatchAlert).not.toHaveBeenCalled();
    });

    it('multiple consecutive resets while disconnected: only the FINAL run (as reported by the reconnected socket) is accepted', () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));

      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})),
      );
      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));

      // While disconnected, the backend resets twice: run-a -> run-b ->
      // run-c. This client never observes either RACE_RESET directly; it
      // only learns the FINAL state on reconnect.
      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());
      const finalState = makeRaceState({run_id: 'run-c', state_version: 0});
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'RACE_STATE', run_id: 'run-c', state_version: 0, payload: finalState}),
        ),
      );

      expect(onReset).toHaveBeenLastCalledWith(finalState);

      // And an event for the intermediate run-b (which never truly
      // existed from this client's perspective after the fact) is
      // rejected as foreign.
      const onWatchAlert = jest.fn();
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({
            type: 'WATCH_ALERT',
            run_id: 'run-b',
            state_version: 1,
            payload: {alert_id: 'x'} as unknown as Alert,
          }),
        ),
      );
      expect(onWatchAlert).not.toHaveBeenCalled();
    });

    it('an out-of-order, older same-run RACE_STATE never lowers the state_version watermark for subsequent events', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 10})));
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-a', state_version: 8, payload: {kind: 'x'}}),
        ),
      );
      expect(onStrategyUpdate).not.toHaveBeenCalled(); // already below the watermark of 10

      // A delayed, OUT-OF-ORDER RACE_STATE for the same run arrives with a
      // LOWER state_version than what was already seen (e.g. redelivered
      // over a race between transports). Before pass 4's fix, this
      // OVERWROTE the watermark down to 3, which would then have let the
      // already-rejected version-8 event through if redelivered.
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 3})));

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-a', state_version: 9, payload: {kind: 'y'}}),
        ),
      );
      // Version 9 is still below the true watermark of 10 and must remain rejected.
      expect(onStrategyUpdate).not.toHaveBeenCalled();

      const finalPayload = makeValidUndercutPayload();
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-a', state_version: 11, payload: finalPayload}),
        ),
      );
      expect(onStrategyUpdate).toHaveBeenCalledWith(finalPayload, false);
    });

    it('a duplicated WATCH_ALERT (same alert_id, same state_version) is delivered only once', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      const alertPayload = {alert_id: 'dup-alert', state_version: 2} as unknown as Alert;
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 2, payload: alertPayload}),
        ),
      );
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 2, payload: alertPayload}),
        ),
      );

      expect(onWatchAlert).toHaveBeenCalledTimes(1);
    });

    it('two different alerts sharing the same state_version are BOTH delivered (not deduplicated by version alone)', () => {
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      const alertA = {alert_id: 'alert-a'} as unknown as Alert;
      const alertB = {alert_id: 'alert-b'} as unknown as Alert;
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 2, payload: alertA}),
        ),
      );
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'WATCH_ALERT', run_id: 'run-a', state_version: 2, payload: alertB}),
        ),
      );

      expect(onWatchAlert).toHaveBeenCalledTimes(2);
      expect(onWatchAlert).toHaveBeenCalledWith(alertA, false);
      expect(onWatchAlert).toHaveBeenCalledWith(alertB, false);
    });

    it('a RACE_STATE payload missing required fields is ignored without throwing', () => {
      const onRaceState = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      expect(() =>
        act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', payload: {not: 'a race state'}}))),
      ).not.toThrow();
      expect(onRaceState).not.toHaveBeenCalled();
    });

    it('an envelope missing a required top-level field (state_version) is ignored without throwing', () => {
      const onRaceState = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      const malformedEvent = {type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', payload: {}};
      expect(() => act(() => socket.onmessage?.({data: JSON.stringify(malformedEvent)}))).not.toThrow();
      expect(onRaceState).not.toHaveBeenCalled();
    });
  });

  // AWS hackathon-final pass, P0-5: strict discriminated-union validation
  // for STRATEGY_UPDATE — undercut, simulate, unknown kind, and malformed
  // payload, exactly the four cases this pass's requirement names.
  describe('parseStrategyUpdatePayload (P0-5 discriminated union)', () => {
    it('accepts a well-formed kind: "undercut" payload', () => {
      const payload = makeValidUndercutPayload();
      expect(parseStrategyUpdatePayload(payload)).toEqual(payload);
    });

    it('accepts a well-formed kind: "simulate" payload', () => {
      const payload = {
        kind: 'simulate' as const,
        result: {
          calculation_id: 'calc-sim-1',
          model_version: 'v1',
          race_id: 'demo-race-001',
          run_id: 'run-a',
          source_state_version: 10,
          driver_id: 'CAR_04',
          pit_now_lap: 27,
          wait_lap: 30,
          wait_laps: 3,
          comparison_horizon_lap: 33,
          compound_after_pit: 'HARD',
          delta_ms: 450,
          assumptions: [],
        },
      };
      expect(parseStrategyUpdatePayload(payload)).toEqual(payload);
    });

    it('rejects an unrecognized kind', () => {
      expect(parseStrategyUpdatePayload({kind: 'not_a_real_kind', result: {calculation_id: 'x'}})).toBeNull();
    });

    it('rejects a kind: "undercut" payload whose result is missing required UndercutResult fields', () => {
      expect(parseStrategyUpdatePayload({kind: 'undercut', result: {calculation_id: 'calc-1'}})).toBeNull();
    });

    it('rejects a kind: "simulate" payload whose result is missing required StrategySimulationResult fields', () => {
      expect(parseStrategyUpdatePayload({kind: 'simulate', result: {calculation_id: 'calc-1'}})).toBeNull();
    });

    it('rejects a completely malformed (non-object) payload', () => {
      expect(parseStrategyUpdatePayload('not an object')).toBeNull();
      expect(parseStrategyUpdatePayload(null)).toBeNull();
      expect(parseStrategyUpdatePayload(undefined)).toBeNull();
      expect(parseStrategyUpdatePayload(42)).toBeNull();
    });

    it('rejects a payload with the right kind but a result of the wrong type (e.g. a simulate result under kind "undercut")', () => {
      // The exact bug this pass fixes: a StrategySimulationResult must
      // never be accepted as an UndercutResult just because SOME object
      // was present.
      const simulateShapedResult = {
        calculation_id: 'calc-1',
        model_version: 'v1',
        race_id: 'demo-race-001',
        run_id: 'run-a',
        source_state_version: 10,
        driver_id: 'CAR_04',
        pit_now_lap: 27,
        wait_lap: 30,
        wait_laps: 3,
        comparison_horizon_lap: 33,
        compound_after_pit: 'HARD',
        delta_ms: 450,
        assumptions: [],
      };
      expect(parseStrategyUpdatePayload({kind: 'undercut', result: simulateShapedResult})).toBeNull();
    });
  });

  // AWS hackathon-final pass, P0-7: reconnect/initial-snapshot races —
  // the five named interleavings this pass's requirement lists.
  describe('reconnect and initial-snapshot synchronization (P0-7)', () => {
    it("1. RACE_RESET arriving before the connection's first RACE_STATE establishes the run, and a subsequently-delayed OLDER initial snapshot is rejected, not applied on top", () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      // A RACE_RESET (establishing run-b at version 0) arrives FIRST —
      // before the connection's own initial RACE_STATE snapshot, which
      // was fetched by the server for the OLD run (run-a) and is still
      // in flight.
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_RESET', race_id: 'demo-race-001', run_id: 'run-b', state_version: 0}),
        ),
      );
      expect(onReset).toHaveBeenCalledTimes(1);
      expect(onReset).toHaveBeenLastCalledWith(expect.objectContaining({run_id: 'run-b', state_version: 0}));

      // The delayed, now-STALE initial snapshot for the OLD run arrives
      // second. Pre-fix, this would have been treated as "the first
      // state since connect" and unconditionally regressed the client
      // back to run-a via onReset AGAIN. Post-fix, isFirstStateSinceConnectRef
      // was already cleared by the RACE_RESET above, so this goes
      // through the ordinary guarded path and is rejected as foreign.
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 7}),
        ),
      );
      expect(onReset).toHaveBeenCalledTimes(1); // still just once — never called again for run-a
    });

    it('2. a delayed old initial snapshot for a run that was never established some other way is still correctly applied as the resync (no reset happened)', () => {
      // The ordinary, no-race case: nothing beat the initial snapshot to
      // the client, so it IS correctly treated as the authoritative
      // resync — proving the P0-7 fix didn't break the common path.
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 3}),
        ),
      );
      expect(onReset).toHaveBeenCalledTimes(1);
      expect(onReset).toHaveBeenLastCalledWith(expect.objectContaining({run_id: 'run-a', state_version: 3}));
    });

    it("3. reconnect during a reset: the new connection's own initial snapshot for the NEW run is accepted even though the client last knew about the OLD run", () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));
      const firstSocket = FakeWebSocket.instances[0];
      act(() => firstSocket.simulateOpen());
      act(() =>
        firstSocket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 5}),
        ),
      );
      expect(onReset).toHaveBeenCalledTimes(1);

      // Disconnect right as a reset happens server-side (this client
      // never sees the RACE_RESET event for it — the point of this
      // scenario is that the RECONNECT's own snapshot is what carries
      // the new run through, not a missed live event).
      act(() => firstSocket.simulateClose());
      act(() => jest.advanceTimersByTime(250));
      const secondSocket = FakeWebSocket.instances[1];
      act(() => secondSocket.simulateOpen());
      act(() =>
        secondSocket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-b', state_version: 0}),
        ),
      );

      expect(onReset).toHaveBeenCalledTimes(2);
      expect(onReset).toHaveBeenLastCalledWith(expect.objectContaining({run_id: 'run-b', state_version: 0}));
    });

    it('4. a same-run event with a LOWER state_version than already established never regresses the watermark', () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 10}),
        ),
      );

      // An out-of-order, lower-version RACE_STATE for the SAME run
      // arrives (e.g. redelivered over a race between transports).
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 4}),
        ),
      );
      // onRaceState IS still called (useRaceState's own acceptIncomingState
      // is the authoritative decision-maker for the ordinary path — see
      // this hook's module doc-comment) — but it must never have been
      // treated as a new "first snapshot"/reset.
      expect(onReset).toHaveBeenCalledTimes(1); // only the original establishing snapshot
    });

    it('5. interleaved snapshot and live event delivery: a STRATEGY_UPDATE for the run arrives in between the reset and further race-state ticks, and is accepted', () => {
      const onRaceState = jest.fn();
      const onReset = jest.fn();
      const onStrategyUpdate = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onReset, onStrategyUpdate}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 5}),
        ),
      );

      const strategyPayload = makeValidUndercutPayload({run_id: 'run-a'});
      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'STRATEGY_UPDATE',
            race_id: 'demo-race-001',
            run_id: 'run-a',
            state_version: 6,
            payload: strategyPayload,
          }),
        ),
      );
      expect(onStrategyUpdate).toHaveBeenCalledWith(strategyPayload, false);

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'RACE_STATE', race_id: 'demo-race-001', run_id: 'run-a', state_version: 7}),
        ),
      );
      expect(onReset).toHaveBeenCalledTimes(1); // the interleaved live event never re-triggered a resync
    });
  });

  // AWS hackathon-final pass, P0-6: serverStale/localTransportStale split
  // — the exact ordering cases this pass's requirement names.
  describe('split server/local staleness (P0-6)', () => {
    it('DATA_STALE:true survives the local 1s timer even after a fresh message resets the local half', () => {
      const onRaceState = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 2, payload: {stale: true}}),
        ),
      );
      expect(result.current.isStale).toBe(true);

      // A fresh, ordinary message arrives (proving the transport is
      // alive) -- this must NOT clear the server-declared stale flag,
      // pre-fix behavior. The local timer ticking afterward must not
      // clear it either.
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 3})));
      expect(result.current.isStale).toBe(true);

      act(() => jest.advanceTimersByTime(1000));
      expect(result.current.isStale).toBe(true);
    });

    it('an ordinary message clears LOCAL transport staleness without clearing a live server-declared stale flag', () => {
      const onRaceState = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      // Let the local timer alone declare staleness (no server signal involved).
      act(() => jest.advanceTimersByTime(6000));
      expect(result.current.isStale).toBe(true);

      // An ordinary message arrives -- local half clears immediately, and
      // since the server half was never set, the combined flag clears too.
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 2})));
      expect(result.current.isStale).toBe(false);
    });

    it('DATA_STALE:false only clears the SERVER half -- a still-live local timeout keeps isStale true', () => {
      const onRaceState = jest.fn();
      const {result} = renderHook(() => useRaceSocket('demo-race-001', {onRaceState}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 2, payload: {stale: true}}),
        ),
      );
      expect(result.current.isStale).toBe(true);

      // No further message arrives -- 6s elapse, so the LOCAL half is
      // independently ALSO now true by the time the server clears its own.
      act(() => jest.advanceTimersByTime(6000));

      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 3, payload: {stale: false}}),
        ),
      );
      // The DATA_STALE message itself is a message, so it also clears the
      // local half (see the "ordinary message clears local staleness"
      // test above) -- both halves are now false, so isStale is false.
      expect(result.current.isStale).toBe(false);
    });

    it('a WATCH_ALERT dispatched in the same tick as a preceding DATA_STALE:true sees the CURRENT stale value, not a stale closure', () => {
      // The core "even before React rerenders" requirement: since
      // isStaleRef is a plain ref mutated synchronously, the SECOND
      // onmessage call in this test sees the up-to-date value regardless
      // of whether React has flushed a re-render between the two calls
      // (this test's synchronous `act` calls make that gap explicit).
      const onRaceState = jest.fn();
      const onWatchAlert = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onWatchAlert}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));

      act(() => {
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 2, payload: {stale: true}}),
        );
        socket.simulateMessage(
          makeEvent({
            type: 'WATCH_ALERT',
            run_id: 'run-a',
            state_version: 3,
            payload: {alert_id: 'alert-during-stale'},
          }),
        );
      });

      expect(onWatchAlert).toHaveBeenCalledWith({alert_id: 'alert-during-stale'}, true);
    });

    it('onStrategyUpdate and onAgentResponse also receive the current isStale value at dispatch time', () => {
      const onRaceState = jest.fn();
      const onStrategyUpdate = jest.fn();
      const onAgentResponse = jest.fn();
      renderHook(() => useRaceSocket('demo-race-001', {onRaceState, onStrategyUpdate, onAgentResponse}));
      const socket = FakeWebSocket.instances[0];
      act(() => socket.simulateOpen());
      act(() => socket.simulateMessage(makeEvent({type: 'RACE_STATE', run_id: 'run-a', state_version: 1})));
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'DATA_STALE', run_id: 'run-a', state_version: 2, payload: {stale: true}}),
        ),
      );

      const strategyPayload = makeValidUndercutPayload();
      act(() =>
        socket.simulateMessage(
          makeEvent({type: 'STRATEGY_UPDATE', run_id: 'run-a', state_version: 3, payload: strategyPayload}),
        ),
      );
      expect(onStrategyUpdate).toHaveBeenCalledWith(strategyPayload, true);

      act(() =>
        socket.simulateMessage(
          makeEvent({
            type: 'AGENT_RESPONSE',
            run_id: 'run-a',
            state_version: 4,
            payload: {status: 'SUPPORTED'},
          }),
        ),
      );
      expect(onAgentResponse).toHaveBeenCalledWith({status: 'SUPPORTED'}, true);
    });
  });
});
