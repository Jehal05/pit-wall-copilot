import {useEffect, useRef, useState} from 'react';
import {API_BASE_URL, assertSecureTransportForRelease, resolveAuthToken} from '../api/client';
import {Alert, RaceEvent, RaceState, StrategySimulationResult, UndercutResult} from '../api/types';

export type SocketConnectionState = 'connecting' | 'open' | 'closed';

/** AWS hackathon-final pass, P0-5: STRATEGY_UPDATE payload shape, as a
 * STRICT discriminated union rather than the previous `{kind: string;
 * result: unknown}` shape (which every caller then had to blindly cast
 * with `as UndercutResult` — see App.tsx's old `onStrategyUpdate` handler
 * before this pass, which cast a `kind: "simulate"` payload's
 * StrategySimulationResult to UndercutResult just the same as an actual
 * undercut result, silently corrupting the Strategy Battle view any time
 * a simulation ran). Backend/app/api/routes.py only ever emits
 * `kind: "undercut"` (paired with an UndercutResult) or
 * `kind: "simulate"` (paired with a StrategySimulationResult) — see
 * those two `event_broadcaster.broadcast(...)` call sites' own
 * `payload={"kind": ..., "result": ...}` literals. `parseStrategyUpdatePayload`
 * below is the one place that turns an untrusted wire payload into this
 * union, validating BOTH the discriminant and the shape of `result`
 * before anything downstream ever sees it — an unknown `kind` or a
 * `result` missing required fields is rejected (returns `null`) rather
 * than silently passed through as if it were valid. */
export type StrategyUpdatePayload =
  | {kind: 'undercut'; result: UndercutResult}
  | {kind: 'simulate'; result: StrategySimulationResult};

function isUndercutResult(value: unknown): value is UndercutResult {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.calculation_id === 'string' &&
    typeof value.candidate === 'string' &&
    typeof value.target === 'string' &&
    typeof value.window === 'string' &&
    typeof value.expected_margin_ms === 'number' &&
    isRecord(value.confidence)
  );
}

function isStrategySimulationResult(value: unknown): value is StrategySimulationResult {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.calculation_id === 'string' &&
    typeof value.driver_id === 'string' &&
    typeof value.pit_now_lap === 'number' &&
    typeof value.wait_lap === 'number' &&
    typeof value.delta_ms === 'number'
  );
}

/** Validates an untrusted STRATEGY_UPDATE wire payload against the
 * discriminated union above. Returns `null` (never throws) for an
 * unrecognized `kind` or a `result` that doesn't match the shape its own
 * `kind` promises — the caller (useRaceSocket's dispatch loop) logs and
 * drops these rather than forwarding a payload that would make the UI
 * show `undefined`/`NaN`/partial data (P0-5's explicit requirement). */
export function parseStrategyUpdatePayload(payload: unknown): StrategyUpdatePayload | null {
  if (!isRecord(payload)) {
    return null;
  }
  if (payload.kind === 'undercut' && isUndercutResult(payload.result)) {
    return {kind: 'undercut', result: payload.result};
  }
  if (payload.kind === 'simulate' && isStrategySimulationResult(payload.result)) {
    return {kind: 'simulate', result: payload.result};
  }
  return null;
}

/** Payload shape of a DATA_STALE event — an explicit, server-pushed
 * staleness signal that a client should treat as authoritative the
 * instant it arrives, in ADDITION to (not instead of) the client's own
 * local 5s-since-last-message timer (see `isStale` below). `stale: false`
 * is an explicit "no longer stale" signal a server may push once fresh
 * data resumes, distinct from simply not sending DATA_STALE again. */
export interface DataStalePayload {
  stale: boolean;
  reason?: string;
}

/** Payload shape of a DEMO_STATUS event — status/progress information
 * about the running demo scenario (e.g. current lap, scenario phase).
 * Not every deployment emits this event; it is handled defensively
 * (an optional callback) rather than assumed present. */
export type DemoStatusPayload = Record<string, unknown>;

