"""Unit tests for the Alexa+ Lambda handler.

These construct REAL `ask_sdk_model` request envelopes — including
simulated slot-type resolutions, the same shape Alexa's NLU would send —
and dispatch them through the real `ask_sdk_core` SkillBuilder built in
lambda_function.py, then assert on the real Response object it builds.
The only thing mocked is the outbound `requests` call to the Pit Wall
Copilot backend (via `monkeypatch`), so there is no dependency on a
running server or real AWS/Alexa infrastructure — see
lambda_function.py's module docstring for what this does and doesn't
prove about real-device readiness.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from ask_sdk_model import (
    Intent,
    IntentRequest,
    LaunchRequest,
    RequestEnvelope,
    Session,
    SessionEndedRequest,
    Slot,
)
from ask_sdk_model.slu.entityresolution import (
    Resolutions,
    Resolution,
    Status,
    ValueWrapper,
    Value,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda"))

import lambda_function  # noqa: E402


def _driver_slot(name: str, raw_value: str, driver_id: str | None) -> Slot:
    if driver_id is None:
        return Slot(name=name, value=raw_value)
    resolutions = Resolutions(
        resolutions_per_authority=[
            Resolution(
                status=Status(code="ER_SUCCESS_MATCH"),
                values=[ValueWrapper(value=Value(id=driver_id, name=raw_value))],
            )
        ]
    )
    return Slot(name=name, value=raw_value, resolutions=resolutions)


def _session(attributes: dict | None = None, *, new: bool = True) -> Session:
    """A realistic Alexa Session object. Every real LaunchRequest/IntentRequest
    carries one of these; earlier versions of these test helpers omitted it
    entirely, which meant the tests never exercised (and didn't catch a bug
    in) the session-attribute-based multi-turn tracking added to
    CreateWatchIntentHandler / ExplainLatestAlertIntentHandler."""
    return Session(
        new=new,
        session_id="session-1",
        application=None,
        attributes=attributes or {},
        user=None,
    )


def _intent_envelope(
    intent_name: str,
    slots: dict[str, Slot] | None = None,
    *,
    session_attributes: dict | None = None,
) -> RequestEnvelope:
    return RequestEnvelope(
        request=IntentRequest(request_id="req-1", intent=Intent(name=intent_name, slots=slots or {})),
        session=_session(session_attributes),
    )


def _launch_envelope() -> RequestEnvelope:
    return RequestEnvelope(request=LaunchRequest(request_id="req-1"), session=_session())


def _dispatch(envelope: RequestEnvelope):
    """Invokes the real, fully-wired skill (built once at import time in
    lambda_function.py) exactly as AWS Lambda would, and returns the
    `ResponseEnvelope` object `skill.invoke` produces."""
    return lambda_function.sb.create().invoke(envelope, context=None)


def _speech_text(response) -> str:
    # ssml is wrapped as "<speak>...</speak>"
    return response.response.output_speech.ssml


def _should_end_session(response) -> bool:
    return response.response.should_end_session


def _response_session_attributes(response) -> dict:
    """ask-sdk-core carries updated session attributes back on the
    ResponseEnvelope itself (`response_envelope.session_attributes`), which
    is what a real Alexa turn would persist server-side and echo back into
    the next request's `Session.attributes`."""
    return response.session_attributes or {}


# ---------------------------------------------------------------------------
# 1. LaunchRequest / Help / Stop — no backend call involved
# ---------------------------------------------------------------------------
def test_launch_request_speaks_a_welcome_message():
    response = _dispatch(_launch_envelope())
    assert "Pit Wall Copilot" in _speech_text(response)
    assert _should_end_session(response) is False


def test_help_intent_lists_example_commands():
    response = _dispatch(_intent_envelope("AMAZON.HelpIntent"))
    assert "check the undercut" in _speech_text(response)


def test_stop_intent_ends_the_session():
    response = _dispatch(_intent_envelope("AMAZON.StopIntent"))
    assert _should_end_session(response) is True


# ---------------------------------------------------------------------------
# 2. CheckUndercutIntent — grounded numbers pass through verbatim
# ---------------------------------------------------------------------------
def test_check_undercut_speaks_the_backends_exact_numbers(monkeypatch):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "window": "OPEN",
        "expected_margin_ms": 910,
        "confidence": {"probability_ahead": 0.705, "trials": 200},
    }
    captured_request = {}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        captured_request.update(method=method, url=url, json=json, headers=headers)
        return mock_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", "CAR_04"),
        "TargetDriver": _driver_slot("TargetDriver", "car three", "CAR_03"),
    }
    response = _dispatch(_intent_envelope("CheckUndercutIntent", slots))
    speech = _speech_text(response)

    assert captured_request["method"] == "POST"
    assert captured_request["url"].endswith("/v1/races/demo-race-001/strategy/undercut")
    assert captured_request["json"] == {"candidate_driver": "CAR_04", "target_driver": "CAR_03"}
    assert "open" in speech
    assert "910" in speech
    assert "70.5" in speech
    assert _should_end_session(response) is True


