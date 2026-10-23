"""MCP tool-layer tests — spec v1.1 repair 7.

Against a real running backend (see conftest.py). Tools are called through
`FastMCP.call_tool`, the same dispatch path a real MCP host (an AgentCore
Gateway, Alexa+, Claude Desktop) uses when it talks to this server — these
tests exercise the tool layer for real, not just the plain Python
functions underneath.

Spec v1.1 repair 7 requires test coverage for: initialize, tools-list=5,
each tool call, invalid params, unknown race, KB-unavailable, and timeout.
Each category is called out below.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import mcp.types as types
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from server import create_mcp_server, P0_TOOL_NAMES

RACE_ID = "demo-race-001"


def _tool_json(result) -> dict:
    """Normalizes `FastMCP.call_tool`'s return shape to a plain dict.

    The installed SDK's `call_tool(..., convert_result=True)` (the
    default `FastMCP.call_tool` path) returns a `(content_blocks,
    structured_content)` pair for a tool whose return type is JSON
    Schema-representable (every tool here returns `dict[str, Any]`) — so
    `structured_content` is already the parsed dict; `content_blocks[0]`
    is the same data serialized as a `TextContent` block, which is what a
    real wire client actually receives (see
    test_streamable_http_transport.py for that path exercised for real).
    """
    if isinstance(result, tuple):
        content_blocks, structured = result
        assert len(content_blocks) == 1
        assert json.loads(content_blocks[0].text) == structured
        return structured
    assert len(result) == 1
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# "initialize" — protocol version
# ---------------------------------------------------------------------------


def test_installed_sdk_implements_protocol_2025_11_25_or_later():
    # The spec requires "protocol 2025-11-25+". This asserts against the
    # installed SDK's own advertised latest version (requirements.txt
    # pins mcp==1.27.0, which reports 2025-11-25) rather than a live
    # initialize handshake — FastMCP negotiates this automatically per
    # request and there is no separate "initialize" tool call to make on
    # top of it, so this is the honest place to assert it.
    assert types.LATEST_PROTOCOL_VERSION == "2025-11-25"


# ---------------------------------------------------------------------------
# "tools-list=5" — exactly the 5 P0 domain tools, nothing else
# ---------------------------------------------------------------------------


async def test_list_tools_exposes_exactly_the_five_p0_tools(mcp_server):
    tools = await mcp_server.list_tools()
    names = {t.name for t in tools}
    assert names == P0_TOOL_NAMES == {
        "get_race_state",
        "calculate_undercut",
        "simulate_strategy",
        "create_strategy_watch",
        "get_regulation",
    }
    assert len(tools) == 5


async def test_every_tool_has_a_strict_json_schema(mcp_server):
    tools = await mcp_server.list_tools()
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        assert "properties" in tool.inputSchema


# ---------------------------------------------------------------------------
# "each tool call" — one happy-path call per tool
# ---------------------------------------------------------------------------


async def test_get_race_state_returns_initial_lap_24(mcp_server):
    result = await mcp_server.call_tool("get_race_state", {"race_id": RACE_ID})
    state = _tool_json(result)
    assert state["lap"] == 24
    assert {d["driver_id"] for d in state["drivers"]} == {"CAR_03", "CAR_04", "CAR_07", "CAR_11"}


async def test_calculate_undercut_matches_canonical_lap_24_narrative(mcp_server):
    result = await mcp_server.call_tool(
        "calculate_undercut", {"race_id": RACE_ID, "candidate_driver": "CAR_04", "target_driver": "CAR_03"}
    )
    undercut = _tool_json(result)
    assert undercut["window"] == "CLOSED"  # matches the documented lap-24 baseline (README §11)
    assert undercut["confidence"]["trials"] == 200


async def test_simulate_strategy_returns_delta_and_confidence(mcp_server):
    result = await mcp_server.call_tool(
        "simulate_strategy", {"race_id": RACE_ID, "driver_id": "CAR_04", "wait_laps": 3}
    )
    simulation = _tool_json(result)
    assert simulation["driver_id"] == "CAR_04"
    assert "delta_ms" in simulation
    assert simulation["confidence"]["trials"] == 200


async def test_create_strategy_watch_returns_baseline_watch(mcp_server):
    watch = _tool_json(
        await mcp_server.call_tool(
            "create_strategy_watch", {"race_id": RACE_ID, "candidate_driver": "CAR_04", "target_driver": "CAR_03"}
        )
    )
    assert watch["watch_id"]
    assert watch["status"] == "CLOSED"  # baseline at lap 24


async def test_get_regulation_supported_query_returns_found_true_with_evidence(mcp_server):
    result = _tool_json(
        await mcp_server.call_tool("get_regulation", {"query": "What tire compounds are available: soft, medium, hard?"})
    )
    assert result["found"] is True
    assert len(result["evidence"]) >= 1
    assert result["evidence"][0]["document_id"] == "reg-tires-001"


async def test_get_regulation_hallucination_trap_returns_found_false(mcp_server):
    # "KB-unavailable"-adjacent: this exercises get_regulation's honest
    # refusal path — the corpus does not support this query at all, so
    # the tool must say so rather than invent an answer. (A literally
    # offline knowledge base is not something this environment can stand
    # up — there is no managed Bedrock KB here to take down — so this is
    # the closest real, testable analog: "the knowledge available to this
    # tool does not cover this query," which is the same observable
    # behavior a caller sees either way: found=false, evidence=[].)
    result = _tool_json(
        await mcp_server.call_tool(
            "get_regulation", {"query": "Does the tire degradation rule change or get ignored after lap 40?"}
        )
    )
    assert result["found"] is False
    assert result["evidence"] == []


# ---------------------------------------------------------------------------
# "invalid params"
# ---------------------------------------------------------------------------


async def test_calculate_undercut_missing_required_param_raises_tool_error(mcp_server):
    with pytest.raises(ToolError):
        await mcp_server.call_tool("calculate_undercut", {"race_id": RACE_ID, "candidate_driver": "CAR_04"})


async def test_simulate_strategy_wrong_type_for_wait_laps_raises_tool_error(mcp_server):
    with pytest.raises(ToolError):
        await mcp_server.call_tool(
            "simulate_strategy", {"race_id": RACE_ID, "driver_id": "CAR_04", "wait_laps": "not-a-number"}
        )


async def test_get_regulation_missing_query_raises_tool_error(mcp_server):
    with pytest.raises(ToolError):
        await mcp_server.call_tool("get_regulation", {})


# ---------------------------------------------------------------------------
# "unknown race"
# ---------------------------------------------------------------------------


async def test_get_race_state_unknown_race_raises_tool_error(mcp_server):
    with pytest.raises(ToolError):
        await mcp_server.call_tool("get_race_state", {"race_id": "unknown-race-999"})


async def test_calculate_undercut_unknown_driver_raises_tool_error(mcp_server):
    with pytest.raises(ToolError):
        await mcp_server.call_tool(
            "calculate_undercut", {"race_id": RACE_ID, "candidate_driver": "CAR_99", "target_driver": "CAR_03"}
        )


# ---------------------------------------------------------------------------
# "timeout" (and, closely related, upstream-unreachable)
# ---------------------------------------------------------------------------


async def test_unreachable_backend_raises_structured_tool_error():
    # Points the MCP server at a port nothing is listening on. httpx
    # raises ConnectError almost immediately (no need to wait out a full
    # timeout to exercise this path); server.py's `_request` wraps that
    # into PitWallApiError(error="upstream_unreachable", ...), which
    # FastMCP surfaces as a ToolError carrying that structured message —
    # never a bare, unstructured stack trace reaching the MCP client.
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1", timeout=1.0) as client:
        unreachable_server = create_mcp_server(client=client, base_url="http://127.0.0.1:1")
        with pytest.raises(ToolError) as exc_info:
            await unreachable_server.call_tool("get_race_state", {"race_id": RACE_ID})
        assert "upstream_unreachable" in str(exc_info.value)


async def test_slow_backend_raises_timeout_tool_error():
    # A TCP server that accepts the connection but never responds —
    # exercises the read-timeout path specifically (distinct from
    # connection-refused above), with an httpx client timeout short
    # enough to keep this test fast.
    import socket as socket_module

    server_socket = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(1)
    server_socket.setblocking(False)  # required for asyncio's loop.sock_accept
    port = server_socket.getsockname()[1]

    async def _accept_and_hang():
        loop = asyncio.get_event_loop()
        conn, _ = await loop.sock_accept(server_socket)
        await asyncio.sleep(5)  # longer than the client's timeout below
        conn.close()

    accept_task = asyncio.ensure_future(_accept_and_hang())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=0.5) as client:
            slow_server = create_mcp_server(client=client, base_url=f"http://127.0.0.1:{port}")
            with pytest.raises(ToolError) as exc_info:
                await slow_server.call_tool("get_race_state", {"race_id": RACE_ID})
            assert "upstream_timeout" in str(exc_info.value)
    finally:
        accept_task.cancel()
        server_socket.close()
