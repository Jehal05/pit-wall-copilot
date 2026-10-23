"""P0-1 repair pass: REST-level `/start`, `/pause`, `/reset`, `/status`
lifecycle tests against production-equivalent DynamoDB repositories (moto-
backed), not only memory repositories — the FastAPI app is built exactly
as `app/main.py`'s real lifespan builds it (`create_app()` +
`app.router.lifespan_context`), with `STATE_REPOSITORY=dynamodb` so
`factory.build_repository` constructs the real
`DynamoDBRaceStateRepository(require_lease_proof=True)` production wires
it into `SimulatorService` — the exact configuration that was broken
before this repair pass (every `/start`/`/pause` call raised
`StaleWriteError` because control writes had no way to succeed without a
`lease_proof`).
"""

from __future__ import annotations

import asyncio
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.config import Settings  # noqa: E402
from tests.conftest import TEST_TICK_SECONDS  # noqa: E402

TABLE_NAME = "pit-wall-api-dynamodb-test"
DEMO_ACCESS_TOKEN = secrets.token_urlsafe(16)
AUTH_HEADERS = {"X-Demo-Access-Token": DEMO_ACCESS_TOKEN}


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _dynamodb_settings() -> Settings:
    return Settings(
        state_repository="dynamodb",
        dynamodb_table_name=TABLE_NAME,
        aws_region="us-east-1",
        demo_access_token=DEMO_ACCESS_TOKEN,
        local_demo_auth_mode=False,
        # Keep event broker/pairing simple/in-memory-equivalent-under-moto:
        # PAIRING_REPOSITORY mirrors state_repository (dynamodb) unless
        # overridden, which is fine — it uses the SAME moto table, exactly
        # like a real single-table AWS deployment.
    )


@pytest_asyncio.fixture
async def dynamodb_api_client(dynamodb_table, monkeypatch):
    """A real FastAPI app (`create_app()`, real `lifespan`), monkeypatched
    to build with `STATE_REPOSITORY=dynamodb` settings instead of calling
    the process-wide, `lru_cache`d `get_settings()` — avoids interfering
    with every other (memory-mode) test's cached settings, while still
    exercising the EXACT SAME `app/main.py` lifespan/wiring code path a
    real deployment uses. Tick interval is sped up exactly like the
    memory-mode `api_client` fixture (tests/conftest.py) does."""
    import app.main as main_module

    settings = _dynamodb_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        from app.services.simulator import SimulatorService

        original_service = app.state.simulator_service
        fast_service = SimulatorService(
            repository=original_service._repository,  # noqa: SLF001 reuse already-initialized race
            scenario=original_service._scenario,  # noqa: SLF001
            sleep_fn=asyncio.sleep,
            tick_interval_seconds=TEST_TICK_SECONDS,
            on_state_committed=original_service._on_state_committed,  # noqa: SLF001
            lease_repository=original_service._lease_repository,  # noqa: SLF001
            lease_ttl_seconds=TEST_TICK_SECONDS * 6,
        )
        app.state.simulator_service = fast_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, fast_service

        await fast_service._cancel_active_loop()  # noqa: SLF001 test cleanup


async def test_dynamodb_start_requires_auth_token(dynamodb_api_client):
    client, _service = dynamodb_api_client
    response = await client.post("/v1/demo/start")
    assert response.status_code == 401


async def test_dynamodb_start_pause_reset_status_flow_via_api(dynamodb_api_client):
    """The exact lifecycle that was broken before this repair pass: every
    one of these calls previously raised StaleWriteError against a real
    DynamoDB (require_lease_proof=True) repository because start()/pause()
    called save_control with no lease_proof."""
    client, _service = dynamodb_api_client

    start_response = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert start_response.status_code == 200
    assert start_response.json()["status"] == "RUNNING"

    lap = 24
    elapsed = 0.0
    while elapsed < TEST_TICK_SECONDS * 10 + 0.5:
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

    # Idempotent repeated pause (already PAUSED) must also succeed, not 500.
    pause_again_response = await client.post("/v1/demo/pause", headers=AUTH_HEADERS)
    assert pause_again_response.status_code == 200
    assert pause_again_response.json()["status"] == "PAUSED"

    resume_response = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert resume_response.status_code == 200
    assert resume_response.json()["status"] == "RUNNING"

    reset_response = await client.post("/v1/demo/reset", headers=AUTH_HEADERS)
    assert reset_response.status_code == 200
    reset_body = reset_response.json()
    assert reset_body["lap"] == 24
    assert reset_body["state_version"] == 0

    status_response = await client.get("/v1/demo/status")
    assert status_response.json()["status"] == "STOPPED"
    assert status_response.json()["current_lap"] == 24


async def test_dynamodb_repeated_start_calls_are_idempotent_via_api(dynamodb_api_client):
    client, _service = dynamodb_api_client

    first = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    second = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "RUNNING"
    assert second.json()["status"] == "RUNNING"


async def test_dynamodb_pause_succeeds_even_when_this_process_never_held_the_lease(dynamodb_api_client):
    """Directly proves requirement 8: `/pause` must succeed even when the
    request lands on a backend task that is not the current simulator
    leader — here, by pausing before this process's own tick loop has had
    any chance to win a lease bid at all (tick interval is short, but we
    pause immediately after start with no intervening sleep)."""
    client, _service = dynamodb_api_client

    start_response = await client.post("/v1/demo/start", headers=AUTH_HEADERS)
    assert start_response.status_code == 200

    pause_response = await client.post("/v1/demo/pause", headers=AUTH_HEADERS)
    assert pause_response.status_code == 200
    assert pause_response.json()["status"] == "PAUSED"
