# ADR 0003: Request-Body Size Limiting as a Plain ASGI Middleware

Status: **Accepted**
Date: 2026-09-23
Related: P2-12 (AWS hackathon-final pass), `backend/app/security_middleware.py`,
`backend/tests/test_security_middleware.py`

## Context

Before this pass, nothing in the backend bounded the size of an inbound
request body. Every real endpoint accepts a small, bounded JSON payload
(a watch definition, a pairing code, a device name) — none is a file
upload — so an unbounded body was pure unnecessary exposure to a trivial
memory-exhaustion request (a client streaming an arbitrarily large body
at a mutating endpoint), not a feature anything in this codebase actually
depends on.

`app/main.py` already uses `starlette.middleware.base.BaseHTTPMiddleware`
for two other concerns (`CorrelationIdMiddleware`, and this same pass's
new `SecurityHeadersMiddleware`), so that was the first approach tried
here too, by reassigning `request._receive` inside `dispatch()` to a
wrapper that raised once a running byte count crossed the limit.

## Problem found

That first attempt did not work. When the wrapped `receive()` raised its
limit-exceeded exception, the exception did not reach `dispatch()`'s own
`try/except` at all — it escaped as an **unhandled `ExceptionGroup`** from
deep inside Starlette's internal `receive_or_disconnect`/streaming
machinery, producing a 500 with no useful body instead of the intended
413. This was found by actually running the middleware against a real
oversized request in this pass's own test suite, not assumed from
documentation. `BaseHTTPMiddleware` funnels the downstream app's body
reads through its own internal buffering/background-task machinery, which
does not reliably observe a `request._receive` reassigned from within
`dispatch()` for this kind of exception-during-downstream-read case.

## Decision

Implement `RequestBodySizeLimitMiddleware` as a **plain ASGI middleware**
instead — a raw class with `__init__(self, app, *, max_bytes)` and
`async def __call__(self, scope, receive, send)`, added via
`app.add_middleware(RequestBodySizeLimitMiddleware, max_bytes=...)`
exactly like any Starlette middleware, but with none of
`BaseHTTPMiddleware`'s internal request/response wrapping. It:

1. Checks `Content-Length`, if present, **before any body is read** — the
   common, cheap case, catching a request that is honest about being too
   large without this service ever allocating a buffer for it.
2. If `Content-Length` is absent (a chunked-transfer-encoded body can
   omit it) or a client sends more bytes than it claimed, wraps `receive`
   itself at the raw ASGI level, with a running counter, so a request
   that lies about its size is still caught mid-stream — before
   Starlette's own `Request`/routing machinery ever sees the body.

Wrapping `receive` at this layer, ahead of `BaseHTTPMiddleware`-based
middleware and Starlette's routing, means every layer above this one
(`SecurityHeadersMiddleware`, `CorrelationIdMiddleware`, every route
handler) receives the SAME wrapped `receive` and is unaffected by how it
works — this is the general lesson, not specific to size-limiting: a
raw ASGI middleware is the correct layer for anything that must reliably
intercept or transform the request body stream itself, while
`BaseHTTPMiddleware` remains a fine, simpler choice for anything that
only needs to run logic before/after the handler without touching the
body stream directly (as `SecurityHeadersMiddleware`, which only adds
response headers, does).

## Consequences

- `test_security_middleware.py::TestRequestBodySizeLimitMiddleware`
  covers all three paths: a body under the limit is accepted; a body over
  a dishonest `Content-Length` is rejected with 413 before being read;
  and a body streamed without an honest `Content-Length` is still caught
  mid-stream. The third test uses a minimal standalone Starlette app with
  a handler that actually reads the full body, rather than a real
  `/v1/auth/pairing-codes` route — that real route's auth dependency
  rejects with 401 before ever reading the body, so the size-limiting
  wrapper is never exercised through it unless something downstream
  actually reads the stream.
- `max_request_body_bytes` (default 1,000,000) is a new `Settings` field,
  overridable per deployment.
- Middleware ordering in `app/main.py`: `CorrelationIdMiddleware` →
  `SecurityHeadersMiddleware` → `CORSMiddleware` →
  `RequestBodySizeLimitMiddleware`. Starlette executes added middleware in
  LIFO order on the way OUT, so `CorrelationIdMiddleware` (added first) is
  outermost and its header lands on every response, including ones the
  inner layers short-circuit; `RequestBodySizeLimitMiddleware` (added
  last) is innermost, wrapping the actual route handlers most closely so
  an oversized body is rejected before any application code ever sees it.
