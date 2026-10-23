import {
  AgentQueryResult,
  Alert,
  ApiErrorBody,
  DemoStatus,
  ExplanationResult,
  RaceChangesResult,
  RaceState,
  RegulationAnswer,
  StrategySimulationResult,
  UndercutResult,
  Watch,
  WatchType,
} from './types';
import {LOCAL_DEV_AUTH_TOKEN} from '../config/authToken';
import {getRuntimeAuthToken} from '../config/runtimeAuthToken';
// '@env' is a virtual module resolved by the react-native-dotenv Babel
// plugin (babel.config.js) at bundle time, not a real package on disk; see
// env.d.ts for its TypeScript type declaration.
import {API_BASE_URL as ENV_API_BASE_URL} from '@env';

/**
 * Configurable API base URL.
 *
 * Spec v1.1 repair 13: "configurable API/WS base URLs (no manual client.ts
 * edits)". The value comes from `.env` (see `.env.example` for the
 * documented keys per target — Android emulator, physical device/Fire TV,
 * iOS simulator) via the react-native-dotenv Babel plugin, so switching
 * targets means editing `.env` and restarting Metro, never this file. The
 * literal below is only a fallback for the case where `.env` doesn't
 * define the key at all (e.g. a fresh checkout before `.env` is created).
 *
 * The WebSocket base URL (src/hooks/useRaceSocket.ts) is derived from this
 * same value, so there is nothing further to configure for sockets.
 */
export const API_BASE_URL: string = ENV_API_BASE_URL || 'http://10.0.2.2:8000';

/**
 * Thrown when a RELEASE build is configured with a plaintext `http://`
 * API base URL. Spec v1.1 pass 3, correction 8: "a release build
 * configured with http:// must fail configuration/build validation or
 * refuse to send credentials." `__DEV__` is statically `false` in a
 * release bundle (see `resolveAuthToken()`'s own doc-comment for the same
 * mechanism), so this check cannot be bypassed by an env value alone —
 * only a genuine dev/debug bundle is ever allowed to talk to `http://`.
 */
export class InsecureReleaseConfigurationError extends Error {
  constructor(baseUrl: string) {
    super(
      'This is a release build, but the configured API base URL ' +
        `("${baseUrl}") is not https://. Release builds must never send ` +
        'requests — and especially never send pairing/device credentials ' +
        '— over plaintext HTTP. Set API_BASE_URL in .env to an https:// ' +
        'URL before building a release, or use a debug build for ' +
        'plaintext local development.',
    );
  }
}

/**
 * Spec v1.1 pass 3, correction 8. Called before every network request
 * this client makes (both `request()` and `verifyAuthToken()`/the
 * device-pairing endpoints), and by `useRaceSocket.ts`'s WebSocket URL
 * derivation. A debug/dev bundle may use `http://`/`ws://` (e.g. the
 * Android emulator's `10.0.2.2` host has no TLS listener); a release
 * bundle never may — this is the one place that rule is enforced, so it
 * cannot be bypassed by adding a new call site that forgets to check.
 */
export function assertSecureTransportForRelease(url: string): void {
  const isDev = typeof __DEV__ !== 'undefined' && __DEV__;
  if (isDev) {
    return;
  }
  if (!url.startsWith('https://') && !url.startsWith('wss://')) {
    throw new InsecureReleaseConfigurationError(url);
  }
}

/**
 * Spec v1.1 pass 4, mandatory correction 5: "Ensure every REST and
 * WebSocket operation invokes the central release transport validation
 * ... Prefer a single secure request wrapper so future call sites cannot
 * bypass validation." Before this pass, `request()`, `verifyAuthToken()`
 * and `pairDevice()` each called `assertSecureTransportForRelease`
 * individually (three separate call sites that could drift), and
 * `revokeOwnDeviceToken()` — added later, in the same pass as the other
 * three — was simply MISSED entirely, meaning a release build's "unpair"
 * action could have sent a plaintext request carrying a real device
 * token, silently, forever, since nothing exercised that path in a
 * release configuration to notice. `secureFetch` is now the ONLY place
 * any of this module ever calls the real `fetch` — every REST call in
 * this file (auth or not, GET or POST, best-effort or not) goes through
 * it, so a future call site cannot forget the check by construction:
 * there is no other way to reach the network from here. `useRaceSocket.ts`
 * calls `assertSecureTransportForRelease` directly for the one non-REST
 * case (the WebSocket URL itself), since a `WebSocket` is not a `fetch`
 * call and cannot be routed through this wrapper.
 */
