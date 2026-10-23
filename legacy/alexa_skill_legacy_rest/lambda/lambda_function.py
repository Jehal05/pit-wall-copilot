"""Pit Wall Copilot Alexa+ skill — Gate 4.

*** LEGACY / OPTIONAL FALLBACK — NOT THE PRIMARY ALEXA+ INTEGRATION ***
Spec v1.1 repair 10 requires the PRIMARY voice integration to be:

    Alexa+  --Streamable HTTP MCP-->  five domain tools  -->  backend

i.e. `alexa_plus_client/` (a real MCP client, run against the real
`mcp_server/` Streamable HTTP transport) at the repository root — see
`../../alexa_plus_client/README.md`. THIS package is the traditional,
pre-repair-10 "classic Alexa Skill" approach: an ask-sdk-core Lambda
handler that calls the backend's REST API DIRECTLY, duplicating
orchestration logic that the MCP tools in `mcp_server/server.py` already
provide. Per spec v1.1 repair 10 ("retain the legacy Alexa Skill only as
an explicitly optional fallback under a clearly named legacy/experimental
directory"), it has been moved to `legacy/alexa_skill_legacy_rest/`
(from its original location at the repository root, `alexa_skill/`) and
must not be presented as, or confused with, the required MCP-based
integration. It is kept only because it is still a real, tested,
functioning Lambda handler that some future consumer might prefer for a
classic (non-agentic) Alexa Skill deployment — not because it is
recommended.

An AWS Lambda handler (ask-sdk-core) that turns voice intents into calls
against the backend REST API directly — this module is a thin adapter
with no strategy math, no watch/alert logic, and no LLM prompting of its
own. Every spoken number this handler says comes verbatim from the
backend's deterministic response (or, for ExplainLatestAlertIntent, from
Gate 3's already grounding-checked narrative) — it is never computed
here.

*** IMPORTANT — HONEST STATUS ***
This handler, and the interaction model in `../models/interaction-model.json`,
are implemented against the documented ask-sdk-core / Alexa Skills Kit
APIs and are covered by `../tests/test_lambda_function.py`, which
constructs real `ask_sdk_model` request envelopes (including simulated
slot-resolution results) and asserts on the real response the handlers
build. They have **not** been deployed to a real Alexa+ skill, registered
in the Alexa Developer Console, or exercised against a real device or
voice input — there is no AWS/Alexa account available in this environment
to do so (the same constraint already documented for Bedrock and for
Fire TV/Android device testing in earlier gates). See `../../README.md`
§7b for exactly what a real deployment would require.

Environment variables (mirrors mcp_server/server.py's env vars):
    PIT_WALL_API_BASE_URL       default "http://localhost:8000"
    PIT_WALL_DEMO_ACCESS_TOKEN  default "local-dev-demo-token"
    PIT_WALL_RACE_ID            default "demo-race-001"
"""

import logging
import os
from typing import Any, Optional

import requests
from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
)
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import is_intent_name, is_request_type
from ask_sdk_model import Response

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("PIT_WALL_API_BASE_URL", "http://localhost:8000")
DEMO_ACCESS_TOKEN = os.environ.get("PIT_WALL_DEMO_ACCESS_TOKEN", "local-dev-demo-token")
RACE_ID = os.environ.get("PIT_WALL_RACE_ID", "demo-race-001")

AUTH_HEADERS = {"X-Demo-Access-Token": DEMO_ACCESS_TOKEN}

# Fallback word -> canonical driver id map, used only when Alexa's slot-type
# resolution did not return an ER_SUCCESS_MATCH (e.g. the person's phrasing
# didn't match a defined synonym). Real slot resolution (see
# models/interaction-model.json's DRIVER_ID custom type) is the primary
# path; this is a documented safety net, not the main mechanism.
_FALLBACK_WORD_TO_DRIVER_ID = {
    "three": "CAR_03",
    "3": "CAR_03",
    "four": "CAR_04",
    "4": "CAR_04",
    "seven": "CAR_07",
    "7": "CAR_07",
    "eleven": "CAR_11",
    "11": "CAR_11",
}


class PitWallApiError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Pit Wall Copilot API error {status_code}: {detail}")