export interface UseRaceSocketOptions {
  /** Called for every RACE_STATE/RACE_RESET event, already passed through
   * the SAME run_id/state_version acceptance guard useRaceState's own
   * poll loop uses (pass `acceptIncomingState` from useRaceState here) —
   * spec v1.1 repair 3's "ignore inactive run_id/lower state_version". */
  onRaceState: (state: RaceState) => void;
  /** Called specifically for RACE_RESET (in addition to onRaceState) so a
   * caller can also re-establish the active run — pass
   * `establishActiveRun` from useRaceState here. */
  onReset?: (state: RaceState) => void;
  /** AWS hackathon-final pass, P0-6: the second argument is the combined
   * (server-OR-local) staleness flag, read directly from `isStaleRef` at
   * the exact moment this event was dispatched — NOT the `isStale` React
   * state a caller might otherwise close over from a stale render. Use
   * it to decide whether to surface this alert as a trusted proactive
   * notification (spec v1.1 pass 4, correction 4: "show the proactive
   * overlay only when data is fresh") while still doing any
   * always-applicable bookkeeping (e.g. refreshing an alerts list)
   * unconditionally. */
  onWatchAlert?: (alert: Alert, isStale: boolean) => void;
  /** See `onWatchAlert`'s doc-comment on the second (`isStale`) argument. */
  onAgentResponse?: (payload: unknown, isStale: boolean) => void;
  /** Called for every accepted STRATEGY_UPDATE event (spec v1.1 pass 3,
   * correction 2 — this event type must no longer be silently ignored).
   * See `onWatchAlert`'s doc-comment on the second (`isStale`) argument
   * — P0-6 requires "suppress strategy/agent recommendations while
   * stale," so a caller should not apply this result as authoritative
   * when `isStale` is true. */
  onStrategyUpdate?: (payload: StrategyUpdatePayload, isStale: boolean) => void;
  /** Called for every accepted DEMO_STATUS event, when the backend emits
   * one (spec v1.1 pass 3, correction 2). */
  onDemoStatus?: (payload: DemoStatusPayload) => void;
  /** Called whenever a server-pushed DATA_STALE event arrives, with the
   * payload's own `stale` flag — lets a caller react immediately (e.g.
   * show/hide a "data may be out of date" banner) rather than waiting for
   * the client's own local staleness timer to catch up. */
  onDataStaleChange?: (payload: DataStalePayload) => void;
  /** Called once a reconnect succeeds, after a period of disconnection —
   * the conventional use is to trigger one REST refresh so the client is
   * caught up on anything it couldn't have received while disconnected,
   * since this server does not replay missed events to a reconnecting
   * client (no event store is configured — see mcp_server/server.py's
   * unrelated but analogous EventStore note for the same tradeoff
   * described on the MCP side). */
  onReconnect?: () => void;
  /** AWS hackathon-final pass, P2 (Fire TV polish, following the same
   * pass's ADR 0002 WebSocket-auth addition). Called when the backend
   * closes the connection with code 4401 (an invalid, missing, or
   * revoked `?token=` — see `credential_is_valid` in
   * backend/app/api/deps.py). This is NOT a transient/reconnectable
   * failure the way a dropped network connection is: retrying with the
   * SAME rejected token will only ever be rejected again. Without this
   * callback, the pre-existing `onclose = scheduleReconnect` wiring
   * would retry forever at the capped 2s backoff interval, silently
   * hammering the backend with a token that can never succeed, while
   * giving the person no indication anything is wrong — exactly the kind
   * of "expired/invalid auth must produce a clear ... error and return
   * to pairing/auth" gap `useDeviceAuth.ts` already closes for every
   * REST call via `AuthExpiredError`, just not (until this pass) for the
   * WebSocket connection. The typical wiring is
   * `onAuthFailure: deviceAuth.demoteToUnpairedDueToRejectedCredential`. */
  onAuthFailure?: () => void;
}

export interface UseRaceSocketResult {
  connectionState: SocketConnectionState;
  /** True once more than 5000ms have elapsed since the last message of
   * ANY kind was received (including reconnects), OR the server pushed an
   * explicit `DATA_STALE {stale: true}` event — spec v1.1 repair 3:
   * "suppress proactive alerts when stale>5s". A caller should check this
   * before surfacing a WATCH_ALERT as a proactive notification (an alert
   * received right as staleness was detected may be describing state the
   * user can no longer trust is current). Cleared either by the local
   * timer (a fresh message arrives) or by an explicit
   * `DATA_STALE {stale: false}` event. */
  isStale: boolean;
}

