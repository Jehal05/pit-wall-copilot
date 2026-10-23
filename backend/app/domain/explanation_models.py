"""LLM-explanation domain models — Gate 3.

Implements the "explain, don't invent" contract described in the product
spec's architecture principle: the LLM narrates deterministic numbers it is
handed, it never computes race state, sporting rules or strategy
mathematics itself. Nothing in this module is written by an LLM; these are
plain, testable request/response shapes for the explanation feature.

`ExplanationResult.grounding_warnings` is the machine-checkable half of
that contract: `app/services/explanation_engine.py` extracts every numeric
token the LLM's narrative contains and flags any that do not correspond to
a value it was actually given. An empty tuple means every number in the
narrative was traced back to a deterministic fact or citation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class KBCitation(BaseModel):
    """One retrieved knowledge-base document, offered to the LLM as
    grounding material and returned to the caller so a client can show its
    source (spec 19 / 50: explanations should be able to cite the rules
    they rely on)."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    title: str
    source_path: str
    excerpt: str
    score: int  # simple keyword-overlap ranking score, see knowledge_base.py


class ExplanationResult(BaseModel):
    """The output of explaining one alert in natural language."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    explanation_id: str
    alert_id: str
    watch_id: str
    race_id: str
    run_id: str
    narrative: str
    citations: tuple[KBCitation, ...]
    # Human-readable rendering of every deterministic fact the LLM was
    # given, keyed by field name — e.g. {"expected_margin_ms": "910",
    # "probability_ahead": "70.5%"}. This is what "grounded" means here:
    # the LLM receives exactly this dict (plus the citations) and nothing
    # else, and is instructed to narrate only these values.
    grounded_facts: dict[str, str]
    llm_provider: str  # "template" (default, deterministic) | "bedrock"
    model_id: str
    grounding_warnings: tuple[str, ...]
    created_at: datetime
    # Spec v1.1 repair 14 / Section 29 "Bedrock unavailable": True iff the
    # configured LLMClient raised LLMClientError and `narrative` was
    # replaced with the literal degraded-mode text "Strategy explanation
    # temporarily unavailable." instead of a real narration. False (the
    # default) for every ordinary explanation, including ones from the
    # deterministic TemplateLLMClient, which cannot itself become
    # "unavailable" the way a real network call to Bedrock can.
    degraded: bool = False
