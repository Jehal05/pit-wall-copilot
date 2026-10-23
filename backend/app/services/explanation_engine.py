"""Explanation Engine — Gate 3.

Implements the "explain, don't invent" architecture principle for the
`GET /v1/races/{race_id}/alerts/{alert_id}/explain` endpoint: an LLM (real
or the deterministic template stand-in, see llm_client.py) narrates the
deterministic numbers already computed by Gate 1's strategy engine and
already persisted by Gate 2's watch evaluator — grounded_facts is built
entirely from `Alert.deterministic_result` and `Alert.triggering_snapshot`,
both of which are immutable once written (spec 49). The LLM is never asked
to compute a margin, a probability, or a lap number; it is handed those
values as text and asked only to phrase them.

The other half of that contract is machine-checked, not just
prompt-requested: after generation, `_check_grounding` extracts every
numeric token from the narrative and verifies each one traces back to a
value actually present in `grounded_facts` or the retrieved KB citations.
Anything that doesn't is recorded in `ExplanationResult.grounding_warnings`
rather than silently trusted — this is what makes it safe to point this
engine at a real, non-deterministic Bedrock model without weakening the
guarantee that a client-visible explanation never contradicts the
deterministic result it is describing.

*** Proximity-based attribution check (strengthens the grounding guard
beyond pure numeric membership) ***
`_check_grounding` alone can only prove a number was *somewhere* in the
grounded facts — it cannot catch a narrative that states only real,
grounded numbers but swaps which driver they belong to (e.g. "the
candidate's tires are 22 laps old" when 22 is actually the TARGET's tire
age, and the candidate's is really 18). Every digit involved is still
individually grounded, so the plain membership check passes even though
the sentence is now factually wrong about who has which tires. This
matters specifically here because a race engineer acts on "whose tires
are older," not just "was some number mentioned" — a swapped attribution
is a materially different, actionable claim from the correct one.
`_check_attribution` closes that gap for the driver-specific facts this
engine has (`candidate_tire_age_laps` / `target_tire_age_laps` — the only
grounded facts that are inherently "about" one specific driver rather
than the pair or the watch as a whole): it finds every mention of the
candidate's and target's driver IDs in the narrative, and for each
occurrence of one of those two values, checks that the *nearest* driver-ID
mention in the text is the one that value actually belongs to. This is
deliberately narrow, not a general entity-linking system: a value that is
identical for both drivers is skipped outright (proximity cannot
disambiguate an inherently ambiguous number, and flagging it would be a
false positive rather than a caught error), and a narrative that never
names either driver ID at all is skipped too (nothing to check proximity
against). See its own docstring for the exact algorithm and its
acknowledged blind spots.

*** Per-alert result cache (fixes the previously-documented Gate 3
limitation "No caching ... on /explain") ***
`Alert.deterministic_result` and `Alert.triggering_snapshot` are immutable
once written (spec 49), and `_build_grounded_facts` derives entirely from
them — so a given `alert_id` can only ever produce one correct
`ExplanationResult`. This engine now caches by `alert_id`: the first call
generates and grounds a narrative (an LLM call for `BedrockLLMClient`, or
a cheap deterministic render for `TemplateLLMClient`) and every subsequent
call for the same alert returns that same cached result rather than
re-generating it. This is not merely a speed optimization — it also makes
the explanation of a given alert stable across repeated reads, which
matters once a real (non-deterministic) LLM is in the loop: without a
cache, asking "explain that alert" twice could legitimately get two
differently-phrased answers for numbers that never changed. The cache is
a plain in-process dict with no eviction, which is an appropriate,
honestly-scoped choice for a single-process hackathon demo bounded by how
many alerts a demo race actually produces (typically a handful); a
production deployment with unbounded alert volume would want an LRU cap
or a TTL, noted in the README rather than implemented speculatively here.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Sequence

from app.domain.explanation_models import ExplanationResult, KBCitation
from app.domain.models import RaceState, utc_now
from app.domain.watch_models import Alert
from app.metrics import emit_bedrock_invocation
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm_client import LLMClient, LLMClientError

# Spec v1.1 repair 14 / Section 29 "Bedrock unavailable": the exact literal
# text a client SHALL see in place of a real narration when the LLM call
# fails. Fire TV continues showing everything else (timing, tire data,
# undercut calculation, alerts) unaffected — only this one field degrades.
BEDROCK_UNAVAILABLE_NARRATIVE = "Strategy explanation temporarily unavailable."

SYSTEM_PROMPT = (
    "You are the Pit Wall Copilot strategy narrator. You explain precomputed, "
    "deterministic race-strategy numbers to a race engineer. You do not compute, "
    "estimate, round differently, or invent any number — every number you state "
    "must be copied verbatim from the grounded facts you are given. If asked about "
    "something not present in the grounded facts, say you don't have that figure "
    "rather than guessing. Keep your answer to 2-4 sentences, in a spoken style "
    "suitable for a voice assistant."
)

# Negative lookbehind for a letter excludes digits glued onto a label like
# "P10" or "P90" (from "P10=-1626ms, P90=3456ms" in the template's own
# output) — without it, "10" and "90" would be extracted as if they were
# free-standing numbers and incorrectly flagged as ungrounded.
_NUMBER_RE = re.compile(r"(?<![A-Za-z])-?\d[\d,]*\.?\d*")
_SMALL_INTEGER_IGNORE_THRESHOLD = 9  # see _check_grounding docstring
_ATTRIBUTION_PROXIMITY_CHARS = 60  # see _check_attribution docstring


class ExplanationEngine:
    def __init__(self, *, knowledge_base: KnowledgeBaseService, llm_client: LLMClient, enable_cache: bool = True):
        self._kb = knowledge_base
        self._llm = llm_client
        self._enable_cache = enable_cache
        self._cache: dict[str, ExplanationResult] = {}

    def explain_alert(self, alert: Alert, *, force_refresh: bool = False) -> ExplanationResult:
        """Returns the (possibly cached) explanation for `alert`. See the
        module docstring's cache section for why caching by alert_id is
        safe: an alert's grounded facts are immutable once written, so
        there is exactly one correct explanation per alert_id, not one
        that needs to be recomputed on every read. `force_refresh=True`
        bypasses the cache and re-generates (and re-caches) — useful for
        tests and for deliberately re-rolling a Bedrock narrative."""
        if self._enable_cache and not force_refresh:
            cached = self._cache.get(alert.alert_id)
            if cached is not None:
                return cached

        grounded_facts = self._build_grounded_facts(alert)
        citations = self._kb.retrieve(self._query_keywords(alert), top_k=3)

        # AWS hackathon-final pass, phase 5: "Bedrock latency/throttling/
        # fallback" — timed and emitted here (rather than inside
        # BedrockLLMClient itself) so ONE metric emission covers every
        # LLMClient implementation uniformly, keyed by the same
        # `provider_name` ("template"/"bedrock") already surfaced to
        # judges, without needing every current/future LLMClient
        # implementation to remember to instrument itself.
        started_at = time.monotonic()
        try:
            narrative = self._llm.explain(
                system_prompt=SYSTEM_PROMPT,
                grounded_facts=grounded_facts,
                citations=citations,
            )
            degraded = False
            grounding_warnings = self._check_grounding(narrative, grounded_facts, citations)
            grounding_warnings += self._check_attribution(narrative, alert)
            emit_bedrock_invocation(
                latency_ms=(time.monotonic() - started_at) * 1000,
                throttled=False,
                fallback_used=False,
                source=self._llm.provider_name,
            )
        except LLMClientError as exc:
            # Spec v1.1 repair 14 / Section 29 "Bedrock unavailable": fail
            # explicitly with the exact specified text rather than letting
            # an LLMClientError (e.g. a real Bedrock invoke_model failure)
            # propagate as an unhandled 500. There is nothing to
            # grounding-check about a fixed literal string that names no
            # numbers, so grounding_warnings is trivially empty here — not
            # because the check was skipped, but because it cannot fail.
            narrative = BEDROCK_UNAVAILABLE_NARRATIVE
            degraded = True
            grounding_warnings = ()
            throttled = "throttl" in str(exc).lower() or "toomanyrequests" in str(exc).lower()
            emit_bedrock_invocation(
                latency_ms=(time.monotonic() - started_at) * 1000,
                throttled=throttled,
                fallback_used=True,
                source=self._llm.provider_name,
            )

        result = ExplanationResult(
            explanation_id=f"expl-{uuid.uuid4()}",
            alert_id=alert.alert_id,
            watch_id=alert.watch_id,
            race_id=alert.race_id,
            run_id=alert.run_id,
            narrative=narrative,
            citations=tuple(citations),
            grounded_facts=grounded_facts,
            llm_provider=self._llm.provider_name,
            model_id=self._llm.model_id,
            grounding_warnings=grounding_warnings,
            created_at=utc_now(),
            degraded=degraded,
        )
        # A degraded result is deliberately NOT cached: unlike a real
        # explanation (which is stable forever once generated, per the
        # module docstring's caching section — an alert's grounded facts
        # never change), "Bedrock is down right now" is a transient
        # condition. Caching this literal would permanently freeze every
        # future read of this alert into the degraded response even after
        # Bedrock recovers, which is worse than just re-attempting the
        # call on the next request.
        if self._enable_cache and not degraded:
            self._cache[alert.alert_id] = result
        return result

    def cached_explanation_count(self) -> int:
        """Number of distinct alert_ids currently cached. Exposed for
        tests/observability, not part of the public HTTP API."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Grounded-facts construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_grounded_facts(alert: Alert) -> dict[str, str]:
        result = alert.deterministic_result
        confidence = result.confidence
        snapshot: RaceState = alert.triggering_snapshot

        facts: dict[str, str] = {
            "candidate": result.candidate,
            "target": result.target,
            "previous_status": alert.previous_status.value,
            "new_status": alert.new_status.value,
            "window": result.window,
            "triggering_lap": str(snapshot.lap),
            "state_version": str(alert.state_version),
            "expected_margin_ms": str(result.expected_margin_ms),
            "comparison_horizon_lap": str(result.comparison_horizon_lap),
            "candidate_tire_age_laps": str(result.candidate_tire_age_laps),
            "target_tire_age_laps": str(result.target_tire_age_laps),
            "traffic_classification": result.traffic_classification,
            "trials": str(confidence.trials),
            "median_margin_ms": str(confidence.median_margin_ms),
            "p10_margin_ms": str(confidence.p10_margin_ms),
            "p90_margin_ms": str(confidence.p90_margin_ms),
            "probability_ahead": f"{confidence.probability_ahead * 100:.1f}%",
            "model_version": result.model_version,
        }
        if result.assumptions:
            facts["assumptions"] = "; ".join(result.assumptions)
        return facts

    @staticmethod
    def _query_keywords(alert: Alert) -> list[str]:
        keywords = ["undercut", "pit stop", "pit lane", "tire"]
        track_state = alert.triggering_snapshot.track_state.value
        if track_state != "GREEN":
            keywords.append("safety car" if track_state == "SAFETY_CAR" else "yellow flag")
        return keywords

    # ------------------------------------------------------------------
    # Grounding guard
    # ------------------------------------------------------------------
    @staticmethod
    def _check_grounding(
        narrative: str, grounded_facts: dict[str, str], citations: Sequence[KBCitation]
    ) -> tuple[str, ...]:
        """Extracts every numeric token from `narrative` and flags any that
        cannot be traced back to a number present in `grounded_facts` or
        the retrieved `citations`. Small integers (0-9) are ignored to
        avoid flagging ordinary phrasing (list counts, "a couple of laps",
        etc.) as ungrounded — this keeps the guard usable against a real
        generative model's natural phrasing rather than only the exact
        template client, while still catching any materially wrong
        figure (a margin, a percentage, a lap number) a hallucinating
        model might state.
        """
        allowed = ExplanationEngine._allowed_numeric_tokens(grounded_facts, citations)
        warnings: list[str] = []
        for match in _NUMBER_RE.finditer(narrative):
            raw_token = match.group()
            token = raw_token.replace(",", "")
            if token in ("", "-"):
                continue
            try:
                value = float(token)
            except ValueError:
                continue
            if abs(value) <= _SMALL_INTEGER_IGNORE_THRESHOLD and "." not in token:
                continue
            normalized = str(int(value)) if value == int(value) else token
            if token not in allowed and normalized not in allowed:
                warnings.append(f"narrative contains ungrounded number: {raw_token}")
        return tuple(warnings)

    @staticmethod
    def _allowed_numeric_tokens(grounded_facts: dict[str, str], citations: Sequence[KBCitation]) -> set[str]:
        allowed: set[str] = set()
        texts = list(grounded_facts.values()) + [c.excerpt for c in citations] + [c.title for c in citations]
        for text in texts:
            for match in _NUMBER_RE.finditer(text):
                token = match.group().replace(",", "")
                # Comparison against a literal placeholder character, not a credential.
                if not token or token == "-":  # nosec B105
                    continue
                allowed.add(token)
                try:
                    value = float(token)
                    if value == int(value):
                        allowed.add(str(int(value)))
                except ValueError:
                    pass
        return allowed

    # ------------------------------------------------------------------
    # Attribution guard (catches a grounded number pinned to the wrong
    # driver — see the module docstring's "Proximity-based attribution
    # check" section for why this is a distinct failure mode from an
    # ungrounded number).
    # ------------------------------------------------------------------
    @staticmethod
    def _check_attribution(narrative: str, alert: Alert) -> tuple[str, ...]:
        """Flags a narrative that states one of this alert's driver-specific
        numbers (`candidate_tire_age_laps` / `target_tire_age_laps` — the
        only grounded facts inherently "about" one specific driver rather
        than the watch or the pair as a whole) closer to the OTHER
        driver's ID than to the one it actually belongs to.

        Algorithm: find every occurrence of the candidate's and target's
        driver IDs in the narrative; for every numeric token that equals
        one of the two attributable values, find the nearest driver-ID
        occurrence (by character distance, either direction) and compare
        it to the value's true owner. A mismatch within
        `_ATTRIBUTION_PROXIMITY_CHARS` characters is flagged.

        Deliberately conservative, by design, in three ways that trade
        recall for avoiding false positives on a machine-safety guard:
          - A value identical for both drivers is skipped outright — with
            no numeric difference to key off, proximity cannot tell a
            correct attribution from a swapped one, so treating it as
            checkable would produce a false positive on a coincidence,
            not catch a real error.
          - A numeric match with no driver-ID mention within
            `_ATTRIBUTION_PROXIMITY_CHARS` characters on either side is
            skipped — far enough away that the sentence may not be
            attributing the number to a driver at all (e.g. a summary
            sentence stating a lap number), and guessing an attribution
            from a distant mention would be more likely to be a false
            positive than a caught error.
          - It only checks the two driver-specific tire-age facts. It is
            not a general entity-linking system and does not (for
            instance) try to disambiguate which of two probabilities in a
            sentence belongs to which driver when both numbers are
            present but neither driver ID is repeated near each one —
            that would require actual language understanding, which is
            exactly what this deterministic guard is not attempting; it
            catches the narrow, high-confidence case of "a real number,
            near the wrong driver's name."
        """
        result = alert.deterministic_result
        candidate_id = result.candidate
        target_id = result.target
        if candidate_id == target_id:
            return ()  # degenerate watch; nothing to disambiguate

        attributable: list[tuple[int, str]] = []
        if result.candidate_tire_age_laps != result.target_tire_age_laps:
            attributable.append((result.candidate_tire_age_laps, candidate_id))
            attributable.append((result.target_tire_age_laps, target_id))
        if not attributable:
            return ()

        driver_positions: list[tuple[int, str]] = []
        for driver_id in (candidate_id, target_id):
            start = 0
            while True:
                idx = narrative.find(driver_id, start)
                if idx == -1:
                    break
                driver_positions.append((idx, driver_id))
                start = idx + len(driver_id)
        if not driver_positions:
            return ()  # narrative never names either driver; nothing to check proximity against

        warnings: list[str] = []
        for match in _NUMBER_RE.finditer(narrative):
            token = match.group().replace(",", "")
            if token in ("", "-"):
                continue
            try:
                value = float(token)
            except ValueError:
                continue
            if value != int(value):
                continue
            int_value = int(value)
            for expected_value, expected_driver in attributable:
                if int_value != expected_value:
                    continue
                nearest_pos, nearest_driver = min(
                    driver_positions, key=lambda p: abs(p[0] - match.start())
                )
                if abs(nearest_pos - match.start()) > _ATTRIBUTION_PROXIMITY_CHARS:
                    continue
                if nearest_driver != expected_driver:
                    warnings.append(
                        f"narrative appears to attribute {expected_driver}'s figure "
                        f"({int_value}) to {nearest_driver} instead"
                    )
        return tuple(warnings)
