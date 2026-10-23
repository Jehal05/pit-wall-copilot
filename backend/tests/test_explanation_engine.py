from __future__ import annotations

import uuid

from app.domain.watch_models import Alert
from app.services.explanation_engine import BEDROCK_UNAVAILABLE_NARRATIVE, ExplanationEngine
from app.services.llm_client import LLMClientError
from tests.conftest import race_state_at_lap


async def _make_real_alert(scenario, watch_evaluator) -> Alert:
    """Drives the real Gate 1/2 pipeline (same pattern as
    test_watch_evaluator.py's real-scenario tests) to produce one genuine,
    fully-populated Alert to explain — not a hand-built fixture, so the
    explanation engine is exercised against the actual demo narrative."""
    run_id = str(uuid.uuid4())
    baseline = race_state_at_lap(scenario, 24, run_id=run_id, state_version=0)
    await watch_evaluator.create_watch(baseline, "CAR_04", "CAR_03")

    alerts = []
    for lap, version in [(25, 1), (26, 2), (27, 3), (28, 4)]:
        state = race_state_at_lap(scenario, lap, run_id=run_id, state_version=version)
        alerts += await watch_evaluator.evaluate(state)
    assert alerts, "expected the canonical demo narrative to open the undercut watch"
    return alerts[0]


# ---------------------------------------------------------------------------
# 1. Grounded facts are built entirely from the alert's own immutable data
# ---------------------------------------------------------------------------
async def test_explain_alert_grounds_facts_in_the_alerts_own_data(scenario, watch_evaluator, explanation_engine):
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = explanation_engine.explain_alert(alert)

    assert result.alert_id == alert.alert_id
    assert result.watch_id == alert.watch_id
    assert result.race_id == alert.race_id
    assert result.run_id == alert.run_id
    assert result.grounded_facts["candidate"] == alert.deterministic_result.candidate
    assert result.grounded_facts["target"] == alert.deterministic_result.target
    assert result.grounded_facts["expected_margin_ms"] == str(alert.deterministic_result.expected_margin_ms)
    assert result.grounded_facts["triggering_lap"] == str(alert.triggering_snapshot.lap)
    assert result.grounded_facts["new_status"] == alert.new_status.value


async def test_explain_alert_with_template_client_has_no_grounding_warnings(
    scenario, watch_evaluator, explanation_engine
):
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = explanation_engine.explain_alert(alert)

    assert result.llm_provider == "template"
    assert result.narrative  # non-empty
    assert result.grounding_warnings == ()


async def test_explain_alert_retrieves_undercut_citations(scenario, watch_evaluator, explanation_engine):
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = explanation_engine.explain_alert(alert)

    assert result.citations
    assert any(c.doc_id == "sr-undercut-001" for c in result.citations)


# ---------------------------------------------------------------------------
# 2. The grounding guard actually catches a hallucinating LLM client
# ---------------------------------------------------------------------------
class _HallucinatingLLMClient:
    """A fake LLM client that deliberately states numbers it was never
    given, to prove the grounding guard is load-bearing rather than a
    check that would trivially always pass."""

    provider_name = "fake-hallucinating"
    model_id = "fake-v1"

    def explain(self, *, system_prompt, grounded_facts, citations) -> str:
        del system_prompt, grounded_facts, citations
        return "The candidate is 4200ms ahead with a 99% chance of success."


class _HonestButDifferentlyFormattedLLMClient:
    """A fake LLM client that only ever restates given facts, but in a
    slightly different textual form (e.g. rounds a percentage differently
    from the template) — used to confirm the guard doesn't just check for
    an exact string match against one specific rendering."""

    provider_name = "fake-honest"
    model_id = "fake-v1"

    def explain(self, *, system_prompt, grounded_facts, citations) -> str:
        del system_prompt, citations
        margin = grounded_facts["expected_margin_ms"]
        lap = grounded_facts["triggering_lap"]
        return f"On lap {lap}, the margin is {margin}ms."


async def test_grounding_guard_flags_a_hallucinating_llm_client(scenario, watch_evaluator, knowledge_base):
    alert = await _make_real_alert(scenario, watch_evaluator)
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_HallucinatingLLMClient())

    result = engine.explain_alert(alert)

    assert result.grounding_warnings != ()
    assert any("4200" in w for w in result.grounding_warnings)
    assert any("99" in w for w in result.grounding_warnings)


async def test_grounding_guard_passes_an_honest_differently_formatted_client(
    scenario, watch_evaluator, knowledge_base
):
    alert = await _make_real_alert(scenario, watch_evaluator)
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_HonestButDifferentlyFormattedLLMClient())

    result = engine.explain_alert(alert)

    assert result.grounding_warnings == ()