def test_check_undercut_without_resolved_slots_asks_to_clarify(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("the backend should not be called when slots didn't resolve")

    monkeypatch.setattr(lambda_function.requests, "request", fail_if_called)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "the blue one", None),
        "TargetDriver": _driver_slot("TargetDriver", "car three", "CAR_03"),
    }
    response = _dispatch(_intent_envelope("CheckUndercutIntent", slots))
    assert "didn't catch" in _speech_text(response)
    assert _should_end_session(response) is False


def test_check_undercut_falls_back_to_word_match_without_resolution(monkeypatch):
    """When slot-type resolution doesn't produce ER_SUCCESS_MATCH (a real
    possibility with live Alexa NLU), the handler still recovers the
    driver id from the raw utterance text via the documented fallback."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "window": "CLOSED",
        "expected_margin_ms": -320,
        "confidence": {"probability_ahead": 0.455, "trials": 200},
    }
    monkeypatch.setattr(
        lambda_function.requests, "request", lambda *a, **k: mock_response
    )

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", None),  # unresolved
        "TargetDriver": _driver_slot("TargetDriver", "car three", None),  # unresolved
    }
    response = _dispatch(_intent_envelope("CheckUndercutIntent", slots))
    assert "closed" in _speech_text(response)


def test_check_undercut_speaks_a_friendly_message_on_422(monkeypatch):
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.json.return_value = {"error": "strategy_validation_error", "message": "unknown driver"}
    monkeypatch.setattr(lambda_function.requests, "request", lambda *a, **k: mock_response)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", "CAR_04"),
        "TargetDriver": _driver_slot("TargetDriver", "car three", "CAR_03"),
    }
    response = _dispatch(_intent_envelope("CheckUndercutIntent", slots))
    speech = _speech_text(response)
    assert "couldn't run that calculation" in speech
    # The API error's raw detail must never be read aloud verbatim.
    assert "strategy_validation_error" not in speech


# ---------------------------------------------------------------------------
# 3. CreateWatchIntent
# ---------------------------------------------------------------------------
def test_create_watch_confirms_baseline_status(monkeypatch):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"watch_id": "watch-abc", "status": "CLOSED"}
    captured_request = {}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        captured_request.update(method=method, url=url, json=json, headers=headers)
        return mock_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", "CAR_04"),
        "TargetDriver": _driver_slot("TargetDriver", "car seven", "CAR_07"),
    }
    response = _dispatch(_intent_envelope("CreateWatchIntent", slots))
    speech = _speech_text(response)

    assert captured_request["headers"] == lambda_function.AUTH_HEADERS
    assert "currently closed" in speech
    # Session stays open specifically so the watch we just created can be
    # referred back to ("explain that") in a follow-up turn.
    assert _should_end_session(response) is False
    response_attributes = _response_session_attributes(response)
    assert response_attributes["last_watch_id"] == "watch-abc"
    # Gate 5: every watch created this session is tracked in "watches"
    # (keyed by watch_id), not just a single flat last_watch_candidate/
    # last_watch_target pair — this is what makes multi-watch
    # disambiguation ("explain the car four watch") possible.
    assert response_attributes["watches"]["watch-abc"] == {"candidate": "CAR_04", "target": "CAR_07"}


# ---------------------------------------------------------------------------
# 3b. Multi-turn: CreateWatchIntent -> ExplainLatestAlertIntent in the SAME
# session should scope the alert query to the watch just created, not fall
# back to the single race-wide newest alert. This is the fix for the
# previously-documented Gate 4 limitation.
# ---------------------------------------------------------------------------
def test_explain_latest_alert_after_create_watch_scopes_to_that_watch(monkeypatch):
    create_response = MagicMock(status_code=200)
    create_response.json.return_value = {"watch_id": "watch-xyz", "status": "CLOSED"}
    monkeypatch.setattr(lambda_function.requests, "request", lambda *a, **k: create_response)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", "CAR_04"),
        "TargetDriver": _driver_slot("TargetDriver", "car three", "CAR_03"),
    }
    turn_1 = _dispatch(_intent_envelope("CreateWatchIntent", slots))
    carried_attributes = _response_session_attributes(turn_1)
    assert carried_attributes["last_watch_id"] == "watch-xyz"

    alerts_response = MagicMock(status_code=200)
    alerts_response.json.return_value = [{"alert_id": "alert-999"}]
    explain_response = MagicMock(status_code=200)
    explain_response.json.return_value = {
        "narrative": "On lap 30, the undercut watch on CAR_04 against CAR_03 moved from CLOSED to OPEN.",
        "grounding_warnings": [],
    }
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/alerts") or "watch_id=" in url:
            return alerts_response
        return explain_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    turn_2 = _dispatch(
        _intent_envelope("ExplainLatestAlertIntent", session_attributes=carried_attributes)
    )
    speech = _speech_text(turn_2)

    assert any("watch_id=watch-xyz" in url for url in calls)
    assert "CAR_04" in speech


def test_explain_latest_alert_after_create_watch_with_no_change_yet(monkeypatch):
    """The watch this session cares about hasn't fired an alert yet — the
    handler should say so specifically, not claim there are no alerts at
    all (which would be misleading once other watches in the race have
    fired their own alerts)."""
    session_attributes = {
        "last_watch_id": "watch-xyz",
        "watches": {"watch-xyz": {"candidate": "CAR_04", "target": "CAR_03"}},
    }
    empty_response = MagicMock(status_code=200)
    empty_response.json.return_value = []
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        return empty_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    response = _dispatch(
        _intent_envelope("ExplainLatestAlertIntent", session_attributes=session_attributes)
    )
    speech = _speech_text(response)

    assert any("watch_id=watch-xyz" in url for url in calls)
    assert "hasn't changed status yet" in speech
    assert "car 4" in speech
    assert "car 3" in speech


# ---------------------------------------------------------------------------
# 3c. Gate 5: multiple concurrently-watched cars per session, disambiguated
# by the optional WatchDriver slot on ExplainLatestAlertIntent.
# ---------------------------------------------------------------------------
def _create_watch_response_attributes(monkeypatch, watch_id: str, candidate: str, target: str, *, prior_attributes=None) -> dict:
    """Dispatches a real CreateWatchIntent turn and returns the resulting
    session attributes, so multi-turn tests can chain several watch
    creations the way a real multi-turn conversation would."""
    create_response = MagicMock(status_code=200)
    create_response.json.return_value = {"watch_id": watch_id, "status": "CLOSED"}
    monkeypatch.setattr(lambda_function.requests, "request", lambda *a, **k: create_response)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", candidate, candidate),
        "TargetDriver": _driver_slot("TargetDriver", target, target),
    }
    response = _dispatch(_intent_envelope("CreateWatchIntent", slots, session_attributes=prior_attributes))
    return _response_session_attributes(response)


def test_explain_latest_alert_disambiguates_by_watch_driver_slot(monkeypatch):
    # Two watches created in the same session — CAR_07-vs-CAR_11 first,
    # then CAR_04-vs-CAR_03 (so CAR_04 is the MOST RECENT one, and
    # disambiguating to CAR_07 explicitly proves the WatchDriver slot is
    # actually doing the picking, not just falling back to last_watch_id).
    attrs = _create_watch_response_attributes(monkeypatch, "watch-a", "CAR_07", "CAR_11")
    attrs = _create_watch_response_attributes(monkeypatch, "watch-b", "CAR_04", "CAR_03", prior_attributes=attrs)
    assert attrs["last_watch_id"] == "watch-b"
    assert set(attrs["watches"]) == {"watch-a", "watch-b"}

    alerts_response = MagicMock(status_code=200)
    alerts_response.json.return_value = [{"alert_id": "alert-777"}]
    explain_response = MagicMock(status_code=200)
    explain_response.json.return_value = {
        "narrative": "On lap 27, the undercut watch on CAR_07 against CAR_11 moved from CLOSED to OPEN.",
        "grounding_warnings": [],
    }
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        return alerts_response if "watch_id=" in url or url.endswith("/alerts") else explain_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    slots = {"WatchDriver": _driver_slot("WatchDriver", "car seven", "CAR_07")}
    response = _dispatch(_intent_envelope("ExplainLatestAlertIntent", slots, session_attributes=attrs))
    speech = _speech_text(response)

    assert any("watch_id=watch-a" in url for url in calls)
    assert not any("watch_id=watch-b" in url for url in calls)
    assert "CAR_07" in speech


def test_explain_latest_alert_without_slot_still_defaults_to_most_recent_watch(monkeypatch):
    attrs = _create_watch_response_attributes(monkeypatch, "watch-a", "CAR_07", "CAR_11")
    attrs = _create_watch_response_attributes(monkeypatch, "watch-b", "CAR_04", "CAR_03", prior_attributes=attrs)

    alerts_response = MagicMock(status_code=200)
    alerts_response.json.return_value = [{"alert_id": "alert-888"}]
    explain_response = MagicMock(status_code=200)
    explain_response.json.return_value = {"narrative": "CAR_04 narrative.", "grounding_warnings": []}
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        return alerts_response if "watch_id=" in url or url.endswith("/alerts") else explain_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    response = _dispatch(_intent_envelope("ExplainLatestAlertIntent", session_attributes=attrs))
    _speech_text(response)

    assert any("watch_id=watch-b" in url for url in calls)


def test_explain_latest_alert_unknown_watch_driver_says_no_watch_open(monkeypatch):
    attrs = _create_watch_response_attributes(monkeypatch, "watch-a", "CAR_07", "CAR_11")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the backend should not be called for a driver with no session watch")

    monkeypatch.setattr(lambda_function.requests, "request", fail_if_called)

    slots = {"WatchDriver": _driver_slot("WatchDriver", "car four", "CAR_04")}
    response = _dispatch(_intent_envelope("ExplainLatestAlertIntent", slots, session_attributes=attrs))
    speech = _speech_text(response)

    assert "don't have a watch open" in speech
    assert "car 4" in speech


def test_find_watch_by_driver_prefers_candidate_match_and_most_recent():
    session_attributes = {
        "watches": {
            "watch-1": {"candidate": "CAR_04", "target": "CAR_03"},
            # CAR_04 also appears as a TARGET here — candidate matches must
            # be preferred over target matches.
            "watch-2": {"candidate": "CAR_11", "target": "CAR_04"},
            "watch-3": {"candidate": "CAR_04", "target": "CAR_07"},
        }
    }
    found = lambda_function._find_watch_by_driver(session_attributes, "CAR_04")
    assert found == ("watch-3", "CAR_04", "CAR_07")  # most recent CANDIDATE match, not the target match


def test_remember_watch_evicts_the_oldest_once_the_cap_is_hit():
    class _FakeHandlerInput:
        def __init__(self):
            self.request_envelope = type("Envelope", (), {"session": object()})()
            self.attributes_manager = type("Manager", (), {"session_attributes": {}})()

    handler_input = _FakeHandlerInput()
    cap = lambda_function._MAX_TRACKED_WATCHES_PER_SESSION
    for i in range(cap + 3):
        lambda_function._remember_watch(handler_input, f"watch-{i}", "CAR_04", "CAR_03")

    watches = handler_input.attributes_manager.session_attributes["watches"]
    assert len(watches) == cap
    # The three oldest were evicted; the most recent `cap` remain.
    assert "watch-0" not in watches
    assert "watch-1" not in watches
    assert "watch-2" not in watches
    assert f"watch-{cap + 2}" in watches


# ---------------------------------------------------------------------------
# 4. ExplainLatestAlertIntent
# ---------------------------------------------------------------------------
def test_explain_latest_alert_speaks_the_grounded_narrative(monkeypatch):
    alerts_response = MagicMock(status_code=200)
    alerts_response.json.return_value = [{"alert_id": "alert-123"}]
    explain_response = MagicMock(status_code=200)
    explain_response.json.return_value = {
        "narrative": "On lap 27, the undercut watch on CAR_04 against CAR_03 moved from CLOSED to OPEN.",
        "grounding_warnings": [],
    }
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/alerts"):
            return alerts_response
        return explain_response

    monkeypatch.setattr(lambda_function.requests, "request", fake_request)

    response = _dispatch(_intent_envelope("ExplainLatestAlertIntent"))
    speech = _speech_text(response)
    assert "CAR_04" in speech
    assert any(url.endswith("/alerts") for url in calls)
    assert any(url.endswith("/alerts/alert-123/explain") for url in calls)


def test_explain_latest_alert_with_no_alerts_yet(monkeypatch):
    empty_response = MagicMock(status_code=200)
    empty_response.json.return_value = []
    monkeypatch.setattr(lambda_function.requests, "request", lambda *a, **k: empty_response)

    response = _dispatch(_intent_envelope("ExplainLatestAlertIntent"))
    assert "no alerts yet" in _speech_text(response)


# ---------------------------------------------------------------------------
# 5. SessionEndedRequest / unhandled exceptions
# ---------------------------------------------------------------------------
def test_session_ended_request_is_handled_without_error():
    response = _dispatch(RequestEnvelope(request=SessionEndedRequest(request_id="req-1")))
    assert response is not None


def test_unhandled_exception_is_caught_and_speaks_an_apology(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated backend outage")

    monkeypatch.setattr(lambda_function.requests, "request", boom)

    slots = {
        "CandidateDriver": _driver_slot("CandidateDriver", "car four", "CAR_04"),
        "TargetDriver": _driver_slot("TargetDriver", "car three", "CAR_03"),
    }
    response = _dispatch(_intent_envelope("CheckUndercutIntent", slots))
    assert "something went wrong" in _speech_text(response).lower()
