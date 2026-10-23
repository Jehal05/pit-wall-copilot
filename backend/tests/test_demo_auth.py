"""Tests for spec v1.1 "repair 11": no bundled/default demo-access-token,
fail-closed behavior when none is configured, and the explicit, ephemeral
local_demo_auth_mode.

These tests build their OWN app instances with a custom `Settings` object
(via monkeypatching `app.main.get_settings`) rather than using the shared
`api_client` fixture, because that fixture relies on `conftest.py` having
already put a real token in play (see conftest.py's own module docstring
on `TEST_DEMO_ACCESS_TOKEN`) — these tests specifically need to control
whether a token is configured at all.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


def _settings(**overrides) -> Settings:
    # bypass real env vars / the lru_cache entirely — a fresh Settings()
    # object built directly, exactly the values this test wants.
    return Settings(**overrides)


async def _make_client(monkeypatch, settings: Settings):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, lifespan_cm


async def test_mutating_endpoint_fails_closed_when_no_token_is_configured(monkeypatch):
    """Default Settings(): demo_access_token=None, local_demo_auth_mode=False.
    Every mutating request must be refused — even one with NO auth header
    at all, and even one that happens to send the string "None"."""
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=None, local_demo_auth_mode=False))
    try:
        response = await client.post("/v1/demo/start")
        assert response.status_code == 503
        assert response.json()["error"] == "auth_not_configured"

        response_with_literal_none = await client.post(
            "/v1/demo/start", headers={"X-Demo-Access-Token": "None"}
        )
        assert response_with_literal_none.status_code == 503
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_mutating_endpoint_refuses_a_wrong_token_distinctly_from_unconfigured(monkeypatch):
    """When a real token IS configured, a WRONG token is a 401
    (unauthorized), not a 503 (unconfigured) — the two failure modes are
    deliberately distinct so an operator can tell "nobody set this up" apart
    from "someone sent the wrong credential."""
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token="real-token-for-this-test"))
    try:
        response = await client.post("/v1/demo/start", headers={"X-Demo-Access-Token": "wrong-token"})
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

        response_ok = await client.post("/v1/demo/start", headers={"X-Demo-Access-Token": "real-token-for-this-test"})
        assert response_ok.status_code == 200
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_local_demo_auth_mode_generates_a_fresh_ephemeral_token(monkeypatch, capsys):
    """LOCAL_DEMO_AUTH_MODE=true with no explicit token: the backend
    generates one itself at startup (never a hardcoded value) and prints
    it so a local developer can use it — and mutating requests using that
    printed token then succeed."""
    client, lifespan_cm = await _make_client(monkeypatch, _settings(local_demo_auth_mode=True, demo_access_token=None))
    try:
        printed = capsys.readouterr().err
        assert "Generated a fresh local-only token" in printed

        # Extract the token from the printed banner. Rather than trying to
        # filter out every prose line by keyword (fragile — any new banner
        # wording breaks it), find the line that immediately follows the
        # "Generated a fresh local-only token for this process:" line: that
        # is structurally where main.py always places the token, indented.
        raw_lines = printed.splitlines()
        marker_index = next(
            i for i, line in enumerate(raw_lines) if "Generated a fresh local-only token" in line
        )
        generated_token = raw_lines[marker_index + 1].strip()
        assert generated_token
        assert not generated_token.startswith("=")

        response = await client.post("/v1/demo/start", headers={"X-Demo-Access-Token": generated_token})
        assert response.status_code == 200

        response_wrong = await client.post("/v1/demo/start", headers={"X-Demo-Access-Token": "not-it"})
        assert response_wrong.status_code == 401
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_auth_verify_endpoint_accepts_the_correct_token(monkeypatch):
    """Spec v1.1 correction 5: /v1/auth/verify is the Fire TV release
    pairing flow's server-side check — non-mutating, but enforced by the
    exact same `require_demo_access_token` dependency as every mutating
    endpoint, so "this token verifies" and "this token can start/pause/
    reset" are never two different claims."""
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token="pair-me-token"))
    try:
        response = await client.get("/v1/auth/verify", headers={"X-Demo-Access-Token": "pair-me-token"})
        assert response.status_code == 200
        assert response.json() == {"valid": True}
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_auth_verify_endpoint_rejects_a_wrong_token(monkeypatch):
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token="pair-me-token"))
    try:
        response = await client.get("/v1/auth/verify", headers={"X-Demo-Access-Token": "not-the-token"})
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_auth_verify_endpoint_rejects_a_missing_token(monkeypatch):
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token="pair-me-token"))
    try:
        response = await client.get("/v1/auth/verify")
        assert response.status_code == 401
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_auth_verify_endpoint_fails_closed_when_backend_has_no_token_configured(monkeypatch):
    client, lifespan_cm = await _make_client(
        monkeypatch, _settings(demo_access_token=None, local_demo_auth_mode=False)
    )
    try:
        response = await client.get("/v1/auth/verify", headers={"X-Demo-Access-Token": "anything"})
        assert response.status_code == 503
        assert response.json()["error"] == "auth_not_configured"
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_auth_verify_endpoint_never_mutates_demo_state(monkeypatch):
    """Calling /v1/auth/verify repeatedly must never start/pause/reset the
    demo as a side effect — it's a pure credential check."""
    client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token="pair-me-token"))
    try:
        headers = {"X-Demo-Access-Token": "pair-me-token"}
        for _ in range(3):
            response = await client.get("/v1/auth/verify", headers=headers)
            assert response.status_code == 200

        status_response = await client.get("/v1/demo/status")
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "STOPPED"
    finally:
        await client.aclose()
        await lifespan_cm.__aexit__(None, None, None)


async def test_local_demo_auth_mode_generates_a_different_token_each_process(monkeypatch):
    """The ephemeral token is not a fixed/predictable value — two separate
    app instances (simulating two separate backend process starts) get
    two different tokens."""
    client_a, lifespan_a = await _make_client(monkeypatch, _settings(local_demo_auth_mode=True, demo_access_token=None))
    client_b, lifespan_b = await _make_client(monkeypatch, _settings(local_demo_auth_mode=True, demo_access_token=None))
    try:
        token_a = client_a._transport.app.state.effective_demo_access_token  # noqa: SLF001 test introspection
        token_b = client_b._transport.app.state.effective_demo_access_token  # noqa: SLF001 test introspection
        assert token_a != token_b
        assert token_a is not None and token_b is not None
    finally:
        await client_a.aclose()
        await client_b.aclose()
        await lifespan_a.__aexit__(None, None, None)
        await lifespan_b.__aexit__(None, None, None)