async def test_grounding_guard_ignores_small_integers_to_avoid_false_positives(
    scenario, watch_evaluator, knowledge_base
):
    """Small integers (0-9) in ordinary phrasing (e.g. list counts) should
    not be flagged even if they don't correspond to a grounded fact —
    otherwise a real generative model's natural phrasing would be
    constantly (and wrongly) flagged."""
    alert = await _make_real_alert(scenario, watch_evaluator)

    class _ClientWithSmallTalk:
        provider_name = "fake-smalltalk"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, citations
            return f"Here are 3 quick points about {grounded_facts['candidate']}'s undercut attempt."

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_ClientWithSmallTalk())
    result = engine.explain_alert(alert)
    assert result.grounding_warnings == ()


# ---------------------------------------------------------------------------
# 3. explanation_id / created_at bookkeeping + per-alert caching
# ---------------------------------------------------------------------------
async def test_explain_alert_is_cached_by_alert_id(scenario, watch_evaluator, explanation_engine):
    """Fixes the previously-documented Gate 3 limitation ("No caching ...
    on /explain"): re-explaining the same immutable alert must return the
    exact same cached ExplanationResult (same explanation_id, same
    narrative, same created_at), not silently regenerate one — see
    explanation_engine.py's module docstring for why that's correct, not
    just cheaper."""
    alert = await _make_real_alert(scenario, watch_evaluator)
    first = explanation_engine.explain_alert(alert)
    second = explanation_engine.explain_alert(alert)
    assert first.explanation_id == second.explanation_id
    assert first.narrative == second.narrative
    assert first.created_at == second.created_at
    assert first.grounded_facts == second.grounded_facts
    assert explanation_engine.cached_explanation_count() == 1


async def test_explain_alert_cache_hit_does_not_call_the_llm_again(scenario, watch_evaluator, knowledge_base):
    """Proves the cache actually skips LLM invocation on a hit (not just
    that the returned object happens to look the same) — the property
    that matters for a real Bedrock-backed deployment, where every avoided
    call is an avoided charge."""

    class _CountingLLMClient:
        provider_name = "fake-counting"
        model_id = "fake-v1"

        def __init__(self):
            self.call_count = 0

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, citations
            self.call_count += 1
            return f"Narrative #{self.call_count} for {grounded_facts['candidate']}."

    counting_client = _CountingLLMClient()
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=counting_client)
    alert = await _make_real_alert(scenario, watch_evaluator)

    engine.explain_alert(alert)
    engine.explain_alert(alert)
    engine.explain_alert(alert)

    assert counting_client.call_count == 1


async def test_explain_alert_force_refresh_bypasses_the_cache(scenario, watch_evaluator, explanation_engine):
    alert = await _make_real_alert(scenario, watch_evaluator)
    first = explanation_engine.explain_alert(alert)
    second = explanation_engine.explain_alert(alert, force_refresh=True)
    assert first.explanation_id != second.explanation_id
    # Still grounded identically — force_refresh re-generates, it doesn't
    # change what facts the alert grounds to.
    assert first.grounded_facts == second.grounded_facts
    # The refreshed result is what a THIRD, ordinary (non-refresh) call
    # now returns — force_refresh also re-caches, it doesn't just bypass.
    third = explanation_engine.explain_alert(alert)
    assert third.explanation_id == second.explanation_id


async def test_explain_alert_caches_separately_per_alert_id(scenario, watch_evaluator, explanation_engine):
    # _make_real_alert drives a fresh run_id/watch/alert each call, so these
    # are two genuinely distinct alerts (distinct alert_id) even though
    # they describe the same CAR_04-vs-CAR_03 matchup.
    first_alert = await _make_real_alert(scenario, watch_evaluator)
    second_alert = await _make_real_alert(scenario, watch_evaluator)
    assert first_alert.alert_id != second_alert.alert_id

    explanation_engine.explain_alert(first_alert)
    explanation_engine.explain_alert(second_alert)
    assert explanation_engine.cached_explanation_count() == 2


async def test_explain_alert_cache_can_be_disabled(scenario, watch_evaluator, knowledge_base, template_llm_client):
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=template_llm_client, enable_cache=False)
    alert = await _make_real_alert(scenario, watch_evaluator)
    first = engine.explain_alert(alert)
    second = engine.explain_alert(alert)
    assert first.explanation_id != second.explanation_id
    assert engine.cached_explanation_count() == 0


