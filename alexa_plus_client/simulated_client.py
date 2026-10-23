"""Simulated Alexa+ client — spec v1.1 repair 10.

*** THIS IS A SIMULATION, NOT A REAL ALEXA+ INTEGRATION ***

There is no Alexa+ developer/onboarding account available in this
environment (the same documented constraint as Bedrock and Fire TV device
access in earlier gates — see `IMPLEMENTATION_STATUS.md` (removed in the repo cleanup; see git history)). This module
stands in for what a real Alexa+ agentic runtime would do once it has
been configured with this project's MCP server as an add-on: it holds an
MCP `ClientSession` over the same Streamable HTTP transport
`mcp_server/server.py` serves, and turns a short, fixed set of example
utterances into calls against the five P0 domain tools —
`get_race_state`, `calculate_undercut`, `simulate_strategy`,
`create_strategy_watch`, `get_regulation` — exactly as a real Alexa+ skill
configured against this MCP add-on would.

Why this, and not a fancier NLU: Alexa+'s own natural-language routing
(utterance -> tool call -> spoken response) is Amazon's proprietary,
hosted agentic runtime, and nothing in this repository can honestly
reproduce or substitute for it without deploying to a real Alexa+
account. What this module DOES prove, honestly, is the piece that is
ours to build and test end-to-end locally: that this project's MCP
server, served over real Streamable HTTP, is a complete and correct
foundation for that integration — a real `initialize` -> `tools/list` ->
`tools/call` round trip against the five P0 tools, producing spoken
responses whose every number comes straight from the deterministic
backend calculation, never invented here or by an LLM.

Primary architecture (spec v1.1 repair 10):

    Alexa+  --Streamable HTTP MCP-->  five domain tools  -->  backend

This module plays the "Alexa+" role in that diagram. It deliberately
does NOT duplicate backend orchestration over raw REST the way the
legacy `legacy/alexa_skill/` Lambda skill did — every one of the six
required voice journeys below reaches the backend exclusively through
an MCP tool call over this session's Streamable HTTP connection, with
one narrow, explicitly-documented exception: "Why now?" uses the
`GET .../alerts` and `GET .../alerts/{id}/explain` REST endpoints,
because `explain_alert` and `list_alerts` are, by spec v1.1 repair 7's
own design, deliberately NOT part of the five advertised P0 MCP domain
tools ("the existing list/get alert and explanation capabilities may
remain as REST endpoints, but they must not be advertised as additional
P0 MCP domain tools"). A real Alexa+ skill answering "why now" would
reach for the same two REST endpoints for the same reason.

Follow-up context (spec v1.1 repair 10: "Preserve follow-up context
using stable watch_id, alert_id and calculation_id") is held in
`ConversationState`, this module's analogue of the legacy skill's
ask-sdk-core session attributes — populated by `create_watch`,
consulted by `why_now`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult

# Fallback word/digit -> canonical driver_id map, identical in spirit to
# the legacy skill's _FALLBACK_WORD_TO_DRIVER_ID: a real Alexa+ interaction
# model's own slot-type resolution is the primary mechanism for a real
# deployment; this is what stands in for that here, since there is no real
# Alexa+ NLU available to resolve "car four" for us.
DRIVER_WORD_TO_ID = {
    "three": "CAR_03",
    "3": "CAR_03",
    "four": "CAR_04",
    "4": "CAR_04",
    "seven": "CAR_07",
    "7": "CAR_07",
    "eleven": "CAR_11",
    "11": "CAR_11",
}


def resolve_driver_word(word: str) -> Optional[str]:
    """"car four" / "4" / "four" -> "CAR_04". Returns None for an
    unrecognized word, matching the legacy skill's own "I didn't catch
    which car you meant" fallback behavior."""
    normalized = word.strip().lower().replace("car", "").strip()
    return DRIVER_WORD_TO_ID.get(normalized)


def speakable_driver_name(driver_id: str) -> str:
    """"CAR_04" -> "car 4" (not "car 04", which some TTS engines read as
    "car zero four") — identical to the legacy skill's helper."""
    number = driver_id.replace("CAR_", "").lstrip("0") or "0"
    return f"car {number}"


class SimulatedAlexaPlusError(RuntimeError):
    """Raised when an MCP tool call fails (isError=True) or the
    transport itself raises McpError — the simulated equivalent of the
    legacy skill's PitWallApiError, carrying through the tool's own
    structured error text rather than a bare exception message."""


