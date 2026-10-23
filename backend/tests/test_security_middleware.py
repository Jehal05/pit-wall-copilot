"""Tests for app/security_middleware.py (AWS hackathon-final pass,
P2-12: "API/security hardening sweep") — response security headers and
request-body size limiting, neither of which existed before this pass.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.unit


async def _client(settings: Settings | None = None, monkeypatch=None) -> AsyncClient:  # noqa: ANN001
    if settings is not None and monkeypatch is not None:
        monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestSecurityHeadersMiddleware:
    @pytest.mark.asyncio
    async def test_every_response_carries_the_standard_security_header_set(self):
        client = await _client()
        response = await client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "max-age" in response.headers["Strict-Transport-Security"]
        assert response.headers["Content-Security-Policy"] == "default-src 'none'"

    @pytest.mark.asyncio
    async def test_security_headers_are_present_even_on_an_error_response(self):
        client = await _client()
        response = await client.get("/v1/races/does-not-exist/state")
        assert response.status_code in (404, 422)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"


class TestRequestBodySizeLimitMiddleware:
    @pytest.mark.asyncio
    async def test_a_body_under_the_limit_is_accepted(self, monkeypatch):
        settings = Settings(max_request_body_bytes=1000, _env_file=None)
        client = await _client(settings, monkeypatch)
        # /v1/auth/pairing-codes requires auth and will 401/403 rather
        # than 413 for a small body -- proves the size check did NOT
        # trip (a 413 would mean the limit rejected a small, legitimate
        # body, which is the failure this test guards against).
        response = await client.post("/v1/auth/pairing-codes", json={"tiny": "body"})
        assert response.status_code != 413

    @pytest.mark.asyncio
    async def test_a_body_over_the_declared_content_length_limit_is_rejected_with_413(self, monkeypatch):
        settings = Settings(max_request_body_bytes=10, _env_file=None)
        client = await _client(settings, monkeypatch)
        oversized_body = b"x" * 1000
        response = await client.post(
            "/v1/auth/pairing-codes",
            content=oversized_body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"

    @pytest.mark.asyncio
    async def test_a_body_streamed_without_an_honest_content_length_is_still_caught(self):
        # Exercised against a minimal standalone Starlette app (rather
        # than the real /v1/auth/pairing-codes route) because that real
        # route's auth dependency rejects with 401 BEFORE ever reading
        # the request body — correct, real behavior (an unauthenticated
        # caller's body is never even parsed), but it means the body-size
        # check specifically never gets exercised through that route
        # unless something downstream actually reads the stream. This
        # test isolates RequestBodySizeLimitMiddleware itself against a
        # trivial handler that DOES read the full body, proving the
        # streamed/no-honest-Content-Length path is caught on its own
        # merits.
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from app.security_middleware import RequestBodySizeLimitMiddleware

        async def _echo(request):  # noqa: ANN001
            body = await request.body()
            return PlainTextResponse(f"received {len(body)} bytes")

        app = Starlette(routes=[Route("/echo", _echo, methods=["POST"])])
        app.add_middleware(RequestBodySizeLimitMiddleware, max_bytes=10)
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")

        async def _oversized_stream():
            for _ in range(5):
                yield b"x" * 100

        response = await client.post("/echo", content=_oversized_stream())
        assert response.status_code == 413