# ---------------------------------------------------------------------------
# 4. Proximity-based attribution guard — catches a narrative that states
#    only grounded numbers but pins one to the wrong driver.
# ---------------------------------------------------------------------------
async def _make_real_alert_with_distinct_tire_ages(scenario, watch_evaluator) -> Alert:
    """Same real Gate 1/2 pipeline as `_make_real_alert`, but forces
    `candidate_tire_age_laps` != `target_tire_age_laps` via `model_copy`
    if the canonical demo scenario happens to produce equal values at the
    alert's triggering lap — the attribution guard is a no-op (by design,
    see its docstring) when the two values are identical, so a test of it
    needs them to genuinely differ. `model_copy(update=...)` on a frozen
    pydantic model is the standard way to derive a modified copy without
    mutating the original; using it here keeps every OTHER field (candidate
    id, target id, margin, etc.) exactly as the real pipeline produced,
    so only the one thing under test changes."""
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = alert.deterministic_result
    if result.candidate_tire_age_laps == result.target_tire_age_laps:
        result = result.model_copy(update={"target_tire_age_laps": result.candidate_tire_age_laps + 4})
        alert = alert.model_copy(update={"deterministic_result": result})
    return alert


async def test_attribution_guard_flags_a_grounded_number_pinned_to_the_wrong_driver(
    scenario, watch_evaluator, knowledge_base
):
    alert = await _make_real_alert_with_distinct_tire_ages(scenario, watch_evaluator)
    result = alert.deterministic_result
    candidate, target = result.candidate, result.target
    candidate_age, target_age = result.candidate_tire_age_laps, result.target_tire_age_laps

    class _SwappedAttributionLLMClient:
        provider_name = "fake-swapped"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, grounded_facts, citations
            # Both numbers are real, grounded values — just swapped onto
            # the wrong driver. A pure numeric-membership check (the
            # pre-existing _check_grounding) would pass this narrative.
            return (
                f"{candidate}'s tires are {target_age} laps old, while "
                f"{target}'s tires are {candidate_age} laps old."
            )

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_SwappedAttributionLLMClient())
    result_obj = engine.explain_alert(alert)

    assert result_obj.grounding_warnings != ()
    assert any(candidate in w and str(target_age) in w for w in result_obj.grounding_warnings)
    assert any(target in w and str(candidate_age) in w for w in result_obj.grounding_warnings)


async def test_attribution_guard_passes_a_correctly_attributed_narrative(
    scenario, watch_evaluator, knowledge_base
):
    alert = await _make_real_alert_with_distinct_tire_ages(scenario, watch_evaluator)
    result = alert.deterministic_result
    candidate, target = result.candidate, result.target
    candidate_age, target_age = result.candidate_tire_age_laps, result.target_tire_age_laps

    class _CorrectlyAttributedLLMClient:
        provider_name = "fake-correct"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, grounded_facts, citations
            return (
                f"{candidate}'s tires are {candidate_age} laps old, while "
                f"{target}'s tires are {target_age} laps old."
            )

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_CorrectlyAttributedLLMClient())
    result_obj = engine.explain_alert(alert)

    assert result_obj.grounding_warnings == ()


async def test_attribution_guard_skips_a_number_identical_for_both_drivers(scenario, watch_evaluator, knowledge_base):
    """When candidate_tire_age_laps == target_tire_age_laps, the guard must
    not flag either mention — there is no way to tell a correct
    attribution from a swapped one for a value that's the same either way,
    so treating it as checkable would be a false positive on a
    coincidence, not a caught error (see _check_attribution's docstring)."""
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = alert.deterministic_result
    same_age = result.candidate_tire_age_laps
    result = result.model_copy(update={"target_tire_age_laps": same_age})
    alert = alert.model_copy(update={"deterministic_result": result})
    candidate, target = result.candidate, result.target

    class _AmbiguousAgeLLMClient:
        provider_name = "fake-ambiguous"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, grounded_facts, citations
            return f"Both {candidate} and {target} are running tires {same_age} laps old."

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_AmbiguousAgeLLMClient())
    result_obj = engine.explain_alert(alert)

    assert result_obj.grounding_warnings == ()


async def test_attribution_guard_skips_a_narrative_that_never_names_either_driver_id(
    scenario, watch_evaluator, knowledge_base
):
    """A narrative that refers to drivers generically ("the candidate",
    never the literal driver_id string) gives the proximity check nothing
    to anchor to, so it must not fabricate a warning out of that absence —
    it stays silent rather than guessing (see _check_attribution's
    docstring on this being a deliberately narrow, high-confidence check,
    not a general entity-linking system)."""
    alert = await _make_real_alert_with_distinct_tire_ages(scenario, watch_evaluator)
    result = alert.deterministic_result
    candidate_age, target_age = result.candidate_tire_age_laps, result.target_tire_age_laps

    class _GenericPhrasingLLMClient:
        provider_name = "fake-generic"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, grounded_facts, citations
            return (
                f"The candidate's tires are {target_age} laps old, while "
                f"the target's tires are {candidate_age} laps old."
            )

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_GenericPhrasingLLMClient())
    result_obj = engine.explain_alert(alert)

    assert result_obj.grounding_warnings == ()


