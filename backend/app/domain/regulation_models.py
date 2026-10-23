"""Domain models for spec v1.1 repair 8: `get_regulation`'s
found/evidence contract.

This is a deliberately narrower, stricter sibling of
`app/domain/explanation_models.py`'s `KBCitation` (Gate 3). `KBCitation` is
"whatever the retrieval ranked highest, for the LLM to read as grounding
material" — a low bar, appropriate for grounding a narrative that a human
will still read critically. `RegulationAnswer` is different: it is asked a
direct factual question ("does rule X say Y?") and must answer definitively
either with textual evidence that actually supports the query, or with an
explicit refusal (`found=False, evidence=()`), never a merely-topically-
related document offered as if it were an answer.

This is the machine-checkable half of "unsupported-information contract":
a caller (an LLM narrating a "Why?" explanation, the Fire TV Evidence/
Sources screen, or an Alexa+ voice response) can check `found` before
saying anything, and inspecting `evidence` costs nothing extra — it is
already broken out as structured fields rather than free text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SupportStatus = Literal["SUPPORTED"]
# Only "SUPPORTED" is a valid value for an *evidence item* — an item is
# only ever constructed when it actually supports the query (see
# regulation_service.py's matching threshold). There is deliberately no
# "PARTIALLY_SUPPORTED" or "CONTRADICTED" status: modeling those honestly
# would require the service to itself judge nuance, which is exactly the
# kind of inference this contract exists to avoid. A query that is not
# cleanly SUPPORTED by a specific passage is UNSUPPORTED at the
# `RegulationAnswer.found` level instead.


class RegulationEvidence(BaseModel):
    """One passage of the fictional sporting-rules corpus that supports
    the query, structured so a caller never has to parse prose to find
    out what backs an answer."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    section_id: str
    title: str
    version: str
    support_status: SupportStatus
    excerpt: str
    source_path: str


class RegulationAnswer(BaseModel):
    """The full response to a `get_regulation` query.

    `found=True` iff `evidence` is non-empty; `found=False` iff `evidence`
    is empty. Both directions are enforced by `RegulationService`, and
    re-checked by tests, rather than left as a convention callers must
    trust.

    `available` is a SEPARATE signal from `found`, added for spec v1.1
    repair 14 (Section 29 "Degraded Modes" — "Knowledge Base unavailable").
    `found=False` with `available=True` means the regulation corpus loaded
    fine and was consulted, but genuinely does not support this query — the
    normal "unsupported-information" outcome. `available=False` (which also
    always implies `found=False`) means the corpus itself could not be
    loaded at all (e.g. its directory is missing), so no query could
    possibly be answered right now; a caller MUST render the distinct
    degraded-mode message for this case ("Regulation source unavailable. I
    won't infer the rule.") rather than the ordinary unsupported-query
    response, since the two have different causes and different, honest
    things to tell the user.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    found: bool
    evidence: tuple[RegulationEvidence, ...]
    available: bool = True
