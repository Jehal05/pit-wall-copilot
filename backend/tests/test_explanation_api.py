from __future__ import annotations

import asyncio

from tests.conftest import TEST_TICK_SECONDS

from tests.conftest import TEST_DEMO_ACCESS_TOKEN  # noqa: E402 spec v1.1 repair 11: no hardcoded token

AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


async def _create_watch_start_and_get_first_alert(client):
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
    assert alert is not None, "expected the live demo to produce an alert within the polling window"
    return alert


async def test_explain_alert_returns_grounded_narrative(api_client):
    client, _service = api_client
    alert = await _create_watch_start_and_get_first_alert(client)

    response = await client.get(f"/v1/races/demo-race-001/alerts/{alert['alert_id']}/explain")
    assert response.status_code == 200
    body = response.json()

    assert body["alert_id"] == alert["alert_id"]
    assert body["watch_id"] == alert["watch_id"]
    assert body["narrative"]
    assert body["llm_provider"] == "template"
    assert body["grounding_warnings"] == []
    assert body["citations"]
    assert any(c["doc_id"] == "sr-undercut-001" for c in body["citations"])
    # The grounded facts must exactly match the alert's own deterministic numbers.
    assert body["grounded_facts"]["expected_margin_ms"] == str(alert["deterministic_result"]["expected_margin_ms"])
    assert body["grounded_facts"]["candidate"] == alert["deterministic_result"]["candidate"]
    assert body["grounded_facts"]["new_status"] == alert["new_status"]


async def test_explain_alert_does_not_require_auth(api_client):
    """Explaining an alert is a read, like GET .../alerts/{id} itself —
    it does not mutate anything, so (consistent with the other GET
    endpoints in this API) it does not require the demo access token."""
    client, _service = api_client
    alert = await _create_watch_start_and_get_first_alert(client)

    response = await client.get(f"/v1/races/demo-race-001/alerts/{alert['alert_id']}/explain")
    assert response.status_code == 200


async def test_explain_unknown_alert_returns_404(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/demo-race-001/alerts/alert-does-not-exist/explain")
    assert response.status_code == 404


async def test_explain_alert_is_stable_across_repeated_calls(api_client):
    """Re-explaining the same immutable alert twice must return the exact
    same cached explanation (same explanation_id, same narrative, same
    grounded facts) — not just equal grounded facts with a fresh id each
    time. This is the fixed behavior: explanation_engine.py now caches by
    alert_id (since an alert's grounded facts never change once written),
    both to avoid re-paying for a real Bedrock call on every read and so a
    client asking "explain that" twice in a row gets a consistent answer
    rather than two independently-phrased narratives for the same facts."""
    client, _service = api_client
    alert = await _create_watch_start_and_get_first_alert(client)

    first = await client.get(f"/v1/races/demo-race-001/alerts/{alert['alert_id']}/explain")
    second = await client.get(f"/v1/races/demo-race-001/alerts/{alert['alert_id']}/explain")

    assert first.json()["grounded_facts"] == second.json()["grounded_facts"]
    assert first.json()["explanation_id"] == second.json()["explanation_id"]
    assert first.json()["narrative"] == second.json()["narrative"]
    assert first.json()["created_at"] == second.json()["created_at"]


async def test_explain_alert_rate_limits_a_client_that_exceeds_the_per_minute_cap(api_client, monkeypatch):
    """A burst of requests past the configured per-minute cap gets a 429
    with Retry-After, instead of an unbounded number of explanation
    generations (each of which is a real, paid call for a Bedrock-backed
    deployment) — see app/services/rate_limiter.py."""
    client, service = api_client
    app = client._transport.app  # noqa: SLF001 - test-only introspection of the wired ASGI app
    monkeypatch.setattr(app.state.explain_rate_limiter, "_limit", 2)

    alert = await _create_watch_start_and_get_first_alert(client)
    path = f"/v1/races/demo-race-001/alerts/{alert['alert_id']}/explain"

    first = await client.get(path)
    second = await client.get(path)
    third = await client.get(path)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"] == "rate_limited"
    assert "Retry-After" in third.headers