def _call_api(method: str, path: str, *, json_body: Optional[dict] = None, auth: bool = False) -> Any:
    headers = AUTH_HEADERS if auth else None
    response = requests.request(method, f"{BASE_URL}{path}", json=json_body, headers=headers, timeout=10)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise PitWallApiError(response.status_code, detail)
    return response.json()


def _resolve_driver_id(handler_input: HandlerInput, slot_name: str) -> Optional[str]:
    """Resolves a DRIVER_ID slot to a canonical driver_id (e.g. "CAR_04"),
    preferring Alexa's own slot-type resolution and falling back to a
    simple word/digit match on the raw utterance text (see module-level
    comment)."""
    intent = handler_input.request_envelope.request.intent
    slot = intent.slots.get(slot_name) if intent.slots else None
    if slot is None:
        return None

    if slot.resolutions and slot.resolutions.resolutions_per_authority:
        for authority in slot.resolutions.resolutions_per_authority:
            if authority.status and authority.status.code == "ER_SUCCESS_MATCH" and authority.values:
                return authority.values[0].value.id

    raw_value = (slot.value or "").lower()
    for word, driver_id in _FALLBACK_WORD_TO_DRIVER_ID.items():
        if word in raw_value:
            return driver_id
    return None


def _get_session_attributes(handler_input: HandlerInput) -> dict:
    """Returns the current session attributes, or {} for an out-of-session
    request (ask-sdk-core raises AttributesManagerException for those —
    every real LaunchRequest/IntentRequest carries a session, but this
    keeps handlers safe rather than falling through to the generic
    apology on what is really just "no session-scoped watch yet")."""
    if handler_input.request_envelope.session is None:
        return {}
    return handler_input.attributes_manager.session_attributes


def _set_session_attributes(handler_input: HandlerInput, attributes: dict) -> None:
    if handler_input.request_envelope.session is not None:
        handler_input.attributes_manager.session_attributes = attributes


# Gate 5: multiple concurrently-watched cars per session, with slot-based
# disambiguation — extends the Gate 4 fix (which only ever remembered the
# single most-recently-created watch). Capped so a very long session can't
# grow this unboundedly; FIFO eviction of the oldest tracked watch once the
# cap is hit is a reasonable, documented simplification for a hackathon
# demo session, not a claim that this scales to production traffic.
_MAX_TRACKED_WATCHES_PER_SESSION = 10


def _remember_watch(handler_input: HandlerInput, watch_id: str, candidate: str, target: str) -> None:
    session_attributes = _get_session_attributes(handler_input)
    # dict preserves insertion order (Python 3.7+, and JSON round-trips it
    # the same way), which `_find_watch_by_driver` below relies on to find
    # the MOST RECENTLY created matching watch.
    watches: dict = dict(session_attributes.get("watches") or {})
    watches[watch_id] = {"candidate": candidate, "target": target}
    if len(watches) > _MAX_TRACKED_WATCHES_PER_SESSION:
        oldest_watch_id = next(iter(watches))
        del watches[oldest_watch_id]
    session_attributes["watches"] = watches
    session_attributes["last_watch_id"] = watch_id
    _set_session_attributes(handler_input, session_attributes)


def _find_watch_by_driver(session_attributes: dict, driver_id: str) -> Optional[tuple[str, str, str]]:
    """Returns (watch_id, candidate, target) for the session watch whose
    candidate or target matches `driver_id` — e.g. "explain the car four
    watch" should find the watch involving CAR_04, whichever session watch
    that is, not just whatever was created most recently. Prefers a
    CANDIDATE match (the driver someone names is almost always "the car
    I'm trying to undercut with", not the target they're undercutting),
    and if more than one watch matches, prefers the most recently created
    one rather than raising an ambiguous-request error back at the person
    — a reasonable, documented simplification for this gate's scope."""
    watches = session_attributes.get("watches") or {}
    if not watches:
        return None
    candidate_matches = [(wid, w) for wid, w in watches.items() if w.get("candidate") == driver_id]
    target_matches = [(wid, w) for wid, w in watches.items() if w.get("target") == driver_id]
    matches = candidate_matches or target_matches
    if not matches:
        return None
    watch_id, watch = matches[-1]
    return watch_id, watch.get("candidate"), watch.get("target")


