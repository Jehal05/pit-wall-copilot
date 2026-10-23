"""Tests for spec v1.1 repair 8: `RegulationService.get_regulation`'s
found/evidence contract.

Two groups of tests:

1. SUPPORTED queries — natural-language questions that a fact in
   `docs/sporting-rules/regulations/*.md` actually answers. These assert
   `found=True`, non-empty `evidence`, and that the returned evidence's
   `document_id`/`section_id`/`title`/`version`/`support_status` are all
   populated and correct.

2. UNSUPPORTED queries (>= 20, per the spec's explicit requirement) — a
   mix of: (a) the canonical hallucination trap ("the tire rule after lap
   40"), (b) queries about real-world motorsport regulations that this
   fictional corpus never claims to model (DRS, parc fermé, points
   systems, grid penalties, ...), (c) queries that are topically adjacent
   to a real fact but ask something that fact does not say (a specific
   fabricated number, an inverted claim, a fabricated exception), and (d)
   queries entirely unrelated to racing. All must return `found=False`
   and `evidence=()`.
"""

from __future__ import annotations

import re

import pytest

from app.config import DEFAULT_KNOWLEDGE_BASE_DIR, DEFAULT_REGULATIONS_DIR
from app.services.regulation_service import RegulationService


@pytest.fixture(scope="module")
def regulation_service() -> RegulationService:
    return RegulationService(DEFAULT_REGULATIONS_DIR, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)


def test_corpus_loads_all_authored_facts(regulation_service: RegulationService):
    # 3 tire facts + 2 pit-lane facts + 2 track-status facts + 3 undercut
    # facts = 10, at the time this test was written. This is a sanity
    # check that the corpus parser found every fact, not an assertion the
    # corpus can never grow — if it does, bump this number too.
    assert regulation_service.fact_count == 10


# ---------------------------------------------------------------------------
# SUPPORTED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected_document_id",
    [
        ("What tire compounds are available, soft medium or hard?", "reg-tires-001"),
        ("Does tire age reset to zero after a pit stop with a new set?", "reg-tires-001"),
        ("How does tire degradation change as tire age grows across laps?", "reg-tires-001"),
        ("What are the four components of pit loss: pit entry, lane transit, stationary time, pit exit?", "reg-pitlane-001"),
        ("What is tire warm-up loss on the out-lap after a pit stop, cold tires?", "reg-pitlane-001"),
        ("What do green flag, yellow flag, safety car and red flag track status mean?", "reg-trackstatus-001"),
        ("Why does a safety car reduce pit loss cost and compress the field?", "reg-trackstatus-001"),
        ("What is an undercut — pits first for fresh tires to emerge ahead of a rival?", "reg-undercut-001"),
        ("What factors set the undercut window: gap before the stop, degradation rate, pit stop cost?", "reg-undercut-001"),
        ("What is the overcut counter-strategy — stays out extra laps on older tires?", "reg-undercut-001"),
    ],
)
def test_supported_query_returns_found_true_with_matching_document(
    regulation_service: RegulationService, query: str, expected_document_id: str
):
    answer = regulation_service.get_regulation(query)
    assert answer.found is True
    assert len(answer.evidence) >= 1
    assert answer.evidence[0].document_id == expected_document_id
    assert answer.evidence[0].support_status == "SUPPORTED"
    assert answer.evidence[0].version == "1.0"
    assert answer.evidence[0].section_id
    assert answer.evidence[0].title
    assert answer.evidence[0].excerpt
    assert answer.query == query


@pytest.mark.parametrize(
    "query",
    [
        "pit lane speed limit",
        "pit-lane speed",
        "mandatory pit limit",
        "Pit-Lane Speed Limit?",
        "PIT LANE SPEED LIMIT",
        "  pit   lane   speed   limit  ",
        "What is the pit entry speed requirement?",
        "Is there a mandatory pit lane speed limit?",
    ],
)
def test_pit_lane_speed_limit_query_is_found_local_demo_repair_issue_3(
    regulation_service: RegulationService, query: str
):
    """Local-demo-fixed repair, issue 3: the knowledge corpus already
    states (in `reg-pitlane-001-components`'s prose) that a mandatory
    pit-lane speed limit applies during pit entry and lane transit - it
    just previously lacked keyword coverage and was punctuation-sensitive
    enough that none of these natural phrasings matched. This must now
    find that fact for every wording/case/punctuation variant above,
    without a numeric speed value ever being invented."""
    answer = regulation_service.get_regulation(query)
    assert answer.found is True, f"expected found=True for: {query!r}"
    assert len(answer.evidence) >= 1
    assert answer.evidence[0].document_id == "reg-pitlane-001"
    assert answer.evidence[0].section_id == "reg-pitlane-001-components"
    assert answer.evidence[0].source_path == "docs/sporting-rules/regulations/pit-lane.md"
    # Honesty requirement: the corpus states a mandatory limit exists but
    # never gives it a numeric value, and this fix must not invent one.
    excerpt = answer.evidence[0].excerpt
    assert "mandatory" in excerpt.lower()
    assert "speed limit" in excerpt.lower()
    assert not re.search(r"\d+\s*(km/?h|kph|mph)", excerpt.lower())


def test_pit_lane_speed_limit_fix_does_not_reopen_the_safety_car_numeric_trap(
    regulation_service: RegulationService,
):
    """Regression guard for the exact collision this fix's first attempt
    caused: blurring hyphen/space forms during normalization made
    "safety-car" collide with the "safety car" keyword and, combined with
    "pit loss", spuriously satisfy the trackstatus safety-car/pit-loss
    fact for a query that invents a specific numeric percentage this
    corpus never states. That must still correctly return found=False."""
    answer = regulation_service.get_regulation(
        "Is there a fixed safety-car pit loss reduction percentage of 40 percent?"
    )
    assert answer.found is False
    assert answer.evidence == ()


