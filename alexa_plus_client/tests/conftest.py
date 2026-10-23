"""Test fixtures for the simulated Alexa+ client.

Spins up two REAL, independently-running services for the duration of
each test — exactly the shape spec v1.1 repair 10's primary architecture
describes (Alexa+ -> Streamable HTTP MCP -> five domain tools -> backend):

1. The real FastAPI backend, as its own subprocess in its OWN virtual
   environment (`backend/.venv`) — identical in spirit to
   `mcp_server/tests/conftest.py`'s `backend_base_url` fixture, and for
   the same two reasons documented there (venv isolation; a more honest
   integration test).
2. The real `mcp_server` Streamable HTTP app (`server.py`,
   `build_streamable_http_app`), served on a real TCP port in a
   background thread — identical in spirit to
   `mcp_server/tests/test_streamable_http_transport.py`'s
   `running_mcp_server` fixture. Imported directly from
   `mcp_server/server.py` via `sys.path`, since this package's own
   dependencies (`mcp==1.27.0`, `httpx==0.27.2` — see requirements.txt)
   are pinned to match `mcp_server/requirements.txt` exactly, so the
   cross-package import is safe (unlike `backend/`, which pins an
   independent, potentially-conflicting Starlette/FastAPI version — see
   mcp_server/tests/conftest.py's own docstring for why THAT boundary
   stays subprocess-only).

This test suite therefore exercises the simulated client end to end, over
a genuine MCP initialize -> tools/list -> tools/call round trip, against a
genuine backend — not against any mocked transport.

Spec v1.1 pass 3, correction 3: the interpreter used to launch the backend
subprocess is resolved via `scripts/testing_support/resolve_backend_python.py`
(PIT_WALL_BACKEND_PYTHON env var, then a platform-appropriate `.venv`, then
the current interpreter if usable, then a clear failure) rather than a
hardcoded `backend/.venv/bin/python` path — see that module's docstring for
the exact order, and `docs/deployment-runbook.md` / the module docstring in
`mcp_server/tests/conftest.py` for PowerShell/Unix examples.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import uvicorn

ALEXA_CLIENT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = ALEXA_CLIENT_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
MCP_SERVER_DIR = REPO_ROOT / "mcp_server"

sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(ALEXA_CLIENT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing_support.resolve_backend_python import (  # noqa: E402
    BackendPythonNotFoundError,
    resolve_backend_python,
)

# Spec v1.1 repair 11: no hardcoded token — generated fresh per test
# session, matching every other test suite's own conftest in this
# repository.
TEST_DEMO_ACCESS_TOKEN = secrets.token_urlsafe(16)
AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:  # noqa: ARG001
    """Spec v1.1 pass 3, correction 10: every test in this suite spins up a
    real backend subprocess and a real mcp_server Streamable HTTP server —
    tagged `integration` uniformly rather than per-function. The `device`
    marker is deliberately NOT applied here: `SimulatedAlexaPlusClient` is a
    simulated MCP client, not a real Alexa+ device/console (see
    REMAINING_EXTERNAL_VERIFICATION.md for that still-open item)."""
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def backend_base_url():
    try:
        backend_python = resolve_backend_python(BACKEND_DIR)
    except BackendPythonNotFoundError as exc:
        if os.environ.get("ALEXA_PLUS_TESTS_ALLOW_MISSING_BACKEND_VENV") == "1":
            pytest.skip(
                f"{exc} — ALEXA_PLUS_TESTS_ALLOW_MISSING_BACKEND_VENV=1 is "
                "set, so skipping instead of failing."
            )
        pytest.fail(
            f"{exc} This suite requires a real running backend. Set "
            "ALEXA_PLUS_TESTS_ALLOW_MISSING_BACKEND_VENV=1 to explicitly "
            "opt into skipping in an environment that knows it doesn't "
            "have a usable interpreter yet."
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DEMO_ACCESS_TOKEN": TEST_DEMO_ACCESS_TOKEN}
    process = subprocess.Popen(
        [backend_python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{base_url}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(0.2)
        else:
            process.terminate()
            raise RuntimeError(f"backend did not become healthy within timeout: {last_error}")

        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def backend_auth_headers():
    return dict(AUTH_HEADERS)


@pytest_asyncio.fixture
async def reset_race(backend_base_url):
    """Resets the demo race before each test — same reason as
    mcp_server/tests/conftest.py's identical fixture: watches/alerts/laps
    must not leak between tests sharing one session-scoped backend."""
    async with httpx.AsyncClient(base_url=backend_base_url, timeout=10.0) as client:
        await client.post("/v1/demo/reset", headers=AUTH_HEADERS)
    return backend_base_url


@pytest_asyncio.fixture
async def running_mcp_server(reset_race) -> AsyncIterator[str]:
    """Serves the REAL mcp_server Streamable HTTP app on an ephemeral
    port, in a background thread, pointed at the real backend
    subprocess."""
    import server as mcp_server_module  # mcp_server/server.py, via sys.path above

    backend_client = httpx.AsyncClient(base_url=reset_race, timeout=10.0)
    mcp = mcp_server_module.create_mcp_server(
        client=backend_client, base_url=reset_race, demo_access_token=TEST_DEMO_ACCESS_TOKEN
    )
    app = mcp_server_module.build_streamable_http_app(mcp, backend_base_url=reset_race)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server_instance = uvicorn.Server(config)

    thread = threading.Thread(target=server_instance.run, daemon=True)
    thread.start()

    deadline = asyncio.get_event_loop().time() + 10
    while not server_instance.started and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert server_instance.started, "mcp_server did not start within timeout"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server_instance.should_exit = True
        thread.join(timeout=5)
        try:
            await backend_client.aclose()
        except RuntimeError:
            pass


@pytest_asyncio.fixture
def alexa_client_factory(running_mcp_server, reset_race):
    """Returns an async context manager factory for a connected
    SimulatedAlexaPlusClient — a real MCP session, plus the REST client
    for the `why_now` journey's two non-MCP endpoints, both pointed at
    the same real backend.

    Deliberately NOT a plain `yield`-based async fixture: entering and
    exiting `connected_session`'s `streamable_http_client`/`ClientSession`
    context managers across a pytest-asyncio fixture's separate setup and
    teardown steps trips anyio's "exit cancel scope in a different task
    than it was entered in" — a known artifact of mixing anyio task
    groups with pytest-asyncio's function-scoped event loop across a
    fixture boundary (see
    mcp_server/tests/test_streamable_http_transport.py's own
    `running_mcp_server` fixture docstring for the same class of issue).
    Each test instead opens and fully closes its own session INSIDE its
    own single coroutine/task via `async with alexa_client_factory() as
    alexa_client:`, exactly like that same test file's working
    `test_real_initialize_tools_list_and_tool_call_over_streamable_http`
    does directly with `streamable_http_client`/`ClientSession`.
    """
    from simulated_client import SimulatedAlexaPlusClient, connected_session

    @asynccontextmanager
    async def factory():
        async with connected_session(running_mcp_server) as session:
            client = SimulatedAlexaPlusClient(session, race_id="demo-race-001", backend_base_url=reset_race)
            try:
                yield client
            finally:
                await client.aclose()

    return factory
