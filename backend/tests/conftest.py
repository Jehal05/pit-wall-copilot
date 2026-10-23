from __future__ import annotations

import asyncio
import os
import secrets
import socket

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import uuid

# Spec v1.1 repair 11: "Local test credentials must be generated or
# supplied only within test fixtures" — this is that fixture. The
# backend (app/config.py) no longer has ANY default demo-access-token
# baked into source, so tests that exercise mutating endpoints need a
# token supplied here, generated fresh per test session, and set as the
# DEMO_ACCESS_TOKEN environment variable BEFORE `app.config.get_settings`
# (which is `@lru_cache`d — first read wins for the whole process) is
# ever called by anything. This module-level statement runs at conftest
# import time, which pytest guarantees happens before any test in this
# directory is collected or run, so it wins that race.
TEST_DEMO_ACCESS_TOKEN = os.environ.setdefault("DEMO_ACCESS_TOKEN", secrets.token_urlsafe(16))

# Spec v1.1 pass 3, correction 10: auto-tags every collected test with the
# `unit` marker by default, UNLESS it already carries one of the registered
# category markers explicitly (see pytest.ini) — the small set of files that
# exercise moto-simulated AWS behavior (real DynamoDB API semantics via
# moto, not a real AWS account) mark themselves `integration` at module
# scope (`pytestmark = pytest.mark.integration`). This means `pytest -m
# unit` and `pytest -m integration` both give an honest, exhaustive split of
# this suite without hand-tagging every one of hundreds of individual test
# functions.
_CATEGORY_MARKER_NAMES = {"unit", "integration", "infrastructure", "android", "aws", "device"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    for item in items:
        already_categorized = any(item.get_closest_marker(name) for name in _CATEGORY_MARKER_NAMES)
        if not already_categorized:
            item.add_marker(pytest.mark.unit)

# Repair pass (repo-wide Ruff cleanup): these imports are deliberately
# NOT at the top of the file — `app.config.get_settings` is `@lru_cache`d
# (first call wins for the whole process), so `app.config` (and anything
# importing it transitively) must not be imported until AFTER line 24's
# `os.environ.setdefault("DEMO_ACCESS_TOKEN", ...)` has run. Each E402 here
# is this same, already-documented ordering requirement, not an oversight —
# suppressed individually (not via a file- or repo-wide ignore) so a
# genuinely accidental late import elsewhere still gets flagged normally.
from app.config import DEFAULT_KNOWLEDGE_BASE_DIR, DEFAULT_SCENARIO_PATH  # noqa: E402
from app.domain.models import RaceState, DriverState, utc_now  # noqa: E402
from app.domain.scenario import Scenario, load_scenario  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.memory import InMemoryRaceStateRepository  # noqa: E402
from app.repositories.watch_memory import InMemoryAlertRepository, InMemoryWatchRepository  # noqa: E402
from app.services.explanation_engine import ExplanationEngine  # noqa: E402
from app.services.knowledge_base import KnowledgeBaseService  # noqa: E402
from app.services.llm_client import TemplateLLMClient  # noqa: E402
from app.services.simulator import SimulatorService  # noqa: E402
from app.services.strategy_engine import StrategyEngine  # noqa: E402
from app.services.watch_evaluator import WatchEvaluator  # noqa: E402

SCENARIO_PATH = DEFAULT_SCENARIO_PATH


# ---------------------------------------------------------------------------
# Local-demo-fixed repair, issue 5: "The full local suite must pass without
# contacting AWS" needs to be an ENFORCED property of the test run, not just
# an outcome that happens to hold today because of an incidental sandbox
# restriction — the whole reason the original `test_bedrock_client_explain_
# raises_llm_client_error_without_real_aws_access` test in test_llm_client.py
# was unsafe is that it relied on exactly that kind of ambient, un-enforced
# assumption. This autouse fixture makes it structural: ANY test in this
# suite that attempts a real outbound network connection (a DNS lookup for a
# non-loopback host, or a socket connect to a non-loopback address) fails
# immediately and loudly, while connections to localhost/127.0.0.1/::1 -
# including a test's own explicitly-created local test server - are left
# alone. This is deliberately socket-layer, not "block boto3 specifically":
# it protects every current and future test (Bedrock, or anything else)
# from accidentally reaching a real external service, not just the one
# client class this repair pass happened to be fixing.
# ---------------------------------------------------------------------------

_ALLOWED_NETWORK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}  # noqa: S104 (test guard, not a bind)


