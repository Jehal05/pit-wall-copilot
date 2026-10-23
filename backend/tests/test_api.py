from __future__ import annotations

import asyncio

from tests.conftest import TEST_TICK_SECONDS

from tests.conftest import TEST_DEMO_ACCESS_TOKEN  # noqa: E402 spec v1.1 repair 11: no hardcoded token

AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


async def test_health_endpoint(api_client):
    client, _service = api_client
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "app_version" in body
    # AWS hackathon-final pass, phase 4/5: memory mode (this fixture's
    # default) is always trivially connected -- no cross-instance broker
    # requirement to fail.
    assert body["event_broker"] == "memory"
    assert body["event_broker_connected"] is True


# AWS hackathon-final pass, phase 4: "do not mark backend ready for
# multi-instance traffic while required broker is unavailable" — GET
# /health must fail (503) when EVENT_BROKER=redis is configured but the
# broker is not currently connected, since this is exactly what ECS's own
# target-group health check and DeploymentCircuitBreaker
# (infrastructure/template.yaml) rely on to stop routing traffic to a
# task that cannot fan events out across instances.
class TestHealthReflectsEventBrokerReadiness:
    async def test_health_is_503_when_redis_broker_is_configured_but_not_connected(self, api_client):
        from app.api.deps import get_event_broker
        from app.config import Settings, get_settings

        client, _service = api_client
        app = client._transport.app  # noqa: SLF001 - test-only introspection to install overrides

        class _DisconnectedBroker:
            is_connected = False

        app.dependency_overrides[get_settings] = lambda: Settings(event_broker="redis")
        app.dependency_overrides[get_event_broker] = lambda: _DisconnectedBroker()
        try:
            response = await client.get("/health")
            assert response.status_code == 503
            body = response.json()
            assert body["status"] == "degraded"
            assert body["event_broker"] == "redis"
            assert body["event_broker_connected"] is False
        finally:
            app.dependency_overrides.pop(get_settings, None)
            app.dependency_overrides.pop(get_event_broker, None)

    async def test_health_is_200_when_redis_broker_is_configured_and_connected(self, api_client):
        from app.api.deps import get_event_broker
        from app.config import Settings, get_settings

        client, _service = api_client
        app = client._transport.app  # noqa: SLF001

        class _ConnectedBroker:
            is_connected = True

        app.dependency_overrides[get_settings] = lambda: Settings(event_broker="redis")
        app.dependency_overrides[get_event_broker] = lambda: _ConnectedBroker()
        try:
            response = await client.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "ok"
            assert body["event_broker_connected"] is True
        finally:
            app.dependency_overrides.pop(get_settings, None)
            app.dependency_overrides.pop(get_event_broker, None)

    async def test_health_stays_200_for_memory_broker_even_if_hypothetically_disconnected(self, api_client):
        """EVENT_BROKER=memory has no cross-instance requirement to fail
        -- InMemoryEventBroker.is_connected is trivially always True (see
        event_broker.py), so this is really a guard against the READINESS
        GATE itself, not just the broker implementation: even a stubbed
        "disconnected" memory-mode broker must not 503, since memory mode
        is single-instance by definition and has nothing to be
        "disconnected" from."""
        from app.api.deps import get_event_broker

        client, _service = api_client
        app = client._transport.app  # noqa: SLF001

        class _DisconnectedBroker:
            is_connected = False

        app.dependency_overrides[get_event_broker] = lambda: _DisconnectedBroker()
        try:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
        finally:
            app.dependency_overrides.pop(get_event_broker, None)


async def test_get_state_returns_initial_lap_24(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/demo-race-001/state")
    assert response.status_code == 200
    body = response.json()
    assert body["lap"] == 24
    assert body["state_version"] == 0
    assert len(body["drivers"]) == 4


# 10. Unknown race returns 404.
async def test_unknown_race_returns_404(api_client):
    client, _service = api_client
    response = await client.get("/v1/races/unknown-race-999/state")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "race_not_found"


async def test_start_requires_auth_token(api_client):
    client, _service = api_client
    response = await client.post("/v1/demo/start")
    assert response.status_code == 401


async def test_start_pause_reset_flow_via_api(api_client):
    client, _service = api_client

    start_response = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert start_response.status_code == 200
    assert start_response.json()["status"] == "RUNNING"

    lap = 24
    elapsed = 0.0
    while elapsed < TEST_TICK_SECONDS * 6 + 0.5:
        await asyncio.sleep(0.02)
        elapsed += 0.02
        state_response = await client.get("/v1/races/demo-race-001/state")
        lap = state_response.json()["lap"]
        if lap > 24:
            break
    assert lap > 24

    pause_response = await client.post("/v1/demo/pause", headers=AUTH_HEADERS)
    assert pause_response.status_code == 200
    assert pause_response.json()["status"] == "PAUSED"

    reset_response = await client.post("/v1/demo/reset", headers=AUTH_HEADERS)
    assert reset_response.status_code == 200
    reset_body = reset_response.json()
    assert reset_body["lap"] == 24
    assert reset_body["state_version"] == 0

    status_response = await client.get("/v1/demo/status")
    assert status_response.json()["status"] == "STOPPED"
    assert status_response.json()["current_lap"] == 24
