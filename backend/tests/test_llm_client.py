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
    """Exercises the real failure path (no AWS credentials/network here)
    end to end: invoke_model must fail, and BedrockLLMClient must wrap
    that failure in LLMClientError rather than letting a raw boto3/botocore
    exception escape. This is a genuine assertion about error handling,
    not a claim that Bedrock itself was successfully called."""
    client = BedrockLLMClient(model_id="anthropic.claude-3-haiku-20240307-v1:0", region_name="us-east-1")
    with pytest.raises(LLMClientError):
        client.explain(system_prompt=SYSTEM_PROMPT, grounded_facts=SAMPLE_FACTS, citations=[])
