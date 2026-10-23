/**
 * Spec v1.1 repair 3: most component-level tests (AppFocus, ResetControl,
 * AlertsPanel, ...) render <App /> end to end, which now mounts
 * useRaceSocket and therefore constructs a real `WebSocket`. Node's
 * built-in WebSocket (undici) tries a REAL network connection in that
 * environment, which fails immediately and — worse — useRaceSocket's own
 * onerror->close handling recurses into onerror again on Node's
 * implementation, overflowing the stack (see the commit that added this
 * file for the exact trace). None of those tests are testing WebSocket
 * behavior itself — see useRaceSocket.test.ts for that — so this global
 * setup file replaces `WebSocket` with an inert stand-in for every test
 * by default: it can be constructed and closed, but never actually
 * connects or fires any event on its own. A test that DOES want to
 * exercise real socket message/reconnect behavior installs its own more
 * capable fake (see useRaceSocket.test.ts) which naturally overrides this
 * default within that file's module scope.
 */
class InertWebSocket {
  constructor() {
    this.readyState = 0; // CONNECTING — deliberately never advances on its own
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }

  close() {
    this.readyState = 3; // CLOSED
    // Deliberately does NOT invoke onclose/onerror — a real close would,
    // but doing so here is exactly what caused the recursive-onerror
    // stack overflow this file exists to prevent, and no test relying on
    // this default inert socket needs that callback to fire.
  }

  send() {}
}

global.WebSocket = InertWebSocket;
