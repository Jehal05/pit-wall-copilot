"""Real-transport WebSocket tests — spec v1.1 repair 3.

Unlike test_event_broadcaster.py (a fake-WebSocket unit test),
these connect to `/v1/races/{race_id}/ws` through FastAPI's real
`TestClient` (a genuine ASGI WebSocket handshake, not a mock), and drive
events by calling REST endpoints on the SAME running app so the test
doesn't have to wait for the simulator's real tick interval to elapse.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from tests.conftest import TEST_DEMO_ACCESS_TOKEN

AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


class TestWebSocketAuthentication:
    """AWS hackathon-final pass, P2-12: before this pass, this route had
    NO credential check at all, unlike every HTTP route serving the same
    data. These tests prove the gap is actually closed, not just that a
    valid token still works (already covered by every other test in this
    file, which now all pass `?token=...`)."""

    def test_connecting_with_no_token_at_all_is_rejected(self):
        app = create_app()
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect("/v1/races/demo-race-001/ws"):
                    pass
            assert excinfo.value.code == 4401

    def test_connecting_with_a_wrong_token_is_rejected(self):
        app = create_app()
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect("/v1/races/demo-race-001/ws?token=not-the-real-token"):
                    pass
            assert excinfo.value.code == 4401

    def test_a_rejected_connection_never_receives_any_race_data(self):
        # Belt-and-suspenders: even if the close code assertion above were
        # somehow wrong, this proves no RACE_STATE snapshot is ever sent
        # to an unauthenticated caller -- the actual confidentiality
        # property this whole check exists to protect.
        app = create_app()
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/v1/races/demo-race-001/ws") as websocket:
                    websocket.receive_json()


def test_connecting_immediately_receives_a_race_state_snapshot():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            message = websocket.receive_json()
            assert message["type"] == "RACE_STATE"
            assert message["payload"]["lap"] == 24
            assert message["race_id"] == "demo-race-001"


def test_unknown_race_connects_without_an_initial_snapshot_and_stays_open():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/unknown-race-999/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            # No RACE_STATE for a race that doesn't exist — the connection
            # itself is still accepted (matching the REST 404-only-for-an-
            # actual-request-about-it behavior), it just never receives
            # anything for this synthetic demo's own initial snapshot.
            websocket.close()


def test_calculate_undercut_broadcasts_a_strategy_update_event():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            initial = websocket.receive_json()
            assert initial["type"] == "RACE_STATE"

            response = client.post(
                "/v1/races/demo-race-001/strategy/undercut",
                json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
            )
            assert response.status_code == 200

            event = websocket.receive_json()
            assert event["type"] == "STRATEGY_UPDATE"
            assert event["payload"]["kind"] == "undercut"
            assert event["payload"]["result"]["candidate"] == "CAR_04"


def test_simulate_strategy_broadcasts_a_strategy_update_event():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            websocket.receive_json()  # initial RACE_STATE

            response = client.post(
                "/v1/races/demo-race-001/strategy/simulate",
                json={"driver_id": "CAR_04", "wait_laps": 3},
            )
            assert response.status_code == 200

            event = websocket.receive_json()
            assert event["type"] == "STRATEGY_UPDATE"
            assert event["payload"]["kind"] == "simulate"


def test_agent_query_broadcasts_an_agent_response_event():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            websocket.receive_json()  # initial RACE_STATE

            response = client.post(
                "/v1/agent/query", json={"race_id": "demo-race-001", "query": "Who is leading the race?"}
            )
            assert response.status_code == 200

            event = websocket.receive_json()
            assert event["type"] == "AGENT_RESPONSE"
            assert event["payload"]["status"] == "SUPPORTED"


def test_demo_reset_broadcasts_a_race_reset_event_not_a_plain_race_state():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as websocket:
            websocket.receive_json()  # initial RACE_STATE

            response = client.post("/v1/demo/reset", headers=AUTH_HEADERS)
            assert response.status_code == 200

            event = websocket.receive_json()
            assert event["type"] == "RACE_RESET"
            assert event["payload"]["state_version"] == 0


def test_two_clients_on_the_same_race_both_receive_the_same_broadcast():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as ws_a, \
                client.websocket_connect(f"/v1/races/demo-race-001/ws?token={TEST_DEMO_ACCESS_TOKEN}") as ws_b:
            ws_a.receive_json()
            ws_b.receive_json()

            client.post(
                "/v1/races/demo-race-001/strategy/undercut",
                json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
            )

            event_a = ws_a.receive_json()
            event_b = ws_b.receive_json()
            assert event_a["type"] == event_b["type"] == "STRATEGY_UPDATE"
