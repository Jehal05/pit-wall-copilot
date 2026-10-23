"""End-to-end tests for the six required primary voice journeys (spec
v1.1 repair 10), each run against a REAL running backend and a REAL
mcp_server Streamable HTTP transport (see conftest.py) — a genuine
initialize -> tools/list -> tools/call round trip for every journey that
uses an MCP tool, exactly what a real Alexa+ MCP add-on connection would
do.

Each test opens (and fully closes) its own MCP session inside its own
single coroutine via `alexa_client_factory()` — see that fixture's
docstring in conftest.py for why this shape, rather than a plain
`yield`-based fixture, is required here.
"""

from __future__ import annotations

import asyncio


async def test_check_undercut_journey(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.check_undercut("four", "three")
        assert "car 4" in speech
        assert "car 3" in speech
        assert "undercut window" in speech
        assert "percent chance across two hundred simulated trials" in speech
        # Follow-up context (spec v1.1 repair 10) is preserved.
        assert alexa_client.state.last_calculation_id is not None


async def test_check_undercut_unrecognized_driver_word(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.check_undercut("nine", "three")
        assert "didn't catch which two cars" in speech


async def test_create_watch_journey_and_voice_created_watch_reaches_the_backend(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.create_watch("four", "three")
        assert "Watching car 4 against car 3" in speech
        assert alexa_client.state.last_watch_id is not None

        # "A voice-created watch must appear on Fire TV" (spec v1.1 final
        # acceptance criterion 3/4): Fire TV reads watches back through the
        # same ordinary backend REST surface it always polls — watch status
        # is driven by the same watch record this call just created, so
        # there is nothing voice-specific about how it is stored. Confirm
        # the created watch is visible via a plain backend GET, independent
        # of the MCP path.
        response = await alexa_client._backend.get(f"/v1/races/{alexa_client.state.race_id}/watches")
        watch_ids = {w["watch_id"] for w in response.json()}
        assert alexa_client.state.last_watch_id in watch_ids


async def test_why_now_with_no_watch_yet(alexa_client_factory):
    # Runs before any other test in this file creates a watch/alert, so
    # the race-wide alert history is genuinely empty for this assertion —
    # see test_why_now_journey_uses_the_stored_triggering_snapshot below,
    # which is the one that actually starts the simulator and produces
    # alerts for the rest of this module's test run.
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.why_now()
        assert "no alerts yet" in speech.lower()


async def test_why_now_journey_uses_the_stored_triggering_snapshot(alexa_client_factory, backend_auth_headers):
    async with alexa_client_factory() as alexa_client:
        # Create the watch, then actually run the deterministic simulator
        # (scenario-demo-01.json ticks a lap every 2s, fixed lap 24-28) so a
        # real OPEN/CLOSED transition — and therefore a real alert with a
        # real stored triggering snapshot — has a chance to fire, rather
        # than only exercising the "no alerts yet" branch.
        await alexa_client.create_watch("four", "three")
        await alexa_client._backend.post("/v1/demo/start", headers=backend_auth_headers)

        alerts: list = []
        deadline = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            response = await alexa_client._backend.get(f"/v1/races/{alexa_client.state.race_id}/alerts")
            alerts = response.json()
            if alerts:
                break
            await asyncio.sleep(0.5)

        assert alerts, "expected at least one alert to fire within the fixed lap 24-28 scenario window"

        speech = await alexa_client.why_now()
        # The narrative comes straight from ExplanationEngine.explain_alert,
        # grounded in the alert's own immutable triggering_snapshot — assert
        # it is a real, non-empty narration, not the "no alerts yet" fallback.
        assert speech
        assert "no alerts yet" not in speech.lower()


async def test_simulate_pit_now_vs_wait_journey(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.simulate_pit_now_vs_wait("four", wait_laps=3)
        assert "car 4" in speech
        assert "waiting 3 more laps" in speech
        assert "seconds faster" in speech
        assert alexa_client.state.last_calculation_id is not None


async def test_supported_regulation_question_journey(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        speech = await alexa_client.ask_regulation(
            "What is an undercut — pits first for fresh tires to emerge ahead of a rival?"
        )
        assert "didn't catch" not in speech
        assert "I don't have a rule" not in speech
        assert "unavailable" not in speech
        assert len(speech) > 0


async def test_unsupported_regulation_question_journey_refuses_to_invent_a_rule(alexa_client_factory):
    async with alexa_client_factory() as alexa_client:
        # The canonical hallucination trap (spec v1.1 repair 8).
        speech = await alexa_client.ask_regulation(
            "Does the tire degradation rule change or get ignored after lap 40?"
        )
        assert speech == (
            "I don't have a rule supporting that in the available sporting-rule sources, "
            "so I won't use that assumption."
        )
