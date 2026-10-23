from __future__ import annotations

import asyncio

from tests.conftest import TEST_TICK_SECONDS

from tests.conftest import TEST_DEMO_ACCESS_TOKEN  # noqa: E402 spec v1.1 repair 11: no hardcoded token

AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


async def test_create_watch_requires_auth(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
    )
    assert response.status_code == 401


async def test_create_watch_returns_watch_id_and_current_status(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["watch_id"]
    assert body["candidate"] == "CAR_04"
    assert body["target"] == "CAR_03"
    assert body["status"] in ("OPEN", "CLOSED")
    assert body["status"] == "CLOSED"  # matches the known lap-24 baseline


async def test_create_watch_unknown_driver_returns_422(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_99", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422


async def test_list_and_get_and_delete_watch(api_client):
    client, _service = api_client
    create_response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    watch_id = create_response.json()["watch_id"]

    list_response = await client.get("/v1/races/demo-race-001/watches")
    assert list_response.status_code == 200
    assert any(w["watch_id"] == watch_id for w in list_response.json())

    get_response = await client.get(f"/v1/races/demo-race-001/watches/{watch_id}")
    assert get_response.status_code == 200
    assert get_response.json()["watch_id"] == watch_id

    delete_no_auth = await client.delete(f"/v1/races/demo-race-001/watches/{watch_id}")
    assert delete_no_auth.status_code == 401

    delete_response = await client.delete(
        f"/v1/races/demo-race-001/watches/{watch_id}", headers=AUTH_HEADERS
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True

    get_after_delete = await client.get(f"/v1/races/demo-race-001/watches/{watch_id}")
    assert get_after_delete.status_code == 404


async def test_get_unknown_watch_returns_404(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/demo-race-001/watches/watch-does-not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# End-to-end: create a watch, run the live demo, confirm an alert appears
# autonomously — this is the acceptance-test-5 story: "the watch
# autonomously triggers when race conditions change" and "Fire TV receives
# the alert" (Gate 2 exposes this via polling, see README).
# ---------------------------------------------------------------------------
async def test_watch_autonomously_opens_as_the_live_demo_advances(api_client):
    client, _service = api_client

    create_response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    watch_id = create_response.json()["watch_id"]
    assert create_response.json()["status"] == "CLOSED"

    # No alerts should exist yet — creation is a baseline, not a transition.
    initial_alerts = await client.get("/v1/races/demo-race-001/alerts")
    assert initial_alerts.json() == []

    start_response = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert start_response.status_code == 200

    # Poll until the watch transitions to OPEN or the demo completes.
    deadline_ticks = 60
    watch_status = "CLOSED"
    for _ in range(deadline_ticks):
        await asyncio.sleep(TEST_TICK_SECONDS)
        watch_response = await client.get(f"/v1/races/demo-race-001/watches/{watch_id}")
        watch_status = watch_response.json()["status"]
        if watch_status == "OPEN":
            break

    assert watch_status == "OPEN"

    alerts_response = await client.get("/v1/races/demo-race-001/alerts")
    alerts = alerts_response.json()
    assert len(alerts) >= 1
    assert alerts[0]["new_status"] == "OPEN"
    assert alerts[0]["watch_id"] == watch_id

    watch_scoped = await client.get(f"/v1/races/demo-race-001/alerts?watch_id={watch_id}")
    assert len(watch_scoped.json()) >= 1


async def test_why_now_returns_stable_triggering_snapshot(api_client):
    """The 'Why now?' contract (spec 49): fetching an alert must always
    return its original triggering snapshot, even after the live race has
    moved on. This exercises it through the real HTTP API end to end."""
    client, _service = api_client

    create_response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    watch_id = create_response.json()["watch_id"]

    await client.post("/v1/demo/start", headers=AUTH_HEADERS)

    alert = None
    for _ in range(60):
        await asyncio.sleep(TEST_TICK_SECONDS)
        alerts_response = await client.get(f"/v1/races/demo-race-001/alerts?watch_id={watch_id}")
        alerts = alerts_response.json()
        if alerts:
            alert = alerts[0]
            break
    assert alert is not None
    alert_id = alert["alert_id"]
    triggering_lap = alert["triggering_snapshot"]["lap"]
    triggering_version = alert["triggering_snapshot"]["state_version"]

    # Let the live race continue advancing past the alert's own lap.
    await asyncio.sleep(TEST_TICK_SECONDS * 3)

    live_state = await client.get("/v1/races/demo-race-001/state")
    assert live_state.json()["lap"] >= triggering_lap  # race has moved on (or completed at the same lap)

    refetched = await client.get(f"/v1/races/demo-race-001/alerts/{alert_id}")
    assert refetched.status_code == 200
    refetched_body = refetched.json()
    assert refetched_body["triggering_snapshot"]["lap"] == triggering_lap
    assert refetched_body["triggering_snapshot"]["state_version"] == triggering_version


async def test_get_unknown_alert_returns_404(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/demo-race-001/alerts/alert-does-not-exist")
    assert response.status_code == 404


async def test_reset_invalidates_previous_run_watch(api_client):
    """After /v1/demo/reset, a watch created under the old run must no
    longer be active for the new run (spec 48)."""
    client, _service = api_client

    create_response = await client.post(
        "/v1/races/demo-race-001/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
        headers=AUTH_HEADERS,
    )
    watch_id = create_response.json()["watch_id"]

    await client.post("/v1/demo/reset", headers=AUTH_HEADERS)

    active_watches = await client.get("/v1/races/demo-race-001/watches?active_only=true")
    assert all(w["watch_id"] != watch_id for w in active_watches.json())

    # The watch still exists in history, but is excluded from the active list.
    full_history = await client.get("/v1/races/demo-race-001/watches?active_only=false")
    assert any(w["watch_id"] == watch_id for w in full_history.json())