async function secureFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${API_BASE_URL}${path}`;
  assertSecureTransportForRelease(url);
  return fetch(url, init);
}

/** Shared response-body parsing for every call site below — a non-JSON or
 * empty body (e.g. a plain 204/500 with no payload) is not treated as a
 * parse failure that crashes the caller; it resolves to `undefined`. */
async function parseJsonBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return undefined;
  }
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

export class ApiError extends Error {
  status: number;
  body?: ApiErrorBody;

  constructor(status: number, body?: ApiErrorBody) {
    super(body?.message ?? `Request failed with status ${status}`);
    this.status = status;
    this.body = body;
  }
}

/**
 * Thrown client-side, before any network request is made, when a mutating
 * call is attempted with no auth token resolvable at all. This is the
 * Fire TV client's own half of spec v1.1 repair 11's "release builds
 * SHALL refuse mutation requests when secure authorization is not
 * configured" — the backend already fails closed (HTTP 503
 * auth_not_configured) if it receives such a request, but refusing it
 * here means a misconfigured release build never even sends the request,
 * and the resulting UI-level error is unambiguous rather than a generic
 * network failure.
 */
export class AuthNotConfiguredError extends Error {
  constructor() {
    super(
      'No demo access token is configured for this device yet. Mutating ' +
        'actions (Start/Pause/Reset/Watch) are disabled until this Fire TV ' +
        'is paired with a backend. Use the pairing screen to enter a token ' +
        '(spec v1.1 correction 5) — or, for local development only, set ' +
        'LOCAL_DEV_AUTH_TOKEN in src/config/authToken.ts.',
    );
  }
}

/**
 * Thrown when a mutating request is refused with 401 by a token that WAS
 * configured (i.e. `resolveAuthToken()` returned a non-null value) — as
 * opposed to `AuthNotConfiguredError`, which is thrown client-side before
 * any request when no token is configured at all. A 401 on an
 * already-paired device means the backend no longer accepts this
 * specific token (it was rotated/revoked server-side, or the device was
 * paired against a different backend instance) — spec v1.1 correction 5:
 * "expired/invalid auth must produce a clear Fire TV error and return to
 * pairing/auth." `useDeviceAuth.ts` catches this specifically and clears
 * the stored device token so the app returns to the pairing screen rather
 * than silently retrying a credential the backend has already rejected.
 */
export class AuthExpiredError extends Error {
  constructor() {
    super('This device’s pairing is no longer valid. Please pair this Fire TV again.');
  }
}

/**
 * Resolves the token to send as X-Demo-Access-Token, if any.
 *
 * Spec v1.1 repair 11 / correction 5: there is no bundled/hardcoded
 * credential anywhere in this file (or anywhere else in the JS source,
 * Gradle files, tests, or docs — see src/config/authToken.ts's own
 * docstring). Two possible sources exist, checked in this order:
 *
 *   1. `LOCAL_DEV_AUTH_TOKEN` — a local-only override a developer pastes
 *      into their own uncommitted working copy of
 *      `src/config/authToken.ts` — but ONLY ever consulted in a `__DEV__`
 *      bundle. `__DEV__` is a React-Native/Metro global that is
 *      statically `false` in a release bundle, so a release build never
 *      reads this constant regardless of what (if anything) ends up left
 *      in authToken.ts — there is no runtime "forgot to strip it" failure
 *      mode, and this is the "release builds must never silently fall
 *      back to local demo credentials" guarantee.
 *   2. The runtime device-pairing token (`config/runtimeAuthToken.ts`) —
 *      populated at app startup from Android-Keystore-backed secure
 *      storage (`config/secureTokenStore.ts`) if this device was
 *      previously paired, or immediately after a fresh pairing
 *      (`useDeviceAuth.ts`). This is the ONLY source consulted in a
 *      release build, and is also consulted in a dev build that has no
 *      `LOCAL_DEV_AUTH_TOKEN` set (so the pairing flow itself remains
 *      testable in a dev build, not just a release one).
 */
// AWS hackathon-final pass, P2-12: exported so useRaceSocket.ts can attach
// the SAME credential to the WebSocket handshake that every HTTP request
// already attaches as the `X-Demo-Access-Token` header (see
// resolveAuthToken's own doc-comment above for the exact precedence rule
// this follows). Before this pass, `/v1/races/{race_id}/ws` had no
// credential check on the backend at all; now that it does (see
// backend/app/api/routes.py's `race_websocket`), a client that doesn't
// send a token gets the connection closed with code 4401 instead of
// silently receiving no data.
export function resolveAuthToken(): string | null {
  if (typeof __DEV__ !== 'undefined' && __DEV__ && LOCAL_DEV_AUTH_TOKEN) {
    return LOCAL_DEV_AUTH_TOKEN;
  }
  return getRuntimeAuthToken();
}

async function request<T>(
  path: string,
  options: {method?: 'GET' | 'POST'; auth?: boolean; body?: unknown} = {},
): Promise<T> {
  const {method = 'GET', auth = false, body: requestBody} = options;
  const headers: Record<string, string> = {'Content-Type': 'application/json'};
  if (auth) {
    const token = resolveAuthToken();
    if (!token) {
      throw new AuthNotConfiguredError();
    }
    headers['X-Demo-Access-Token'] = token;
  }

  const response = await secureFetch(path, {
    method,
    headers,
    ...(requestBody !== undefined ? {body: JSON.stringify(requestBody)} : {}),
  });

  const body = await parseJsonBody(response);

  if (!response.ok) {
    // A 401 on a request this client itself attached a (non-null) token
    // to means the backend rejected THAT SPECIFIC token, not "no token
    // was sent" — distinct from AuthNotConfiguredError above, and the
    // signal useDeviceAuth.ts uses to clear the stored pairing and return
    // to the pairing screen (spec v1.1 correction 5).
    if (auth && response.status === 401) {
      throw new AuthExpiredError();
    }
    throw new ApiError(response.status, body as ApiErrorBody | undefined);
  }
  return body as T;
}

/**
 * Validates a CANDIDATE token against the backend's `/v1/auth/verify`
 * (spec v1.1 correction 5) — deliberately independent of `request()`'s
 * `resolveAuthToken()`/runtime-token machinery above, because pairing
 * necessarily happens BEFORE a candidate token is trusted enough to be
 * saved as "the" device token: `useDeviceAuth.pair()` calls this with
 * whatever the person just typed on the pairing screen, and only calls
 * `secureTokenStore.saveDeviceToken()` (and
 * `runtimeAuthToken.setRuntimeAuthToken()`) once this resolves
 * successfully. Returns normally (void) on a valid token; throws
 * `ApiError` (401 for a wrong token, 503 if the backend has no token
 * configured at all) otherwise — `useDeviceAuth.ts` translates both into
 * a human-readable pairing-screen error.
 */
export async function verifyAuthToken(candidateToken: string): Promise<void> {
  const response = await secureFetch('/v1/auth/verify', {
    method: 'GET',
    headers: {'X-Demo-Access-Token': candidateToken},
  });
  const body = await parseJsonBody(response);
  if (!response.ok) {
    throw new ApiError(response.status, body as ApiErrorBody | undefined);
  }
}

export interface PairDeviceResult {
  device_id: string;
  device_token: string;
  expires_in_seconds: number;
}

/**
 * Spec v1.1 pass 3, correction 5: exchanges a SHORT, short-lived, one-time
 * pairing code (never the master backend secret — see
 * `POST /v1/auth/pairing-codes`, called by an administrator, not by this
 * client) for a brand-new, device-specific token. `useDeviceAuth.pair()`
 * calls this with whatever short code a person just typed on the pairing
 * screen, and stores the returned `device_token` (never the code itself,
 * which is single-use and would be useless to store) in Android-Keystore-
 * backed secure storage on success.
 */
export async function pairDevice(code: string, deviceName: string): Promise<PairDeviceResult> {
  const response = await secureFetch('/v1/auth/pair', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, device_name: deviceName}),
  });
  const body = await parseJsonBody(response);
  if (!response.ok) {
    throw new ApiError(response.status, body as ApiErrorBody | undefined);
  }
  return body as PairDeviceResult;
}

/**
 * Spec v1.1 pass 3, correction 5: self-revokes THIS device's own token —
 * used by the "Unpair this device" action. Sends the device's own token
 * (never the master secret, which this client never holds) and no
 * `device_id` — the backend infers which device from the token itself
 * (see routes.py's `revoke_device` self-revoke path). Best-effort: a
 * network failure here must not block the local unpair — the caller
 * clears local secure storage regardless (see useDeviceAuth.signOut()).
 *
 * Spec v1.1 pass 4, mandatory correction 5 FIX: this function previously
 * called the raw `fetch()` directly, bypassing
 * `assertSecureTransportForRelease` entirely — the one call site in this
 * whole module that skipped the release-transport guard, meaning a
 * misconfigured release build's "unpair" action could have sent this
 * device's own live token over plaintext HTTP. It now goes through
 * `secureFetch`, the SAME wrapper every other call in this file uses, so
 * that gap cannot recur at a new call site either. The `try/catch` below
 * still exists for the ORIGINAL reason (a genuine network failure must
 * not block the local unpair) — but it does not defeat the guard: if
 * `secureFetch` throws `InsecureReleaseConfigurationError`, that throw
 * happens BEFORE any `fetch()` call is made, so nothing is ever sent
 * over plaintext either way; catching it here only decides whether
 * `signOut()` (the caller) proceeds to clear local storage regardless
 * (spec requirement: "the unpair flow still clears the local credential
 * even if server revocation fails or is blocked") — it does not permit
 * the network call itself to proceed insecurely.
 */
export async function revokeOwnDeviceToken(deviceToken: string): Promise<void> {
  try {
    await secureFetch('/v1/auth/revoke', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Demo-Access-Token': deviceToken},
      body: JSON.stringify({}),
    });
  } catch {
    // Best-effort — see doc-comment above. The local unpair proceeds
    // regardless of whether this server-side revoke request succeeded.
  }
}

export const api = {
  health: () => request<{status: string; app_version: string; timestamp: string}>('/health'),

  getRaceState: (raceId: string) => request<RaceState>(`/v1/races/${raceId}/state`),

  startDemo: () => request<DemoStatus>('/v1/demo/start', {method: 'POST', auth: true}),

  pauseDemo: () => request<DemoStatus>('/v1/demo/pause', {method: 'POST', auth: true}),

  resetDemo: () => request<RaceState>('/v1/demo/reset', {method: 'POST', auth: true}),

  getDemoStatus: () => request<DemoStatus>('/v1/demo/status'),

  // Newest-first, per GET /v1/races/{race_id}/alerts's own docstring
  // ("Fire TV polls this to surface proactive alerts").
  listAlerts: (raceId: string) => request<Alert[]>(`/v1/races/${raceId}/alerts`),

  // ---------------------------------------------------------------------
  // Spec v1.1 repair 2: P0 screens beyond the Gate 0 dashboard. Each of
  // these backs one of the D-pad primary actions in section 11 (Why?,
  // Simulate, Watch, What Changed?, Sources) via the same REST endpoints
  // the 5 MCP domain tools mirror (mcp_server/server.py) — a Fire TV
  // button press and an Alexa+ voice request reach the identical backend
  // logic, never a separate client-side computation.
  // ---------------------------------------------------------------------

  /** SIMULATE / STRATEGY BATTLE panel: calculate_undercut, the "PIT NOW"
   * baseline (candidate pits now, target responds next lap). */
  calculateUndercut: (raceId: string, candidateDriver: string, targetDriver: string) =>
    request<UndercutResult>(`/v1/races/${raceId}/strategy/undercut`, {
      method: 'POST',
      body: {candidate_driver: candidateDriver, target_driver: targetDriver},
    }),

  /** SIMULATE screen: simulate_strategy, the "PIT NOW vs WAIT N LAPS"
   * counterfactual (UJ-3) — `delta_ms` on the result is the net margin. */
  simulateStrategy: (raceId: string, driverId: string, waitLaps?: number) =>
    request<StrategySimulationResult>(`/v1/races/${raceId}/strategy/simulate`, {
      method: 'POST',
      body: {driver_id: driverId, ...(waitLaps !== undefined ? {wait_laps: waitLaps} : {})},
    }),

  /** WATCH action: create_strategy_watch. Mutating, so it requires the
   * demo access token like start/pause/reset. */
  createWatch: (
    raceId: string,
    candidateDriver: string,
    targetDriver: string,
    watchType: WatchType = 'UNDERCUT',
  ) =>
    request<Watch>(`/v1/races/${raceId}/watches`, {
      method: 'POST',
      auth: true,
      body: {candidate_driver: candidateDriver, target_driver: targetDriver, watch_type: watchType},
    }),

  /** WHY? / Agent Answer screen: explain_alert, grounded entirely in the
   * alert's own immutable snapshot (UJ-2). Rate-limited server-side. */
  explainAlert: (raceId: string, alertId: string) =>
    request<ExplanationResult>(`/v1/races/${raceId}/alerts/${alertId}/explain`),

  /** SOURCES / Evidence screen: get_regulation, the found/evidence
   * contract (UJ-4/UJ-5) — never fabricates a rule (see
   * regulation_service.py). `available=false` is the Section 29 "Knowledge
   * Base unavailable" degraded signal, distinct from an ordinary
   * found=false. */
  getRegulation: (query: string) =>
    request<RegulationAnswer>(`/v1/regulations?query=${encodeURIComponent(query)}`),

  /** WHY? fallback (no alert available yet) / general free-text question:
   * POST /v1/agent/query. Read-only, no auth required. */
  queryAgent: (raceId: string, query: string) =>
    request<AgentQueryResult>('/v1/agent/query', {method: 'POST', body: {race_id: raceId, query}}),

  /** WHAT CHANGED? screen: pre-computed, ranked deltas since the last
   * seen state (UJ-6) — never "hand two states to the LLM". */
  getRaceChanges: (raceId: string) => request<RaceChangesResult>(`/v1/races/${raceId}/changes`),
};
