"""End-to-end Streamable HTTP transport test — spec v1.1 repair 7.

Unlike test_mcp_tools.py (which calls `FastMCP.call_tool` in-process),
this test suite actually serves `build_streamable_http_app(...)` over a
real TCP port with uvicorn and connects to it with the official MCP
Streamable HTTP client, doing a genuine initialize -> tools/list ->
tools/call round trip over the wire. This is what proves the transport
rewrite itself (not just the tool functions underneath it) — the /health
and /ready routes, the auth middleware, and the actual HTTP framing.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server import build_streamable_http_app, create_mcp_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def running_mcp_server(reset_race):
    """Serves the real Streamable HTTP app (with health/ready routes and
    auth middleware) on an ephemeral port, in a background thread, for
    the duration of one test — pointed at the same real backend
    subprocess `reset_race`/`mcp_server` already use."""
    from tests.conftest import TEST_DEMO_ACCESS_TOKEN

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
        # A real MCP client session's own anyio task-group teardown (in
        # the test body above, already complete by this point) can leave
        # this test's event loop in a state where a subsequent graceful
        # httpx close raises "Event loop is closed" despite everything
        # having already completed successfully — a fixture-teardown-
        # ordering artifact of mixing anyio (the MCP SDK's concurrency
        # backend) with asyncio directly in the same test, not a defect
        # in the server being tested. Best-effort close; never let this
        # mask a real assertion failure, which would already have
        # surfaced above as a FAILED test, not just this teardown.
        try:
            await backend_client.aclose()
        except RuntimeError:
            pass


async def test_health_endpoint_is_reachable_without_a_session(running_mcp_server):
    async with httpx.AsyncClient(base_url=running_mcp_server, timeout=5.0) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert set(body["tools"]) == {
            "calculate_undercut", "create_strategy_watch", "get_race_state", "get_regulation", "simulate_strategy",
        }


async def test_ready_endpoint_reports_backend_reachable(running_mcp_server):
    async with httpx.AsyncClient(base_url=running_mcp_server, timeout=5.0) as client:
        response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


async def test_real_initialize_tools_list_and_tool_call_over_streamable_http(running_mcp_server):
    async with streamable_http_client(f"{running_mcp_server}/mcp") as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            assert init_result.protocolVersion  # a real handshake happened

            tools_result = await session.list_tools()
            names = {t.name for t in tools_result.tools}
            assert names == {
                "calculate_undercut", "create_strategy_watch", "get_race_state", "get_regulation", "simulate_strategy",
            }

            call_result = await session.call_tool("get_race_state", {"race_id": "demo-race-001"})
            assert call_result.isError is not True
            import json

            state = json.loads(call_result.content[0].text)
            assert state["lap"] == 24
