from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import create_app


class _BrokenStrategyEngine:
    """Stands in for a strategy engine that fails with something OTHER
    than StrategyValidationError — an unexpected internal failure, not a
    caller mistake. Spec v1.1 repair 14 / Section 29 "Strategy engine
    failure": this must surface as the exact literal degraded response,
    never a raw 500 traceback and never a silently-substituted LLM guess."""

    def calculate_undercut(self, *args, **kwargs):
        raise RuntimeError("simulated unexpected strategy engine failure")

    def simulate_strategy(self, *args, **kwargs):
        raise RuntimeError("simulated unexpected strategy engine failure")

    def watch_thresholds(self):
        raise RuntimeError("simulated unexpected strategy engine failure")


async def test_calculate_undercut_unexpected_engine_failure_returns_strategy_model_unavailable():
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.strategy_engine = _BrokenStrategyEngine()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/races/demo-race-001/strategy/undercut",
                json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
            )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "strategy_engine_unavailable"
    assert body["message"] == "STRATEGY MODEL UNAVAILABLE"


async def test_simulate_strategy_unexpected_engine_failure_returns_strategy_model_unavailable():
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.strategy_engine = _BrokenStrategyEngine()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/races/demo-race-001/strategy/simulate",
                json={"driver_id": "CAR_04", "wait_laps": 3},
            )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "strategy_engine_unavailable"
    assert body["message"] == "STRATEGY MODEL UNAVAILABLE"


async def test_create_watch_unexpected_engine_failure_returns_strategy_model_unavailable():
    from app.services.watch_evaluator import WatchEvaluator

    app = create_app()
    async with app.router.lifespan_context(app):
        # create_strategy_watch's WatchEvaluator holds its own
        # strategy_engine reference from construction time (see
        # app/main.py's lifespan) rather than reading app.state.strategy_
        # engine per-request, so the broken engine has to be wired into a
        # fresh WatchEvaluator built from the app's real repositories,
        # not just assigned onto app.state.strategy_engine (which is what
        # the undercut/simulate endpoints above DO read per-request via
        # Depends(get_strategy_engine)).
        app.state.watch_evaluator = WatchEvaluator(
            watch_repository=app.state.watch_repository,
            alert_repository=app.state.alert_repository,
            strategy_engine=_BrokenStrategyEngine(),
        )
        # create_strategy_watch is a mutating endpoint, so it needs the
        # demo access token this test process's conftest.py already
        # generated and set as DEMO_ACCESS_TOKEN.
        import os

        token = os.environ["DEMO_ACCESS_TOKEN"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/races/demo-race-001/watches",
                json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
                headers={"X-Demo-Access-Token": token},
            )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "strategy_engine_unavailable"
    assert body["message"] == "STRATEGY MODEL UNAVAILABLE"


async def test_calculate_undercut_endpoint_returns_result(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/strategy/undercut",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate"] == "CAR_04"
    assert body["target"] == "CAR_03"
    assert body["window"] in {"OPEN", "CLOSED"}
    assert "expected_margin_ms" in body
    assert body["confidence"]["trials"] == 200
    assert 0.0 <= body["confidence"]["probability_ahead"] <= 1.0


async def test_calculate_undercut_unknown_driver_returns_422(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/strategy/undercut",
        json={"candidate_driver": "CAR_99", "target_driver": "CAR_03"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "strategy_validation_error"


async def test_calculate_undercut_unknown_race_returns_404(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/unknown-race/strategy/undercut",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03"},
    )
    assert response.status_code == 404


async def test_simulate_strategy_endpoint_returns_result(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/strategy/simulate",
        json={"driver_id": "CAR_04", "wait_laps": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["driver_id"] == "CAR_04"
    assert body["wait_laps"] == 3
    assert "delta_ms" in body
    assert body["confidence"]["trials"] == 200


async def test_simulate_strategy_default_wait_laps(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/strategy/simulate",
        json={"driver_id": "CAR_04"},
    )
    assert response.status_code == 200
    assert response.json()["wait_laps"] == 3  # DEFAULT_WAIT_LAPS


async def test_simulate_strategy_invalid_wait_laps_returns_422(api_client):
    client, _service = api_client
    response = await client.post(
        "/v1/races/demo-race-001/strategy/simulate",
        json={"driver_id": "CAR_04", "wait_laps": 0},
    )
    assert response.status_code == 422