def test_supported_evidence_tuple_is_empty_iff_not_found(regulation_service: RegulationService):
    found_answer = regulation_service.get_regulation("What tire compounds are available, soft medium hard?")
    assert found_answer.found is True
    assert len(found_answer.evidence) > 0

    not_found_answer = regulation_service.get_regulation("What is the weather forecast for tomorrow?")
    assert not_found_answer.found is False
    assert not_found_answer.evidence == ()


# ---------------------------------------------------------------------------
# UNSUPPORTED — the canonical hallucination trap, plus >= 19 more.
# ---------------------------------------------------------------------------

UNSUPPORTED_QUERIES = [
    # (a) The canonical hallucination trap named explicitly in the spec:
    # an LLM asked this in isolation might invent an answer ("yes, after
    # lap 40 degradation is capped" or similar) because it pattern-matches
    # real-world tire-rule complexity. Nothing in this corpus says any
    # such thing — tire-compounds.md's degradation fact explicitly states
    # there is no special-cased lap number.
    "Does the tire degradation rule change or get ignored after lap 40?",
    "Is there a rule that tire wear stops being calculated after lap 40?",
    "After lap 40, do tires stop degrading under the regulations?",
    # (b) Real-world motorsport concepts this fictional corpus never
    # mentions at all — a real regulation body, but not this one.
    "What are the DRS activation rules?",
    "How does parc ferme restrict setup changes between qualifying and the race?",
    "What is the point system for race finishing positions?",
    "What is the grid penalty for an unscheduled gearbox change?",
    "How many free practice sessions are permitted before qualifying?",
    "What is the minimum weight limit for the car and driver combined?",
    "What fuel flow rate limit applies during the race?",
    "How does the tire blanket temperature regulation work?",
    "What is the procedure for a red flag restart from the pit lane?",
    "Is there a curfew restricting when teams can work on the cars overnight?",
    # (c) Topically adjacent to a real fact but asking about a specific
    # fabricated numeric value or exception the corpus does not contain.
    "Is there a mandatory pit loss penalty of exactly 25 seconds applied to every stop?",
    "Is there a tire age carryover rule that only resets tire age after two pit stops?",
    "What is the blue flag procedure for backmarkers and lapped traffic?",
    "Is there a fixed safety-car pit loss reduction percentage of 40 percent?",
    "Is there a specific numeric tire warm-up loss penalty of 1.2 seconds defined in the rules?",
    "Is there a mandatory minimum number of pit stops required per race?",
    "What is the maximum permitted number of undercut attempts per race?",
    "Is there a cool-down lap requirement before a driver may attempt an overcut?",
    # (d) Entirely unrelated to racing or this corpus.
    "What is the capital of France?",
    "How do I bake a chocolate cake?",
    "What is the current exchange rate between USD and EUR?",
]


def test_at_least_twenty_unsupported_queries_are_declared():
    # Guards against someone silently trimming the list below the spec's
    # explicit "at least 20 unsupported-rule test cases" requirement.
    assert len(UNSUPPORTED_QUERIES) >= 20


@pytest.mark.parametrize("query", UNSUPPORTED_QUERIES)
def test_unsupported_query_returns_found_false_with_no_evidence(
    regulation_service: RegulationService, query: str
):
    answer = regulation_service.get_regulation(query)
    assert answer.found is False, f"expected found=False for unsupported query: {query!r}"
    assert answer.evidence == (), f"expected no evidence for unsupported query: {query!r}"
    assert answer.query == query


def test_empty_query_is_unsupported_not_an_error():
    service = RegulationService(DEFAULT_REGULATIONS_DIR, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)
    answer = service.get_regulation("")
    assert answer.found is False
    assert answer.evidence == ()


# ---------------------------------------------------------------------------
# Availability (spec v1.1 repair 14 / Section 29 "Knowledge Base
# unavailable") — a SEPARATE signal from found/evidence. See
# RegulationAnswer's and RegulationService.is_available's docstrings for
# why this must not be conflated with an ordinary found=False.
# ---------------------------------------------------------------------------


def test_available_service_reports_available_true(regulation_service: RegulationService):
    assert regulation_service.is_available is True


def test_available_service_marks_every_answer_available_true(regulation_service: RegulationService):
    # Both an ordinary SUPPORTED and an ordinary UNSUPPORTED answer from a
    # working corpus must carry available=True — "unavailable" is reserved
    # for when the corpus itself could not be loaded at all.
    supported = regulation_service.get_regulation("What tire compounds are available, soft medium or hard?")
    assert supported.found is True
    assert supported.available is True

    unsupported = regulation_service.get_regulation("What is the capital of France?")
    assert unsupported.found is False
    assert unsupported.available is True


def test_missing_corpus_directory_is_reported_as_unavailable(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    service = RegulationService(missing_dir, repo_root=tmp_path)

    assert service.is_available is False
    assert service.fact_count == 0

    answer = service.get_regulation("What tire compound gives the least degradation?")
    assert answer.found is False
    assert answer.evidence == ()
    assert answer.available is False


def test_unavailable_service_empty_query_is_also_unavailable(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    service = RegulationService(missing_dir, repo_root=tmp_path)

    answer = service.get_regulation("")
    assert answer.found is False
    assert answer.available is False
