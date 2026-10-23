"""Bearer-auth coverage for the internet-facing MCP deployment — spec v1.1
correction 3: "add end-to-end tests for: no auth header, wrong bearer
token, correct bearer token, tool listing after auth, tool invocation
after auth, health/readiness without leaking secrets."

Like test_streamable_http_transport.py, this serves the real
`build_streamable_http_app(...)` over a real TCP port and drives it with
plain `httpx` (for the raw-HTTP-level auth failure cases, where we need to
see actual status codes/headers rather than an MCP SDK exception) and the
official MCP Streamable HTTP client (for the authenticated success path,
proving a real client can complete initialize -> tools/list -> tools/call
once it presents the correct token).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading

import httpx
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server import build_streamable_http_app, create_mcp_server

REQUIRED_TOKEN = secrets.token_urlsafe(24)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def running_authed_mcp_server(reset_race, monkeypatch):
    """Same shape as test_streamable_http_transport.py's
    `running_mcp_server`, except PIT_WALL_MCP_ACCESS_TOKEN is set BEFORE
    `build_streamable_http_app` is called, so `_AuthMiddleware` is
    actually active (not the documented local-dev no-op) — this is what
    `infrastructure/template.yaml` always configures for the deployed ECS
    task (see McpTaskDefinition's `Secrets`, correction 3)."""
    from tests.conftest import TEST_DEMO_ACCESS_TOKEN

    monkeypatch.setenv("PIT_WALL_MCP_ACCESS_TOKEN", REQUIRED_TOKEN)

    backend_client = httpx.AsyncClient(base_url=reset_race, timeout=10.0)
    mcp = create_mcp_server(client=backend_client, base_url=reset_race, demo_access_token=TEST_DEMO_ACCESS_TOKEN)
    app = build_streamable_http_app(mcp, backend_base_url=reset_race)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = asyncio.get_event_loop().time() + 10
    while not server.started and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn server did not start within timeout"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        try:
            await backend_client.aclose()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------
# Unauthenticated / malformed requests are rejected with 401 before ever
# reaching the MCP protocol layer (no tool is invoked, no backend call is
# made).
# ---------------------------------------------------------------------
async def test_missing_authorization_header_is_rejected(running_authed_mcp_server):
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert REQUIRED_TOKEN not in response.text


async def test_malformed_authorization_header_is_rejected(running_authed_mcp_server):
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        # Missing the "Bearer " scheme prefix entirely.
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": REQUIRED_TOKEN,
            },
        )
        assert response.status_code == 401
        assert REQUIRED_TOKEN not in response.text


async def test_wrong_bearer_token_is_rejected(running_authed_mcp_server):
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": "Bearer definitely-the-wrong-token",
            },
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert REQUIRED_TOKEN not in response.text


async def test_bearer_scheme_with_no_token_is_rejected(running_authed_mcp_server):
    # "Bearer" with nothing after it at all (not even a space) — a real
    # HTTP client (httpx included) refuses to send a header value with
    # trailing whitespace, so "Bearer " followed by an empty token isn't a
    # constructible wire request; this is the nearest real equivalent and
    # still exercises presented_token == "" once past the prefix check for
    # a value that DOES start with "Bearer" but has no separator/token.
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": "Bearer",
            },
        )
        assert response.status_code == 401
        assert REQUIRED_TOKEN not in response.text


# ---------------------------------------------------------------------
# Health/readiness stay reachable with no credentials at all (required so
# an ALB health check can probe this service), and never echo the
# configured token.
# ---------------------------------------------------------------------
async def test_health_reachable_without_auth_and_does_not_leak_token(running_authed_mcp_server):
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert REQUIRED_TOKEN not in response.text


async def test_ready_reachable_without_auth_and_does_not_leak_token(running_authed_mcp_server):
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        response = await client.get("/ready")
        assert response.status_code == 200
        assert REQUIRED_TOKEN not in response.text


# ---------------------------------------------------------------------
# The correct token allows a real client through the full protocol
# surface: initialize, tools/list, and tools/call.
# ---------------------------------------------------------------------
async def test_correct_bearer_token_allows_initialize_list_and_call(running_authed_mcp_server):
    authed_http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {REQUIRED_TOKEN}"})
    async with streamable_http_client(
        f"{running_authed_mcp_server}/mcp", http_client=authed_http_client
    ) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            assert init_result.protocolVersion

            tools_result = await session.list_tools()
            names = {t.name for t in tools_result.tools}
            assert names == {
                "calculate_undercut",
                "create_strategy_watch",
                "get_race_state",
                "get_regulation",
                "simulate_strategy",
            }

            call_result = await session.call_tool("get_race_state", {"race_id": "demo-race-001"})
            assert call_result.isError is not True
            state = json.loads(call_result.content[0].text)
            assert state["lap"] == 24


async def test_unauthorized_client_cannot_list_or_invoke_tools(running_authed_mcp_server):
    """A client that never gets past the 401 must not be able to reach
    tools/list or tools/call at all — asserted directly at the transport
    level, since an MCP ClientSession would simply fail its own
    initialize() handshake against a 401 and never offer a way to call
    list_tools()/call_tool() in the first place."""
    async with httpx.AsyncClient(base_url=running_authed_mcp_server, timeout=5.0) as client:
        list_response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        assert list_response.status_code == 401

        call_response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_race_state", "arguments": {"race_id": "demo-race-001"}},
            },
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        assert call_response.status_code == 401