async def test_attribution_guard_ignores_a_mention_far_outside_the_proximity_window(
    scenario, watch_evaluator, knowledge_base
):
    """A driver ID that appears only far away from a matching number (past
    `_ATTRIBUTION_PROXIMITY_CHARS`) must not anchor an attribution — the
    guard would rather stay silent than draw a low-confidence conclusion
    from a distant mention (see _check_attribution's docstring)."""
    alert = await _make_real_alert_with_distinct_tire_ages(scenario, watch_evaluator)
    result = alert.deterministic_result
    candidate = result.candidate
    target_age = result.target_tire_age_laps

    padding = "This sentence exists purely to push the driver mention far away from the number that follows it. " * 3

    class _DistantMentionLLMClient:
        provider_name = "fake-distant"
        model_id = "fake-v1"

        def explain(self, *, system_prompt, grounded_facts, citations) -> str:
            del system_prompt, grounded_facts, citations
            # `candidate` is named once, far from any number; `target_age`
            # (the TARGET's real value) then appears with no driver id
            # nearby at all, so there is nothing within the proximity
            # window to (mis)attribute it to.
            return f"{candidate}. {padding}The tire age figure of interest is {target_age} laps."

    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_DistantMentionLLMClient())
    result_obj = engine.explain_alert(alert)

    assert result_obj.grounding_warnings == ()


# ---------------------------------------------------------------------------
# Spec v1.1 repair 14 / Section 29 "Bedrock unavailable": an LLMClientError
# from the configured LLM client must produce the exact specified degraded
# narrative, marked `degraded=True`, and must not be cached (so a later
# request can succeed once the underlying provider recovers).
# ---------------------------------------------------------------------------


class _AlwaysFailingLLMClient:
    provider_name = "fake-bedrock-down"
    model_id = "fake-v1"

    def explain(self, *, system_prompt, grounded_facts, citations) -> str:
        del system_prompt, grounded_facts, citations
        raise LLMClientError("simulated Bedrock invoke_model failure")


class _FailsOnceThenSucceedsLLMClient:
    """Fails its first call, succeeds on every call after — used to prove
    a degraded result is not cached (a real, stable explanation IS cached;
    see the module's caching docstring)."""

    provider_name = "fake-bedrock-flaky"
    model_id = "fake-v1"

    def __init__(self):
        self.calls = 0

    def explain(self, *, system_prompt, grounded_facts, citations) -> str:
        del system_prompt, citations
        self.calls += 1
        if self.calls == 1:
            raise LLMClientError("simulated transient Bedrock failure")
        return f"Recovered narrative mentioning {grounded_facts.get('expected_margin_ms', '0')}ms."


async def test_bedrock_unavailable_produces_the_exact_degraded_narrative(
    scenario, watch_evaluator, knowledge_base
):
    alert = await _make_real_alert(scenario, watch_evaluator)
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=_AlwaysFailingLLMClient())

    result = engine.explain_alert(alert)

    assert result.narrative == BEDROCK_UNAVAILABLE_NARRATIVE
    assert result.narrative == "Strategy explanation temporarily unavailable."
    assert result.degraded is True
    assert result.grounding_warnings == ()
    # The grounded facts (deterministic numbers already computed) are
    # still returned even though the narration failed — Fire TV can keep
    # showing timing/tire/undercut data unaffected, per Section 29.
    assert result.grounded_facts["candidate"] == alert.deterministic_result.candidate


async def test_bedrock_unavailable_result_is_not_cached_so_a_retry_can_recover(
    scenario, watch_evaluator, knowledge_base
):
    alert = await _make_real_alert(scenario, watch_evaluator)
    flaky_client = _FailsOnceThenSucceedsLLMClient()
    engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=flaky_client)

    first = engine.explain_alert(alert)
    assert first.degraded is True
    assert first.narrative == BEDROCK_UNAVAILABLE_NARRATIVE
    assert engine.cached_explanation_count() == 0

    second = engine.explain_alert(alert)
    assert second.degraded is False
    assert "Recovered narrative" in second.narrative
    assert engine.cached_explanation_count() == 1

    # And now that a real (non-degraded) result exists, it IS stably
    # cached per the module's existing caching contract.
    third = engine.explain_alert(alert)
    assert third.explanation_id == second.explanation_id


async def test_ordinary_template_explanation_is_not_marked_degraded(
    scenario, watch_evaluator, explanation_engine
):
    alert = await _make_real_alert(scenario, watch_evaluator)
    result = explanation_engine.explain_alert(alert)
    assert result.degraded is False