@dataclass
class ConversationState:
    """Stable ids preserved across turns of one simulated voice session
    (spec v1.1 repair 10). Mirrors the legacy Alexa Skill's session
    attributes, but is owned by this client rather than by ask-sdk-core,
    since the primary path talks to the backend through MCP tool calls
    (plus the two REST endpoints noted in the module docstring), not
    ask-sdk-core's own REST orchestration."""

    race_id: str
    last_watch_id: Optional[str] = None
    last_calculation_id: Optional[str] = None
    last_alert_id: Optional[str] = None
    # All watches created this session, most-recent-last — lets a later
    # "why now" disambiguate by driver, exactly like the legacy skill's
    # Gate 5 multi-watch disambiguation (_find_watch_by_driver).
    watches: dict[str, dict[str, str]] = field(default_factory=dict)

    def remember_watch(self, watch_id: str, candidate: str, target: str) -> None:
        self.watches[watch_id] = {"candidate": candidate, "target": target}
        self.last_watch_id = watch_id

    def find_watch_by_driver(self, driver_id: str) -> Optional[tuple[str, str, str]]:
        candidate_matches = [(wid, w) for wid, w in self.watches.items() if w.get("candidate") == driver_id]
        target_matches = [(wid, w) for wid, w in self.watches.items() if w.get("target") == driver_id]
        matches = candidate_matches or target_matches
        if not matches:
            return None
        watch_id, watch = matches[-1]
        return watch_id, watch["candidate"], watch["target"]


def _unwrap_tool_result(tool_name: str, result: CallToolResult) -> dict[str, Any]:
    """Every one of the 5 P0 tools returns a JSON object as its single
    text content block (see mcp_server/server.py) — this parses that,
    or raises SimulatedAlexaPlusError with the tool's own structured
    error text when isError is set (spec v1.1 repair 7's "structured
    errors", carried through to this simulated voice layer rather than
    swallowed)."""
    if result.isError:
        detail = result.content[0].text if result.content else "(no error detail)"
        raise SimulatedAlexaPlusError(f"{tool_name} failed: {detail}")
    if not result.content:
        raise SimulatedAlexaPlusError(f"{tool_name} returned no content")
    return json.loads(result.content[0].text)