class ExternalNetworkAccessBlocked(RuntimeError):
    """Raised by the autouse network guard below when a test attempts to
    reach a non-loopback host. If you hit this in a new test, the fix is
    almost always to mock/stub the external call (see
    `tests/test_llm_client.py`'s Stubber-based Bedrock tests for the
    pattern) — not to widen `_ALLOWED_NETWORK_HOSTS`."""


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is not None and host not in _ALLOWED_NETWORK_HOSTS:
        raise ExternalNetworkAccessBlocked(
            f"Test attempted a DNS lookup for external host {host!r}. "
            "The local test suite must never reach real external services "
            "(see conftest.py's network-guard fixture docstring)."
        )
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_socket_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _ALLOWED_NETWORK_HOSTS:
        raise ExternalNetworkAccessBlocked(
            f"Test attempted a real outbound connection to {address!r}. "
            "The local test suite must never reach real external services "
            "(see conftest.py's network-guard fixture docstring)."
        )
    return _REAL_SOCKET_CONNECT(self, address)


_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def _block_external_network_access(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _guarded_socket_connect)
    yield


def race_state_at_lap(scenario: Scenario, lap_number: int, *, run_id: str | None = None, state_version: int = 0) -> RaceState:
    """Build a canonical RaceState directly from the scenario file for a
    given lap number, without running the simulator loop. Used by strategy
    tests that need race state at a specific lap synchronously."""
    scenario_lap = scenario.lap_by_number(lap_number)
    if scenario_lap is None:
        raise ValueError(f"Scenario has no lap {lap_number}")

    drivers = []
    for car_id, car_config in scenario.cars.items():
        lap_data = scenario_lap.drivers[car_id]
        drivers.append(
            DriverState(
                driver_id=car_id,
                display_name=car_config.display_name,
                team_name=car_config.team_name,
                position=lap_data.position,
                gap_to_leader_ms=lap_data.gap_to_leader_ms,
                gap_to_ahead_ms=0,
                compound=car_config.compound,
                tire_age_laps=lap_data.tire_age_laps,
                base_pace_ms=car_config.base_pace_ms,
                current_lap_pace_ms=lap_data.current_lap_pace_ms,
                pit_stops=car_config.pit_stops,
                pit_window=car_config.pit_window,
                traffic_factor=lap_data.traffic_factor,
            )
        )
    drivers.sort(key=lambda d: d.position)

    return RaceState(
        race_id=scenario.race_id,
        run_id=run_id or str(uuid.uuid4()),
        state_version=state_version,
        lap=lap_number,
        total_laps=scenario.total_laps,
        race_time_ms=scenario_lap.race_time_ms,
        track_state=scenario.track_state,
        generated_at=utc_now(),
        scenario_id=scenario.scenario_id,
        synthetic=True,
        drivers=tuple(drivers),
    )


# Tests use a short but non-zero real interval (instead of the scenario's
# production 2-second cadence) so the demo can be exercised deterministically
# and quickly, while still leaving enough real time between ticks for
# assertions like "pause happened between tick N and tick N+1" to be
# meaningful and non-flaky.
TEST_TICK_SECONDS = 0.03


@pytest.fixture
def scenario():
    return load_scenario(SCENARIO_PATH)


@pytest_asyncio.fixture
async def simulator(scenario):
    repository = InMemoryRaceStateRepository()
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        sleep_fn=asyncio.sleep,
        tick_interval_seconds=TEST_TICK_SECONDS,
    )
    await service.ensure_initialized()
    yield service
    await service._cancel_active_loop()  # noqa: SLF001 test cleanup


@pytest.fixture
def strategy_engine(scenario):
    return StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)


@pytest.fixture
def watch_repository():
    return InMemoryWatchRepository()


@pytest.fixture
def alert_repository():
    return InMemoryAlertRepository()


@pytest.fixture
def watch_evaluator(strategy_engine, watch_repository, alert_repository):
    return WatchEvaluator(
        watch_repository=watch_repository,
        alert_repository=alert_repository,
        strategy_engine=strategy_engine,
    )


@pytest.fixture
def knowledge_base():
    return KnowledgeBaseService(DEFAULT_KNOWLEDGE_BASE_DIR, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)


@pytest.fixture
def template_llm_client():
    return TemplateLLMClient()


@pytest.fixture
def explanation_engine(knowledge_base, template_llm_client):
    return ExplanationEngine(knowledge_base=knowledge_base, llm_client=template_llm_client)


@pytest_asyncio.fixture
async def api_client(scenario):
    """An HTTP client wired to a real FastAPI app, but with the simulator's
    tick interval sped up so tests don't wait 2 real seconds per lap."""
    app = create_app()

    async with app.router.lifespan_context(app):
        original_service: SimulatorService = app.state.simulator_service
        fast_service = SimulatorService(
            repository=original_service._repository,  # noqa: SLF001 reuse already-seeded state
            scenario=scenario,
            sleep_fn=asyncio.sleep,
            tick_interval_seconds=TEST_TICK_SECONDS,
            on_state_committed=original_service._on_state_committed,  # noqa: SLF001 keep watch-evaluator wiring
        )
        app.state.simulator_service = fast_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, fast_service

        await fast_service._cancel_active_loop()  # noqa: SLF001 test cleanup