const STALE_THRESHOLD_MS = 5000;
const STALE_CHECK_INTERVAL_MS = 1000;
// Reconnect backoff: 250ms, 500ms, 1000ms, 2000ms, capped at 2000ms — the
// first (and every subsequent) reconnect ATTEMPT is scheduled well within
// the spec's "<5s" bound; this is deliberately much tighter than the 5s
// ceiling itself, since 5s is a maximum tolerance, not a target.
const RECONNECT_DELAYS_MS = [250, 500, 1000, 2000];

/**
 * Spec v1.1 correction 4: "Fire TV must derive wss:// from an HTTPS API
 * URL." `API_BASE_URL` is whatever `BackendUrl` the deployed stack
 * actually reports (see infrastructure/template.yaml's `BackendUrl`
 * output, which is `https://` whenever `CertificateArn` was configured —
 * the required secure default — and only ever `http://` when the stack
 * was explicitly deployed with `AllowInsecureDemoHttp=true`). The regex
 * below only ever touches the LEADING scheme token, so:
 *   "https://api.example.com" -> "wss://api.example.com/v1/races/.../ws"
 *   "http://10.0.2.2:8000"    -> "ws://10.0.2.2:8000/v1/races/.../ws"
 * There is no separate, independently-configured WebSocket URL anywhere
 * in this client that could drift from the API URL's own scheme (and
 * silently downgrade to plaintext ws:// against an https:// backend, or
 * the reverse) — this derivation is the ONLY place a WebSocket URL is
 * ever constructed. Exported for direct unit testing (see
 * `useRaceSocket.test.ts`'s scheme-derivation tests).
 *
 * `apiBaseUrl` defaults to the real, build-time-inlined `API_BASE_URL`
 * (see client.ts) — it is only ever overridden explicitly in tests, which
 * need to exercise both the http-> and https-derived schemes without
 * depending on whatever single value `.env` happened to inline for this
 * test run.
 *
 * AWS hackathon-final pass, P2-12: appends `?token=...` when
 * `resolveAuthToken()` has a credential (the same one `api/client.ts`
 * attaches to every HTTP request as `X-Demo-Access-Token`) — the backend
 * WebSocket route now requires it (see backend/app/api/routes.py's
 * `race_websocket`), and a browser/React-Native `WebSocket` cannot set a
 * custom header on the handshake request, so a query parameter is the
 * standard workaround. `encodeURIComponent` guards against a token value
 * (however this client's own token format never actually produces one)
 * containing a character that would otherwise corrupt the URL. When no
 * token is available yet (e.g. connecting before pairing has completed),
 * the URL is left bare and the connection is refused by the backend with
 * close code 4401 — the same "not yet authenticated" outcome an HTTP
 * request without the header gets today, not a new failure mode.
 */
export function toWebSocketUrl(raceId: string, apiBaseUrl: string = API_BASE_URL): string {
  const wsBase = apiBaseUrl.replace(/^http/, 'ws');
  const token = resolveAuthToken();
  const tokenSuffix = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${wsBase}/v1/races/${raceId}/ws${tokenSuffix}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** Spec v1.1 pass 4, correction 3: "Validate the complete event envelope
 * ... before applying it." Every event this server emits carries type,
 * race_id, run_id (string), state_version (number) and a payload — see
 * `RaceEvent` in `../api/types`. Anything missing one of these required
 * fields is treated the same as an unparseable message: dropped, never
 * partially applied. */
function isValidEventEnvelope(value: unknown): value is RaceEvent {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.type === 'string' &&
    typeof value.race_id === 'string' &&
    typeof value.run_id === 'string' &&
    typeof value.state_version === 'number' &&
    'payload' in value
  );
}

/** Minimal structural check for a RACE_STATE/RACE_RESET payload — just
 * enough to guarantee `run_id`/`state_version` (which the run/version
 * guard and `useRaceState.acceptIncomingState` both dereference) are
 * actually present, without re-implementing full `RaceState` validation
 * here. */
function isValidRaceStatePayload(value: unknown): value is RaceState {
  return isRecord(value) && typeof value.run_id === 'string' && typeof value.state_version === 'number';
}

/** AWS hackathon-final pass, P0-7: "validate envelope+payload agree on
 * race_id/run_id/state_version." The envelope (`RaceEvent`'s own
 * top-level fields) and the payload (the embedded `RaceState`) are
 * produced together server-side and SHOULD always agree, but nothing
 * previously checked that they actually do — a bug or a future change on
 * either side (or a message doctored in transit, defense in depth) that
 * let them disagree would silently establish the run/watermark from ONE
 * of the two while displaying data described by the OTHER. Rejecting a
 * disagreement outright (rather than picking one side to trust) is the
 * safe default here. */
