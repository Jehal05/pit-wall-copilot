# ADR 0002: WebSocket Authentication via `?token=` Query Parameter

Status: **Accepted**
Date: 2026-09-23
Related: P2-12 (AWS hackathon-final pass), `backend/app/api/routes.py`'s
`race_websocket`, `backend/app/api/deps.py`'s `credential_is_valid`,
`fire-tv/src/hooks/useRaceSocket.ts`'s `toWebSocketUrl`,
`backend/tests/test_websocket_events.py::TestWebSocketAuthentication`

## Context

Every mutating REST endpoint, and every endpoint that reads
race/strategy/watch/alert data, requires the `X-Demo-Access-Token` header
(`app/api/deps.py`'s `require_demo_access_token`) — either the master
token or a live, paired device token. `/v1/races/{race_id}/ws`, the
WebSocket route that streams that same data (`RACE_STATE`,
`STRATEGY_UPDATE`, `WATCH_ALERT`, `AGENT_RESPONSE`) in real time, had **no
credential check at all**. Anyone who could reach the route's URL — no
token, no pairing, nothing — received the exact same race and strategy
data the rest of the API is deliberately gated behind. This was found
during a general timeout/concurrency review of the API surface (P2-12),
not reported by an external party, and is fixed in this same pass.

A regular FastAPI `Depends()` HTTP dependency cannot be attached to a
`@router.websocket(...)` handler the way it can to an HTTP route — a
WebSocket connection is a distinct ASGI scope, not a `Request` — so
`require_demo_access_token` itself could not simply be reused unmodified;
some new mechanism was needed either way.

The harder constraint: a browser's (and React Native's) native
`WebSocket` constructor **cannot set a custom header** on the handshake
request. `X-Demo-Access-Token` was therefore not an option for this route
regardless of the dependency-injection question above.

## Decision

1. Factor the credential-checking logic out of `require_demo_access_token`
   into a standalone function, `credential_is_valid(app_state, token)`
   (`app/api/deps.py`), that both the HTTP dependency and the WebSocket
   route call — one rule, enforced identically in both places, rather
   than two independently-maintained copies that could silently drift.
2. `race_websocket` accepts an optional `token: str | None = None` query
   parameter (FastAPI auto-parses a plain function parameter on a
   WebSocket route from the query string, the same way it would on an
   HTTP route). If `credential_is_valid` rejects it, the connection is
   closed with code **4401** (an application-reserved close code,
   deliberately echoing HTTP 401) **before** `broadcaster.connect` is
   ever called — meaning before `websocket.accept()` — so an
   unauthenticated caller never receives even the initial `RACE_STATE`
   snapshot.
3. `fire-tv/src/hooks/useRaceSocket.ts`'s `toWebSocketUrl` now appends
   `?token=<resolveAuthToken()>` (the exact credential `api/client.ts`
   already attaches to every HTTP request), `encodeURIComponent`-escaped.
   `resolveAuthToken` was exported from `api/client.ts` for this purpose.
4. A rejected/missing-token connection audit-logs an
   `authentication_failed` event (`transport: "websocket"`), matching the
   HTTP dependency's own audit trail for the same failure class.

### Why a query parameter, not a subprotocol or a first-message handshake

- **Subprotocol** (`Sec-WebSocket-Protocol`): technically settable from
  JS, but overloading a protocol-negotiation header to carry a bearer
  credential is a well-known anti-pattern (it's visible in the same
  places a query string is — server access logs, proxies — with none of
  a query string's simplicity, and some WebSocket client libraries
  constrain its charset).
- **First-message handshake** (accept the connection, require an auth
  message as the first frame, close if it's missing/wrong): this would
  mean `websocket.accept()` runs, and a brief window exists where an
  unauthenticated socket is open, before this route's own logic gets a
  chance to reject it — a strictly weaker guarantee than rejecting before
  `accept()` ever runs. It would also require re-implementing a
  mini-protocol on both ends for no benefit over a query parameter this
  route already had cheap, standard support for.
- **Query parameter**: works unmodified with every WebSocket client
  (browser, React Native, `httpx`/Starlette's own `TestClient` used in
  this repo's own tests), requires no new protocol, and rejects before
  `accept()`.

### Known trade-off, accepted

A token in a URL query string can end up in server access logs, browser
history (irrelevant here — this is a WebSocket URL, never navigated to),
or an intermediate proxy's logs, in a way a header is somewhat less
exposed to by convention. This is judged an acceptable trade-off for this
application specifically: the ALB does not log WebSocket upgrade request
query strings by default in this template (no S3 access-log target is
configured to capture full URLs at the WAF/ALB layer in this template),
the token is a short-lived-in-practice demo/pairing credential (not a
long-lived production API key), and it is exactly the same credential
already sent — just as visibly, from an intercepting-proxy point of
view — as a header value on the equivalent HTTP requests this same client
makes moments earlier and later on the same connection/session. A future,
longer-lived production deployment of this pattern should consider a
short-lived, single-use WebSocket-specific token minted by an HTTP
endpoint immediately before connecting, rather than reusing the
long-lived device/master token directly — noted here rather than
implemented, since it is not required to close the actual gap this ADR
addresses (an unauthenticated caller reading live race data).

## Consequences

- Every existing WebSocket test in `test_websocket_events.py` now
  connects with `?token={TEST_DEMO_ACCESS_TOKEN}`; three new tests
  (`TestWebSocketAuthentication`) assert a missing or wrong token is
  rejected with close code 4401 and never receives race data.
- `fire-tv`'s `useRaceSocket.test.ts` gained three tests
  (`toWebSocketUrl auth token propagation`) covering: a token present →
  appended, a token with URL-unsafe characters → percent-encoded, no
  token yet (e.g. connecting before pairing completes) → the connection
  is refused by the backend exactly like an unauthenticated HTTP request
  would be, not a new failure mode.
- A Fire TV device that has not yet paired (no runtime auth token set)
  will have its WebSocket connection refused with 4401 until pairing
  completes — this is the intended, secure-by-default behavior, not a
  regression: such a device could not have made any mutating REST call
  either, before this pass.
