"""Bedrock trust/evidence hardening (AWS hackathon-final pass, P2).

Every existing grounding/attribution test in `test_explanation_engine.py`
exercises `ExplanationEngine` against a hand-written FAKE `LLMClient`
stub — never against a real `BedrockLLMClient` parsing an actual
`invoke_model` response shape. That leaves a real gap: nothing proves
`BedrockLLMClient.explain()` itself correctly parses a realistic Bedrock
response, or that `ExplanationEngine` degrades safely for every
*Bedrock-specific* failure mode (malformed body, no text content, a
throttling/throughput exception) rather than only the generic
`LLMClientError` path already covered.

This file closes that gap with SIX NAMED scenarios, each driving a real
`BedrockLLMClient` (constructed normally — `boto3.client(...)`
construction is local, no network call) whose `_client.invoke_model` is
replaced with a `MagicMock` returning/raising exactly what a real
`bedrock-runtime` call could return/raise, run end-to-end through a real
`ExplanationEngine.explain_alert` against a REAL alert produced by the
actual Gate 1/2 pipeline (via `_make_real_alert`, reused from
`test_explanation_engine.py`) — not a synthetic grounded-facts dict. This
is "trust hardening": the point is not that Bedrock is trusted, it's that
every one of these scenarios proves the system does NOT blindly trust it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from app.services.explanation_engine import BEDROCK_UNAVAILABLE_NARRATIVE, ExplanationEngine
from app.services.llm_client import BedrockLLMClient
from tests.test_explanation_engine import _make_real_alert

pytestmark = pytest.mark.unit


def _bedrock_client(monkeypatch) -> BedrockLLMClient:
    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    monkeypatch.setattr(client, "_client", MagicMock())
    return client


def _fake_body(payload: dict) -> MagicMock:
    """A stand-in for the `StreamingBody` boto3 hands back in
    `response["body"]` — `BedrockLLMClient.explain` only ever calls
    `.read()` on it, exactly like the real one."""
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return body


def _fake_raw_body(raw: bytes) -> MagicMock:
    body = MagicMock()
    body.read.return_value = raw
    return body


class TestBedrockTrustScenarios:
    # -----------------------------------------------------------------
    # Scenario 1: a well-formed, honest response is trusted and passes
    # both guards clean.
    # -----------------------------------------------------------------
    async def test_scenario_1_well_formed_grounded_response_passes_clean(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        client = _bedrock_client(monkeypatch)
        result = alert.deterministic_result
        honest_text = (
            f"On lap {alert.triggering_snapshot.lap}, the undercut watch on {result.candidate} against "
            f"{result.target} opened. The projected margin is {result.expected_margin_ms}ms, with a "
            f"{result.confidence.probability_ahead * 100:.1f}% chance {result.candidate} comes out ahead "
            f"across {result.confidence.trials} simulated trials."
        )
        client._client.invoke_model.return_value = {
            "body": _fake_body({"content": [{"type": "text", "text": honest_text}]})
        }
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is False
        assert explanation.grounding_warnings == ()
        assert explanation.narrative == honest_text
        assert explanation.llm_provider == "bedrock"

    # -----------------------------------------------------------------
    # Scenario 2: Bedrock invents a number that is nowhere in the
    # grounded facts. The system does not crash and does not silently
    # trust it — it surfaces the problem in grounding_warnings while
    # still returning the (flagged) narrative, exactly as it would for
    # any other hallucinating client (test_explanation_engine.py's
    # `test_grounding_guard_flags_a_hallucinating_llm_client` proves the
    # same contract against a fake client; this proves it end-to-end
    # through the real Bedrock response-parsing path too).
    # -----------------------------------------------------------------
    async def test_scenario_2_hallucinated_number_is_flagged_not_silently_trusted(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        client = _bedrock_client(monkeypatch)
        result = alert.deterministic_result
        hallucinated_text = (
            f"{result.candidate} will pit and come out 47281ms ahead of {result.target} — "
            "a number invented by this test, present nowhere in the grounded facts."
        )
        client._client.invoke_model.return_value = {
            "body": _fake_body({"content": [{"type": "text", "text": hallucinated_text}]})
        }
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is False  # Bedrock DID respond; this is a grounding failure, not an outage
        assert any("47281" in warning for warning in explanation.grounding_warnings)
        # The flagged narrative is still returned (visible for debugging/
        # judging), never silently swapped for something else — the
        # warning is the trust signal, not a cover-up.
        assert explanation.narrative == hallucinated_text

    # -----------------------------------------------------------------
    # Scenario 3: Bedrock responds successfully, but the content array
    # has no text block at all (e.g. a tool-use-only response) — text
    # extraction yields an empty string, which `BedrockLLMClient.explain`
    # explicitly rejects rather than returning a blank explanation.
    # -----------------------------------------------------------------
    async def test_scenario_3_response_with_no_text_content_degrades_safely(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        client = _bedrock_client(monkeypatch)
        client._client.invoke_model.return_value = {
            "body": _fake_body({"content": [{"type": "tool_use", "name": "some_tool", "input": {}}]})
        }
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is True
        assert explanation.narrative == BEDROCK_UNAVAILABLE_NARRATIVE
        assert explanation.grounding_warnings == ()

    # -----------------------------------------------------------------
    # Scenario 4: the response body is not valid JSON at all (a
    # malformed/truncated payload) — must not raise an unhandled
    # exception out of the explain() call chain into the API route.
    # -----------------------------------------------------------------
    async def test_scenario_4_malformed_response_body_degrades_safely(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        client = _bedrock_client(monkeypatch)
        client._client.invoke_model.return_value = {"body": _fake_raw_body(b"not valid json {{{")}
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is True
        assert explanation.narrative == BEDROCK_UNAVAILABLE_NARRATIVE

    # -----------------------------------------------------------------
    # Scenario 5: a real AWS-shaped failure — ThrottlingException, the
    # single most likely real-world Bedrock failure under judge-day
    # concurrent load — raised directly by invoke_model as botocore
    # would raise it, not merely a generic Python exception.
    # -----------------------------------------------------------------
    async def test_scenario_5_throttling_exception_degrades_safely_not_a_500(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        client = _bedrock_client(monkeypatch)
        client._client.invoke_model.side_effect = botocore.exceptions.ClientError(
            error_response={
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "Too many requests, please wait before trying again.",
                }
            },
            operation_name="InvokeModel",
        )
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is True
        assert explanation.narrative == BEDROCK_UNAVAILABLE_NARRATIVE
        # The API route (app/api/routes.py's explain_alert) returns this
        # ExplanationResult with a normal 200 — a Bedrock throttle must
        # never surface as a 500 to the Fire TV/Alexa+ client (spec v1.1
        # repair 14 / Section 29's "Bedrock unavailable" contract).

    # -----------------------------------------------------------------
    # Scenario 6: a realistic narrative that states both real, grounded
    # tire-age numbers but swaps which driver each belongs to — the
    # attribution guard, not the plain grounding guard, is what catches
    # this (see explanation_engine.py's module docstring). Proven here
    # through the real Bedrock parsing path with genuinely different
    # per-driver tire ages pulled from the real alert.
    # -----------------------------------------------------------------
    async def test_scenario_6_bedrock_narrative_with_swapped_driver_attribution_is_caught(
        self, scenario, watch_evaluator, knowledge_base, monkeypatch
    ):
        alert = await _make_real_alert(scenario, watch_evaluator)
        result = alert.deterministic_result
        if result.candidate_tire_age_laps == result.target_tire_age_laps:
            pytest.skip("this scenario's real alert has identical tire ages for both drivers; nothing to swap")
        client = _bedrock_client(monkeypatch)
        # Deliberately swapped: candidate's sentence carries the TARGET's
        # tire age and vice versa.
        swapped_text = (
            f"{result.candidate}'s tires are {result.target_tire_age_laps} laps old, while "
            f"{result.target}'s tires are {result.candidate_tire_age_laps} laps old."
        )
        client._client.invoke_model.return_value = {
            "body": _fake_body({"content": [{"type": "text", "text": swapped_text}]})
        }
        engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=client)

        explanation = engine.explain_alert(alert)

        assert explanation.degraded is False
        assert any("instead" in warning for warning in explanation.grounding_warnings)
