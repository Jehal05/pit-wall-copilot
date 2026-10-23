"""REST-level tests for POST /v1/agent/query and
GET /v1/races/{race_id}/changes — spec v1.1 repair 9.

Unit coverage of the underlying logic lives in tests/test_agent_service.py;
these check the HTTP contract only.
"""

from __future__ import annotations


async def test_agent_query_returns_200_with_structured_evidence(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/agent/query", json={"race_id": "demo-race-001", "query": "Who is leading the race?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUPPORTED"
    assert len(body["evidence"]) >= 1
    assert body["evidence"][0]["type"] == "race_state_field"


async def test_agent_query_unsupported_returns_200_not_an_error(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/agent/query", json={"race_id": "demo-race-001", "query": "What is the capital of France?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "UNSUPPORTED"
    assert body["evidence"] == []


async def test_agent_query_unknown_race_returns_404(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/agent/query", json={"race_id": "unknown-race-999", "query": "Who is leading?"}
    )
    assert response.status_code == 404


async def test_agent_query_does_not_require_auth(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/agent/query", json={"race_id": "demo-race-001", "query": "What lap is it?"}
    )
    assert response.status_code == 200


async def test_race_changes_with_no_prior_state_returns_empty_changes(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/demo-race-001/changes")
    assert response.status_code == 200
    body = response.json()
    assert body["from_state_version"] is None
    assert body["changes"] == []
    assert body["explained_changes"] == []


async def test_race_changes_unknown_race_returns_404(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/unknown-race-999/changes")
    assert response.status_code == 404


async def test_race_changes_reflects_state_after_demo_start(api_client):
    import asyncio

    from tests.conftest import TEST_TICK_SECONDS, TEST_DEMO_ACCESS_TOKEN

    client, _service = api_client
    await client.post("/v1/demo/start", headers={"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN})

    # Wait for at least one lap advance (a real, committed state
    # transition) so the tracker has a previous/current pair to diff.
    for _ in range(60):
        await asyncio.sleep(TEST_TICK_SECONDS)
        state_response = await client.get("/v1/races/demo-race-001/state")
        if state_response.json()["lap"] > 24:
            break

    response = await client.get("/v1/races/demo-race-001/changes")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["to_state_version"] >= 1
    assert len(body["explained_changes"]) <= 3