def _speakable_driver_name(driver_id: str) -> str:
    """"CAR_04" -> "car 4" (not "car 04", which some TTS engines read as
    "car zero four")."""
    number = driver_id.replace("CAR_", "").lstrip("0") or "0"
    return f"car {number}"


def _speak(handler_input: HandlerInput, text: str, *, end_session: bool = False, reprompt: Optional[str] = None) -> Response:
    builder = handler_input.response_builder.speak(text).set_should_end_session(end_session)
    if reprompt:
        builder = builder.ask(reprompt)
    return builder.response


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        return _speak(
            handler_input,
            "Welcome to Pit Wall Copilot. Ask me to check an undercut, watch two cars, "
            "or explain the latest alert.",
            reprompt="You can say, check the undercut for car four against car three.",
        )


class CheckUndercutIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("CheckUndercutIntent")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        candidate = _resolve_driver_id(handler_input, "CandidateDriver")
        target = _resolve_driver_id(handler_input, "TargetDriver")
        if not candidate or not target:
            return _speak(
                handler_input,
                "I didn't catch which two cars you meant. Try, check the undercut for car four against car three.",
                reprompt="Which two cars should I check?",
            )
        try:
            result = _call_api(
                "POST",
                f"/v1/races/{RACE_ID}/strategy/undercut",
                json_body={"candidate_driver": candidate, "target_driver": target},
            )
        except PitWallApiError as exc:
            return _speak(handler_input, _api_error_to_speech(exc), end_session=True)

        window = result["window"]
        margin_ms = result["expected_margin_ms"]
        probability_pct = round(result["confidence"]["probability_ahead"] * 100, 1)
        window_phrase = "open" if window == "OPEN" else "closed"
        margin_phrase = f"{abs(margin_ms)} milliseconds " + ("ahead" if margin_ms >= 0 else "behind")
        speech = (
            f"The undercut window for {_speakable_driver_name(candidate)} against "
            f"{_speakable_driver_name(target)} is {window_phrase}. The simulation projects "
            f"{_speakable_driver_name(candidate)} finishing {margin_phrase}, with a "
            f"{probability_pct} percent chance across two hundred simulated trials."
        )
        return _speak(handler_input, speech, end_session=True)


class CreateWatchIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("CreateWatchIntent")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        candidate = _resolve_driver_id(handler_input, "CandidateDriver")
        target = _resolve_driver_id(handler_input, "TargetDriver")
        if not candidate or not target:
            return _speak(
                handler_input,
                "I didn't catch which two cars you meant. Try, watch the undercut for car four against car three.",
                reprompt="Which two cars should I watch?",
            )
        try:
            result = _call_api(
                "POST",
                f"/v1/races/{RACE_ID}/watches",
                json_body={"candidate_driver": candidate, "target_driver": target},
                auth=True,
            )
        except PitWallApiError as exc:
            return _speak(handler_input, _api_error_to_speech(exc), end_session=True)

        # Remember which watch this conversation is about, so a later
        # "explain that" / "explain the latest alert" / "explain the car
        # four watch" in the SAME session can refer to this watch
        # specifically rather than always falling back to the single
        # newest alert race-wide (Gate 4's fix), and — since Gate 5 —
        # multiple watches created in the same session are ALL remembered,
        # not just the latest one (see _find_watch_by_driver).
        _remember_watch(handler_input, result["watch_id"], candidate, target)

        status_phrase = "already open" if result["status"] == "OPEN" else "currently closed"
        speech = (
            f"Watching {_speakable_driver_name(candidate)} against {_speakable_driver_name(target)}. "
            f"The undercut window is {status_phrase}. I'll have an alert ready the moment that changes. "
            "Just ask me to explain the latest alert once it fires."
        )
        # Session stays open (should_end_session=False) specifically so
        # last_watch_id survives into a follow-up turn — ending the
        # session here would discard it, since Alexa starts a brand-new,
        # attribute-free session on the person's next request.
        return _speak(handler_input, speech, end_session=False, reprompt="Anything else?")


class ExplainLatestAlertIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("ExplainLatestAlertIntent")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        session_attributes = _get_session_attributes(handler_input)

        # Gate 5: an optional WatchDriver slot lets a multi-watch session
        # disambiguate ("explain the car four watch") instead of always
        # meaning "the watch I most recently created" (Gate 4's behavior,
        # still the fallback below when no slot is given/resolved).
        watch_driver_id = _resolve_driver_id(handler_input, "WatchDriver")
        watch_id: Optional[str] = None
        candidate: Optional[str] = None
        target: Optional[str] = None

        if watch_driver_id:
            found = _find_watch_by_driver(session_attributes, watch_driver_id)
            if found is None:
                return _speak(
                    handler_input,
                    f"I don't have a watch open on {_speakable_driver_name(watch_driver_id)} this session. "
                    "Try, watch the undercut for that car against another one, first.",
                    end_session=True,
                )
            watch_id, candidate, target = found
        else:
            watch_id = session_attributes.get("last_watch_id")
            if watch_id:
                watches = session_attributes.get("watches") or {}
                watch = watches.get(watch_id, {})
                candidate = watch.get("candidate")
                target = watch.get("target")

        # Prefer the watch this conversation already set up (tracked via
        # session attributes by CreateWatchIntentHandler above); only fall
        # back to the single race-wide newest alert when nothing in this
        # session points at a specific watch — e.g. the person asks
        # straight after launch, without having created a watch first.
        alerts_path = f"/v1/races/{RACE_ID}/alerts"
        if watch_id:
            alerts_path += f"?watch_id={watch_id}"

        try:
            alerts = _call_api("GET", alerts_path)
        except PitWallApiError as exc:
            return _speak(handler_input, _api_error_to_speech(exc), end_session=True)

        if not alerts:
            if watch_id and candidate and target:
                message = (
                    f"The watch on {_speakable_driver_name(candidate)} "
                    f"against {_speakable_driver_name(target)} "
                    "hasn't changed status yet. I'll have something to explain as soon as it does."
                )
            else:
                message = "There are no alerts yet. Ask me to watch an undercut first, and I'll let you know when it changes."
            return _speak(handler_input, message, end_session=True)

        latest_alert_id = alerts[0]["alert_id"]
        try:
            explanation = _call_api("GET", f"/v1/races/{RACE_ID}/alerts/{latest_alert_id}/explain")
        except PitWallApiError as exc:
            return _speak(handler_input, _api_error_to_speech(exc), end_session=True)

        return _speak(handler_input, explanation["narrative"], end_session=True)


def _api_error_to_speech(exc: PitWallApiError) -> str:
    if exc.status_code == 422:
        return "I couldn't run that calculation — one of those cars isn't in the current race, or the candidate isn't behind the target. Try two different, currently-racing cars."
    if exc.status_code == 404:
        return "I couldn't find that. The race or alert may not exist yet."
    return "Something went wrong reaching Pit Wall Copilot's strategy engine. Please try again in a moment."


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        return _speak(
            handler_input,
            "You can say, check the undercut for car four against car three; or, watch car four against car "
            "seven; or, explain the latest alert.",
            reprompt="What would you like to do?",
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("AMAZON.CancelIntent")(handler_input) or is_intent_name("AMAZON.StopIntent")(
            handler_input
        )

    def handle(self, handler_input: HandlerInput) -> Response:
        return _speak(handler_input, "Goodbye.", end_session=True)


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        return _speak(
            handler_input,
            "Sorry, I didn't understand that. You can ask me to check an undercut, watch two cars, or "
            "explain the latest alert.",
            reprompt="What would you like to do?",
        )


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input: HandlerInput) -> bool:
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input: HandlerInput) -> Response:
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input: HandlerInput, exception: Exception) -> bool:
        return True

    def handle(self, handler_input: HandlerInput, exception: Exception) -> Response:
        logger.exception("Unhandled exception in Pit Wall Copilot skill")
        return _speak(
            handler_input,
            "Sorry, something went wrong. Please try again.",
            end_session=True,
        )


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(CheckUndercutIntentHandler())
sb.add_request_handler(CreateWatchIntentHandler())
sb.add_request_handler(ExplainLatestAlertIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

handler = sb.lambda_handler()  # AWS Lambda entry point: lambda_function.handler
