from __future__ import annotations

import pytest

from app.services.llm_client import BedrockLLMClient, LLMClientError, TemplateLLMClient
from app.services.explanation_engine import SYSTEM_PROMPT

SAMPLE_FACTS = {
    "candidate": "CAR_04",
    "target": "CAR_03",
    "previous_status": "CLOSED",
    "new_status": "OPEN",
    "window": "OPEN",
    "triggering_lap": "26",
    "expected_margin_ms": "910",
    "trials": "200",
    "probability_ahead": "70.5%",
    "p10_margin_ms": "-1626",
    "p90_margin_ms": "3456",
}


def test_template_client_identifies_itself_as_non_bedrock():
    client = TemplateLLMClient()
    assert client.provider_name == "template"
    assert client.model_id


def test_template_client_is_deterministic_for_identical_inputs():
    client = TemplateLLMClient()
    first = client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
    second = client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
    assert first == second


def test_template_client_narrative_mentions_the_given_facts():
    client = TemplateLLMClient()
    narrative = client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
    assert "CAR_04" in narrative
    assert "CAR_03" in narrative
    assert "26" in narrative
    assert "910" in narrative
    assert "70.5%" in narrative


def test_template_client_never_mentions_a_number_it_was_not_given():
    """A cheap direct sanity check independent of ExplanationEngine's own
    grounding guard (tested separately in test_explanation_engine.py):
    the template only ever interpolates values that are literally present
    in the facts dict, so no other numeric string can appear."""
    client = TemplateLLMClient()
    narrative = client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
    for forbidden in ("999", "4200", "12345"):
        assert forbidden not in narrative


def test_bedrock_client_can_be_constructed_without_network_access():
    """boto3.client(...) construction is a local operation (no network
    call), so this is safe to run without AWS credentials or egress. The
    resulting client is NOT invoked against real AWS anywhere in this
    test suite — see llm_client.py's module docstring."""
    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    assert client.provider_name == "bedrock"
    assert client.model_id == "anthropic.claude-3-haiku-20240307-v1:0"


def test_bedrock_client_render_user_prompt_embeds_all_facts_and_citations():
    from app.domain.explanation_models import KBCitation

    citation = KBCitation(
        doc_id="sr-undercut-001",
        title="The Undercut",
        source_path="docs/sporting-rules/undercut.md",
        excerpt="An undercut is a strategic pit-stop sequence...",
        score=3,
    )
    prompt = BedrockLLMClient._render_user_prompt(SAMPLE_FACTS, [citation])
    for key, value in SAMPLE_FACTS.items():
        assert key in prompt
        assert value in prompt
    assert "sr-undercut-001" in prompt
    assert "The Undercut" in prompt


def test_bedrock_client_configures_bounded_connect_and_read_timeouts():
    # AWS hackathon-final pass, P2-12: without an explicit botocore Config,
    # boto3's default timeouts are large enough that a hung Bedrock call
    # made via run_in_threadpool (see app/api/routes.py) could tie up a
    # worker thread for a very long time. Asserts the actual bound applied
    # to the constructed client, not just that construction succeeds.
    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    botocore_config = client._client.meta.config
    assert botocore_config.connect_timeout == 5
    assert botocore_config.read_timeout == 15
    # botocore normalizes the `max_attempts` we pass into its own internal
    # `total_max_attempts` key once the client is constructed. Empirically
    # (verified against the installed botocore==1.35.99 at the time of
    # this pass), the effective `total_max_attempts` comes out ONE HIGHER
    # than the `max_attempts` value we passed in (max_attempts=2 ->
    # total_max_attempts=3) -- asserting on the normalized, actually-
    # effective value here rather than re-asserting our own input back at
    # ourselves, so this test would catch a change in that behavior.
    assert botocore_config.retries["mode"] == "standard"
    assert botocore_config.retries["total_max_attempts"] == 3


def test_bedrock_client_explain_raises_llm_client_error_without_real_aws_access():
    """Local-demo-fixed repair, issue 5: this test used to make a REAL
    `bedrock-runtime` network call and simply expect it to fail (no AWS
    credentials/network available in that environment) — unsafe, because
    it stops being a no-network test and starts being a real (and, in an
    environment that DOES have Bedrock access and billing enabled,
    billable) Bedrock invocation the moment it runs somewhere less
    restricted. It also means this test's pass/fail depended on the
    ambient network environment rather than on `BedrockLLMClient`'s own
    error-handling code.

    Replaced with a `botocore.stub.Stubber`: a local, fully offline test
    double that intercepts the call inside botocore itself, before any
    DNS lookup, HTTP connection, or credential lookup happens, and hands
    back a realistic AWS-shaped error (`ThrottlingException`, the same
    error code exercised in `test_bedrock_trust_scenarios.py`'s scenario
    5). This proves the exact same contract the original test intended —
    a real Bedrock-shaped failure is converted to `LLMClientError`, never
    left as a raw boto3/botocore exception — with zero network dependency
    at all, so it passes identically whether or not the sandbox running it
    happens to have AWS egress.
    """
    from botocore.stub import Stubber

    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    stubber = Stubber(client._client)
    stubber.add_client_error(
        "invoke_model",
        service_error_code="ThrottlingException",
        service_message="Too many requests, please wait before trying again.",
        http_status_code=429,
    )
    with stubber:
        with pytest.raises(LLMClientError):
            client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
    # The Stubber asserts internally that the expected call was made and
    # that no unconsumed responses remain; nothing further to check here.


def test_bedrock_client_explain_never_attempts_real_network_access(monkeypatch):
    """Belt-and-suspenders companion to the Stubber test above: even if a
    future change accidentally bypassed the Stubber, this asserts at the
    socket layer that `explain()` makes no outbound connection attempt at
    all. `socket.create_connection` is patched to raise immediately if
    called; the Stubber (which intercepts far earlier, inside botocore's
    request pipeline) means it is never reached, and this test proves
    that by construction rather than by hoping the network is unreachable
    in whatever environment runs it."""
    import socket as socket_module

    from botocore.stub import Stubber

    def _fail_if_called(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("explain() attempted a real network connection")

    monkeypatch.setattr(socket_module, "create_connection", _fail_if_called)

    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    stubber = Stubber(client._client)
    stubber.add_client_error("invoke_model", service_error_code="ThrottlingException")
    with stubber:
        with pytest.raises(LLMClientError):
            client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