function envelopeAgreesWithPayload(event: RaceEvent, payload: RaceState): boolean {
  return (
    event.race_id === payload.race_id &&
    event.run_id === payload.run_id &&
    event.state_version === payload.state_version
  );
}

/**
 * Spec v1.1 repair 3: the primary path for race-state/alert/agent-answer
 * delivery. `useRaceState`'s REST poll loop remains running underneath
 * unconditionally as the mandated fallback ("When WebSocket is
 * unavailable, poll race state every two seconds") — this hook does not
 * control or pause that poll loop; the two simply both feed the same
 * `acceptIncomingState` guard, so whichever transport delivers a given
 * state first "wins" and the other's delivery of the same state is a
 * harmless no-op (state_version does not advance twice for one state).
 */
export function useRaceSocket(raceId: string, options: UseRaceSocketOptions): UseRaceSocketResult {
  const [connectionState, setConnectionState] = useState<SocketConnectionState>('connecting');
  const [isStale, setIsStale] = useState(false);

  // AWS hackathon-final pass, P0-6: staleness used to be a single boolean
  // written by TWO independent, un-coordinated writers -- the local 1s
  // timer (below) and the server's DATA_STALE push -- with no rule for
  // which one wins. Concretely: `lastMessageAtRef` is updated on EVERY
  // inbound message, including the DATA_STALE message itself, so the very
  // next 1s timer tick saw a fresh timestamp and called `setIsStale(false)`
  // -- silently clearing a server-declared staleness the server had not
  // retracted. This is fixed by tracking the two signals as SEPARATE refs
  // (a ref, not state, so the combined value is available SYNCHRONOUSLY
  // inside this hook's own dispatch code below -- no waiting for a React
  // re-render, which is what "even before React rerenders" requires) and
  // OR-ing them for the single combined value every consumer actually
  // cares about:
  //   - `serverStaleRef`: set directly by an explicit DATA_STALE event.
  //     `{stale: true}` sets it true; `{stale: false}` sets it false. The
  //     local timer below NEVER touches this ref -- only another
  //     DATA_STALE event can clear it, so it genuinely "survives" every
  //     1s timer tick until the server itself retracts it.
  //   - `localTransportStaleRef`: set by the local 1s
  //     time-since-last-message timer, same as before. An ordinary
  //     message (including a DATA_STALE message, which by definition just
  //     proved the transport is alive) updates `lastMessageAtRef`, which
  //     this timer uses -- so local transport staleness clears on its own
  //     next tick after any message, WITHOUT ever touching
  //     `serverStaleRef`.
  // `isStaleRef.current` (recomputed by `recomputeStale` below every time
  // either input changes) is what this hook's OWN dispatch logic checks
  // before invoking onWatchAlert/onStrategyUpdate/onAgentResponse -- see
  // those cases below -- so "suppress strategy/agent recommendations
  // while stale" is enforced with the always-current ref value, not a
  // value closed over from whatever React render happened to produce the
  // callback (the stale-closure bug the ref exists to eliminate). The
  // `isStale` REACT STATE below stays in sync with the same ref purely so
  // existing consumers (StaleDataBadge) keep working via ordinary render
  // props for display purposes, where a one-tick render delay is fine.
  const serverStaleRef = useRef(false);
  const localTransportStaleRef = useRef(false);
  const isStaleRef = useRef(false);
  const recomputeStale = () => {
    const combined = serverStaleRef.current || localTransportStaleRef.current;
    if (combined !== isStaleRef.current) {
      isStaleRef.current = combined;
      setIsStale(combined);
    }
  };

  const lastMessageAtRef = useRef<number>(Date.now());
  const attemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const isMountedRef = useRef(true);
  const hasConnectedOnceRef = useRef(false);
  const tokenWasSentRef = useRef(false);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  // Spec v1.1 pass 4, mandatory correction 2: the very first RACE_STATE
  // received on ANY (re)connection is treated as an AUTHORITATIVE
  // resynchronization — dispatched via `onReset` (which App.tsx wires to
  // `establishActiveRun`), not the ordinary `onRaceState`/
  // `acceptIncomingState` path, which refuses to switch the active run on
  // its own. This is what fixes "server sends run B as an ordinary
  // initial RACE_STATE after reconnect, client rejects run B because run A
  // is still considered active": the server always sends exactly one
  // RACE_STATE snapshot immediately on every new connection (see
  // backend/app/api/routes.py's websocket handler), so that first message
  // — on the very first connection too, not just a reconnect — is always
  // exactly this "fresh state request" the correction calls for. Every
  // SUBSEQUENT RACE_STATE on the same connection goes through the normal
  // guarded path, so an ordinary in-run update still cannot switch runs.
  const isFirstStateSinceConnectRef = useRef(true);

  // Local run_id/state_version guard for event types that don't already
  // pass through useRaceState's own `acceptIncomingState` (STRATEGY_UPDATE,
  // WATCH_ALERT, AGENT_RESPONSE, DATA_STALE, DEMO_STATUS) — spec v1.1 pass
  // 3, correction 2: these must not be applied on top of a superseded run,
  // the same way RACE_STATE/RACE_RESET already aren't. RACE_RESET always
  // establishes a new active run; RACE_STATE establishes one only the
  // first time (mirrors useRaceState's own `activeRunIdRef` semantics,
  // kept independently here since this hook has no visibility into
  // useRaceState's internal ref).
  const activeRunIdRef = useRef<string | null>(null);
  const lastStateVersionByRunRef = useRef<Map<string, number>>(new Map());

  // A stable identifier seen per accepted alert (spec v1.1 pass 4,
  // correction 3: "deduplicate genuinely duplicated alert events using a
  // stable identifier where available"). Alerts can legitimately be
  // re-delivered (e.g. a reconnect resync, or a broadcaster retry) without
  // the underlying state_version changing at all -- the run/version guard
  // alone does not catch a same-version redelivery of the SAME alert.
  const seenAlertIdsRef = useRef<Set<string>>(new Set());

  /** True if `event` belongs to a run that is not (or no longer) the
   * active one, or repeats/regresses a state_version already seen for its
   * run — i.e. it should be dropped rather than dispatched. Also updates
   * the tracked active run / max-seen state_version as a side effect for
   * events that ARE accepted, so this must be called at most once per
   * incoming event, in dispatch order.
   *
   * Spec v1.1 pass 4, correction 3 fixes three real bugs the pass-3
   * version of this guard had:
   *   1. `establishesRun` used to OVERWRITE the per-run watermark with
   *      the incoming event's own state_version, so an older, out-of-order
   *      same-run RACE_STATE could LOWER the watermark and let an
   *      already-rejected lower-version alert/strategy event through on a
   *      later delivery. Fixed by merging with `Math.max` here too, the
   *      same as the non-establishing branch already did.
   *   2. A non-establishing event (WATCH_ALERT/STRATEGY_UPDATE/
   *      AGENT_RESPONSE/DEMO_STATUS) arriving before ANY authoritative
   *      RACE_STATE/RACE_RESET used to be accepted and allowed to
   *      bootstrap the active run itself. Only an authoritative sync
   *      (`establishesRun: true`, set exclusively by the RACE_STATE and
   *      RACE_RESET cases below) may ever establish or change the active
   *      run — every other event type is dropped until one arrives.
   *   3. `activeRunIdRef.current` is reset to `null` on every genuine
   *      reconnect (see `onopen` below), not just on remount — so a
   *      missed RACE_RESET while disconnected can never leave the client
   *      permanently stuck rejecting its own backend's current run: the
   *      very next RACE_STATE (which the server always sends immediately
   *      on every new connection) re-establishes tracking cleanly,
   *      merging with whatever this run_id's watermark already was rather
   *      than assuming disconnection means "nothing changed".
   */
  const shouldRejectAsStaleOrInactive = (
    event: RaceEvent,
    opts: {establishesRun?: boolean} = {},
  ): boolean => {
    if (opts.establishesRun) {
      activeRunIdRef.current = event.run_id;
      const priorMax = lastStateVersionByRunRef.current.get(event.run_id) ?? -1;
      lastStateVersionByRunRef.current.set(event.run_id, Math.max(priorMax, event.state_version));
      return false;
    }
    if (activeRunIdRef.current === null || event.run_id !== activeRunIdRef.current) {
      // Either no authoritative run has been established yet (only
      // RACE_STATE/RACE_RESET may do that — see the docstring above), or
      // this event belongs to a stale/foreign run. Either way: reject.
      return true;
    }
    const lastSeen = lastStateVersionByRunRef.current.get(event.run_id) ?? -1;
    if (event.state_version < lastSeen) {
      return true; // out-of-order delivery of an older state_version
    }
    lastStateVersionByRunRef.current.set(event.run_id, Math.max(lastSeen, event.state_version));
    return false;
  };

  useEffect(() => {
    isMountedRef.current = true;
    activeRunIdRef.current = null;
    lastStateVersionByRunRef.current = new Map();

    const connect = () => {
      if (!isMountedRef.current) {
        return;
      }
      setConnectionState('connecting');
      isFirstStateSinceConnectRef.current = true;
      const wsUrl = toWebSocketUrl(raceId);
      // AWS hackathon-final pass, P2 (Fire TV polish): whether THIS
      // connection attempt actually carried a token, captured at connect
      // time so `onclose` can tell apart "no token was available yet"
      // (e.g. the app just launched and useDeviceAuth's own secure-
      // storage check hasn't resolved yet, or the device is genuinely
      // still unpaired) from "a real, present token was sent and the
      // backend rejected it" — only the latter is a meaningful
      // demote-to-pairing signal (see `onAuthFailure`'s own doc-comment).
      // A connection attempt with no token was never going to succeed
      // either way, and calling `onAuthFailure` for it would fire
      // `demoteToUnpairedDueToRejectedCredential` on every cold app
      // launch's very first (token-less) connection attempt, racing
      // ahead of `useDeviceAuth`'s own bootstrap check.
      tokenWasSentRef.current = wsUrl.includes('?token=');
      // Spec v1.1 pass 3, correction 8: a release build must never open a
      // plaintext ws:// connection (which would also carry no request
      // credentials, but nonetheless must not be attempted at all).
      assertSecureTransportForRelease(wsUrl);
      const socket = new WebSocket(wsUrl);
      socketRef.current = socket;

      socket.onopen = () => {
        if (!isMountedRef.current) {
          return;
        }
        lastMessageAtRef.current = Date.now();
        setConnectionState('open');
        if (hasConnectedOnceRef.current) {
          // Spec v1.1 pass 4, mandatory correction 2: a genuine RECONNECT
          // (as opposed to the very first connection) clears this hook's
          // own active-run tracking. Without this, a RACE_RESET that fired
          // while disconnected is permanently missed: this hook would keep
          // comparing every subsequent event against the OLD run_id it
          // last saw before dropping, forever rejecting the new run's
          // events as "foreign". Clearing `activeRunIdRef` (but NOT the
          // per-run_id state_version watermarks in
          // `lastStateVersionByRunRef` — those are kept so a same-run
          // reconnect still can't have its watermark regress) means the
          // very next event this hook is allowed to use to establish a run
          // (RACE_STATE or RACE_RESET) does so fresh — and the server
          // always sends an authoritative RACE_STATE snapshot immediately
          // on every new connection (see backend/app/api/routes.py's
          // websocket handler), so this is resolved within one message,
          // not left pending on a future push.
          activeRunIdRef.current = null;
          optionsRef.current.onReconnect?.();
        }
        hasConnectedOnceRef.current = true;
        attemptRef.current = 0; // reset backoff after a successful connection
      };

      socket.onmessage = (messageEvent: WebSocketMessageEvent) => {
        if (!isMountedRef.current || typeof messageEvent.data !== 'string') {
          return;
        }
        lastMessageAtRef.current = Date.now();
        // P0-6: ANY message (this one, whatever type it turns out to be)
        // proves the transport is alive right now, so clear the LOCAL
        // half of staleness immediately rather than waiting up to 1s for
        // the next timer tick — this is the "normal messages clear local
        // transport staleness" half of this pass's requirement. This
        // never touches `serverStaleRef`: a still-live DATA_STALE:true
        // from the server is untouched by the mere fact that some
        // message (including that very DATA_STALE message, or an
        // unrelated one arriving after it) was received — only another
        // DATA_STALE event can change the server half. See
        // `recomputeStale`'s own doc-comment above for the full picture.
        localTransportStaleRef.current = false;
        recomputeStale();
        let event: RaceEvent;
        try {
          event = JSON.parse(messageEvent.data);
        } catch {
          return; // malformed message — ignore rather than crash the socket
        }

        if (!isValidEventEnvelope(event)) {
          return; // malformed/unrecognized envelope — ignore rather than crash the socket
        }

        switch (event.type) {
          case 'RACE_STATE': {
            if (!isValidRaceStatePayload(event.payload)) {
              break; // required payload fields missing — ignore rather than crash
            }
            const state = event.payload as RaceState;
            if (!envelopeAgreesWithPayload(event, state)) {
              break; // P0-7: envelope/payload disagreement — reject rather than trust either side blindly
            }
            shouldRejectAsStaleOrInactive(event, {
              establishesRun: activeRunIdRef.current === null || event.run_id === activeRunIdRef.current,
            });
            if (isFirstStateSinceConnectRef.current) {
              // Spec v1.1 mandatory correction 2: the connection's first
              // snapshot is an authoritative resync — see the docstring on
              // `isFirstStateSinceConnectRef` above. Dispatched exactly
              // like RACE_RESET (onReset, which App.tsx wires to
              // `establishActiveRun`) so it CAN switch the active run, not
              // the ordinary onRaceState/acceptIncomingState path (which
              // refuses to).
              isFirstStateSinceConnectRef.current = false;
              if (optionsRef.current.onReset) {
                optionsRef.current.onReset(state);
              } else {
                optionsRef.current.onRaceState(state);
              }
            } else {
              // An ordinary in-connection update: delegates to
              // useRaceState's own acceptIncomingState (passed in as
              // onRaceState), which is the authoritative run_id/
              // state_version guard for displayed race state — never
              // rejected at THIS layer; useRaceState decides.
              optionsRef.current.onRaceState(state);
            }
            break;
          }
          case 'RACE_RESET': {
            if (!isValidRaceStatePayload(event.payload) || !envelopeAgreesWithPayload(event, event.payload)) {
              break;
            }
            // A reset always establishes a NEW active run, by definition
            // — this is the one event type allowed to change which run
            // this hook (and, via onReset, useRaceState) treats as active.
            const state = event.payload as RaceState;
            shouldRejectAsStaleOrInactive(event, {establishesRun: true});
            // AWS hackathon-final pass, P0-7: this is the fix for "a
            // RACE_RESET can arrive before the first RACE_STATE." Before
            // this line existed, a RACE_RESET never cleared
            // `isFirstStateSinceConnectRef` -- so if the connection's
            // still-in-flight initial RACE_STATE snapshot (fetched by the
            // server before this reset happened, and therefore now
            // STALE) arrived immediately afterward, the RACE_STATE case
            // below would see `isFirstStateSinceConnectRef.current` still
            // true and treat that stale snapshot as an unconditional
            // "authoritative resync" -- regressing the client straight
            // back past the reset it had JUST correctly applied. Clearing
            // it here means that delayed snapshot instead falls through
            // to the ORDINARY RACE_STATE path, which defers to
            // `onRaceState`/`acceptIncomingState`'s own run_id/
            // state_version guard and correctly rejects it as belonging
            // to a superseded run.
            isFirstStateSinceConnectRef.current = false;
            if (optionsRef.current.onReset) {
              optionsRef.current.onReset(state);
            } else {
              optionsRef.current.onRaceState(state);
            }
            break;
          }
          case 'WATCH_ALERT': {
            if (!isRecord(event.payload) || typeof event.payload.alert_id !== 'string') {
              break; // required payload field missing — ignore rather than crash
            }
            if (shouldRejectAsStaleOrInactive(event)) {
              break;
            }
            const alert = event.payload as unknown as Alert;
            // Spec v1.1 pass 4, correction 3: dedupe a genuinely
            // redelivered alert (same alert_id) — the run/version guard
            // above does not catch this, since a redelivery can arrive at
            // the SAME state_version as the first delivery (e.g. after a
            // reconnect resync). A truly NEW alert always has a new
            // alert_id even if two alerts happen to share a state_version
            // (different watches transitioning in the same tick), so this
            // never suppresses a legitimate second alert.
            if (alert?.alert_id && seenAlertIdsRef.current.has(alert.alert_id)) {
              break;
            }
            if (alert?.alert_id) {
              seenAlertIdsRef.current.add(alert.alert_id);
            }
            // P0-6: `isStaleRef.current` is read HERE, synchronously, at
            // dispatch time — never via a closed-over `isStale` value
            // that might reflect a render that hasn't happened yet.
            optionsRef.current.onWatchAlert?.(alert, isStaleRef.current);
            break;
          }
          case 'AGENT_RESPONSE':
            if (shouldRejectAsStaleOrInactive(event)) {
              break;
            }
            optionsRef.current.onAgentResponse?.(event.payload, isStaleRef.current);
            break;
          case 'STRATEGY_UPDATE': {
            // P0-5: strict discriminated-union validation — an unknown
            // `kind`, or a `result` that doesn't match the shape its own
            // `kind` promises, is rejected (logged, not forwarded) rather
            // than blindly cast, so the Strategy Battle / Simulation
            // Comparison views can never be fed the wrong result type or
            // partial/malformed data.
            const parsed = parseStrategyUpdatePayload(event.payload);
            if (parsed === null) {
              console.warn(
                'useRaceSocket: dropped an unrecognized/malformed STRATEGY_UPDATE payload',
                event.payload,
              );
              break;
            }
            if (shouldRejectAsStaleOrInactive(event)) {
              break;
            }
            optionsRef.current.onStrategyUpdate?.(parsed, isStaleRef.current);
            break;
          }
          case 'DEMO_STATUS':
            if (shouldRejectAsStaleOrInactive(event)) {
              break;
            }
            optionsRef.current.onDemoStatus?.(event.payload as DemoStatusPayload);
            break;
          case 'DATA_STALE': {
            // Spec v1.1 pass 4, correction 3: this event now genuinely
            // goes through the same run/version guard as every other
            // non-establishing event type — a DATA_STALE describing a run
            // that is no longer active (or an out-of-order redelivery)
            // must not be allowed to toggle `isStale` for the CURRENT
            // run's display.
            if (shouldRejectAsStaleOrInactive(event)) {
              break;
            }
            const payload = event.payload as DataStalePayload;
            if (typeof payload?.stale === 'boolean') {
              // P0-6: only the SERVER half of the combined flag is
              // touched here -- never the local-transport half, and
              // never derived from/tied to `lastMessageAtRef` (which was
              // the root cause: that timestamp is updated unconditionally
              // on every message, including this one, a few lines up in
              // `onmessage`). `recomputeStale` immediately updates both
              // the ref (synchronous, for this hook's own dispatch below)
              // and the React state (for render-time consumers).
              serverStaleRef.current = payload.stale;
              recomputeStale();
            }
            optionsRef.current.onDataStaleChange?.(payload);
            break;
          }
          default:
            break; // unknown event type — ignore rather than crash the socket
        }
      };

      const scheduleReconnect = () => {
        if (!isMountedRef.current) {
          return;
        }
        setConnectionState('closed');
        const delay = RECONNECT_DELAYS_MS[Math.min(attemptRef.current, RECONNECT_DELAYS_MS.length - 1)];
        attemptRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      };

      // AWS hackathon-final pass, P2 (Fire TV polish): a 4401 close means
      // the backend rejected this connection's credential outright (see
      // `onAuthFailure`'s own doc-comment above) — reconnecting with the
      // exact same token would just be rejected again, forever, at this
      // hook's normal backoff cadence. Stop reconnecting and hand off to
      // the caller instead of spinning silently.
      socket.onclose = (event: {code?: number}) => {
        if (event?.code === 4401 && tokenWasSentRef.current) {
          if (!isMountedRef.current) {
            return;
          }
          setConnectionState('closed');
          optionsRef.current.onAuthFailure?.();
          return;
        }
        scheduleReconnect();
      };
      socket.onerror = () => {
        socket.close();
      };
    };

    connect();

    const staleCheckId = setInterval(() => {
      if (!isMountedRef.current) {
        return;
      }
      // P0-6: this timer only ever writes `localTransportStaleRef` — it
      // must never be able to clear a server-declared `serverStaleRef`
      // (that's what let a DATA_STALE:true get silently overwritten
      // within one second, pre-fix). `recomputeStale` re-ORs both refs,
      // so a live server-stale flag keeps `isStale` true regardless of
      // what this tick computes for the local half.
      localTransportStaleRef.current = Date.now() - lastMessageAtRef.current > STALE_THRESHOLD_MS;
      recomputeStale();
    }, STALE_CHECK_INTERVAL_MS);

    return () => {
      isMountedRef.current = false;
      clearInterval(staleCheckId);
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (socketRef.current) {
        socketRef.current.onclose = null; // don't trigger a reconnect on our own unmount-close
        socketRef.current.close();
      }
    };
  }, [raceId]);

  return {connectionState, isStale};
}