@asynccontextmanager
async def connected_session(
    mcp_base_url: str, *, access_token: Optional[str] = None
) -> AsyncIterator[ClientSession]:
    """Opens a real Streamable HTTP MCP session against a running
    `mcp_server` instance (e.g. `http://localhost:9000`) — a genuine
    initialize handshake, exactly what a real Alexa+ MCP add-on
    connection would do. `access_token` mirrors `mcp_server`'s own
    optional `PIT_WALL_MCP_ACCESS_TOKEN` bearer-auth check."""
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
    # AWS hackathon-final pass, P2-12: this was the one httpx.AsyncClient
    # in this codebase without an explicit `timeout=` -- every other
    # instance (this same file's own `_backend` client below,
    # mcp_server/server.py's backend client, both test conftests) sets
    # one explicitly. httpx does apply its own 5s default even when
    # unset, so this was never a genuinely UNBOUNDED hang risk, but
    # relying on that implicit default is inconsistent with this
    # codebase's own established "timeouts are stated, not assumed"
    # convention, and 10.0s matches every sibling client for the same
    # reason those chose it.
    http_client = httpx.AsyncClient(headers=headers, timeout=10.0) if headers else None
    try:
        async with streamable_http_client(
            f"{mcp_base_url.rstrip('/')}/mcp", http_client=http_client
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
    finally:
        if http_client is not None:
            await http_client.aclose()


class SimulatedAlexaPlusClient:
    """One simulated Alexa+ voice conversation. Holds an already-open MCP
    `ClientSession` (see `connected_session`) plus a plain `httpx.AsyncClient`
    for the two REST-only endpoints the module docstring explains (`why_now`
    only)."""

    def __init__(self, session: ClientSession, *, race_id: str, backend_base_url: str):
        self._session = session
        self._backend = httpx.AsyncClient(base_url=backend_base_url, timeout=10.0)
        self.state = ConversationState(race_id=race_id)

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self._session.call_tool(name, arguments)
        except McpError as exc:
            raise SimulatedAlexaPlusError(f"{name} failed: {exc}") from exc
        return _unwrap_tool_result(name, result)

    # ------------------------------------------------------------------
    # The six required primary voice journeys (spec v1.1 repair 10).
    # ------------------------------------------------------------------

    async def check_undercut(self, candidate_word: str, target_word: str) -> str:
        """Journey 1: "Check the undercut for car four against car
        three." -> calculate_undercut."""
        candidate = resolve_driver_word(candidate_word)
        target = resolve_driver_word(target_word)
        if not candidate or not target:
            return "I didn't catch which two cars you meant. Try, check the undercut for car four against car three."

        result = await self._call_tool(
            "calculate_undercut",
            {"race_id": self.state.race_id, "candidate_driver": candidate, "target_driver": target},
        )
        self.state.last_calculation_id = result["calculation_id"]
        window_phrase = "open" if result["window"] == "OPEN" else "closed"
        margin_ms = result["expected_margin_ms"]
        margin_phrase = f"{abs(margin_ms)} milliseconds " + ("ahead" if margin_ms >= 0 else "behind")
        probability_pct = round(result["confidence"]["probability_ahead"] * 100, 1)
        return (
            f"The undercut window for {speakable_driver_name(candidate)} against "
            f"{speakable_driver_name(target)} is {window_phrase}. The simulation projects "
            f"{speakable_driver_name(candidate)} finishing {margin_phrase}, with a "
            f"{probability_pct} percent chance across two hundred simulated trials."
        )

    async def create_watch(self, candidate_word: str, target_word: str) -> str:
        """Journey 2: "Watch car four against car three." ->
        create_strategy_watch. Remembers watch_id in ConversationState so
        a later why_now() in the same simulated session can refer back to
        it (spec v1.1 repair 10's "preserve follow-up context")."""
        candidate = resolve_driver_word(candidate_word)
        target = resolve_driver_word(target_word)
        if not candidate or not target:
            return "I didn't catch which two cars you meant. Try, watch the undercut for car four against car three."

        result = await self._call_tool(
            "create_strategy_watch",
            {"race_id": self.state.race_id, "candidate_driver": candidate, "target_driver": target},
        )
        self.state.remember_watch(result["watch_id"], candidate, target)

        status_phrase = "already open" if result["status"] == "OPEN" else "currently closed"
        return (
            f"Watching {speakable_driver_name(candidate)} against {speakable_driver_name(target)}. "
            f"The undercut window is {status_phrase}. I'll have an alert ready the moment that changes. "
            "Just ask me to explain the latest alert once it fires."
        )

    async def why_now(self, driver_word: Optional[str] = None) -> str:
        """Journey 3: "Why now?" / "Explain the car four watch." Uses the
        stored triggering snapshot via GET .../alerts + .../explain — see
        the module docstring's explanation of why this one journey is
        REST, not MCP: explain_alert/list_alerts are deliberately not
        among the five advertised P0 MCP domain tools (spec v1.1 repair
        7)."""
        watch_id: Optional[str] = None
        if driver_word:
            driver_id = resolve_driver_word(driver_word)
            if driver_id is None:
                return "I didn't catch which car's watch you meant."
            found = self.state.find_watch_by_driver(driver_id)
            if found is None:
                return f"I don't have a watch open on {speakable_driver_name(driver_id)} this session."
            watch_id = found[0]
        else:
            watch_id = self.state.last_watch_id

        params = {"watch_id": watch_id} if watch_id else None
        response = await self._backend.get(f"/v1/races/{self.state.race_id}/alerts", params=params)
        response.raise_for_status()
        alerts = response.json()
        if not alerts:
            return "There are no alerts yet. Ask me to watch an undercut first, and I'll let you know when it changes."

        latest_alert_id = alerts[0]["alert_id"]
        self.state.last_alert_id = latest_alert_id
        explain_response = await self._backend.get(
            f"/v1/races/{self.state.race_id}/alerts/{latest_alert_id}/explain"
        )
        explain_response.raise_for_status()
        return explain_response.json()["narrative"]

    async def simulate_pit_now_vs_wait(self, driver_word: str, wait_laps: int = 3) -> str:
        """Journey 4: "Should car four pit now, or wait three laps?" ->
        simulate_strategy (UJ-3)."""
        driver_id = resolve_driver_word(driver_word)
        if driver_id is None:
            return "I didn't catch which car you meant."

        result = await self._call_tool(
            "simulate_strategy",
            {"race_id": self.state.race_id, "driver_id": driver_id, "wait_laps": wait_laps},
        )
        self.state.last_calculation_id = result["calculation_id"]
        delta_ms = result["delta_ms"]
        seconds = abs(delta_ms) / 1000.0
        faster_option = "pitting now" if delta_ms >= 0 else f"waiting {wait_laps} more laps"
        return (
            f"For {speakable_driver_name(driver_id)}: {faster_option} is projected "
            f"{seconds:.1f} seconds faster than the alternative, comparing pit now against "
            f"waiting {wait_laps} more laps."
        )

    async def ask_regulation(self, query: str) -> str:
        """Journeys 5 and 6: a supported and an unsupported regulation
        question -> get_regulation. Builds its own spoken narrative from
        the tool's raw found/evidence/available fields (never a narrative
        field from the tool itself, since get_regulation returns none) —
        the same found/evidence contract Fire TV's EvidenceSourcesScreen
        renders, and the same UJ-5 hallucination-trap discipline: an
        unsupported query is answered with an explicit refusal, never a
        guessed rule (spec v1.1 repair 8)."""
        result = await self._call_tool("get_regulation", {"query": query})

        if not result.get("available", True):
            # Spec v1.1 repair 14 / Section 29 "Knowledge Base unavailable".
            return "Regulation source unavailable. I won't infer the rule."

        if not result["found"]:
            return (
                "I don't have a rule supporting that in the available sporting-rule sources, "
                "so I won't use that assumption."
            )

        evidence = result["evidence"][0]
        return f"{evidence['title']}: {evidence['excerpt']}"
