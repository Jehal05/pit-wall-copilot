r"""Test fixtures for the MCP server.

These tests deliberately exercise the MCP server against a REAL, separately
running instance of the backend (a subprocess running its own uvicorn, in
its own virtual environment) rather than importing the FastAPI app
in-process. Two reasons:

1. `mcp_server/` and `backend/` are intentionally isolated virtual
   environments (see server.py's module docstring — the `mcp` SDK's
   Starlette version can drift from the pinned FastAPI/Starlette in
   `backend/requirements.txt`), so this test suite cannot import
   `app.main` directly without risking that exact conflict.
2. It is also a more honest integration test: in any real deployment, the
   MCP server and the REST API are two independently-running services
   talking over real HTTP, not two halves of one process — this test
   suite exercises exactly that shape.

Spec v1.1 repair 7: "CI must fail if MCP integration tests are skipped
unexpectedly." The previous version of this fixture silently
`pytest.skip`d the entire suite if `backend/.venv` was missing — in a CI
environment that forgot to set up that venv, every test in this suite
would report as "skipped," which looks like "nothing to worry about" in a
CI summary rather than "this suite never actually ran." That is now a
hard failure by default; set `MCP_TESTS_ALLOW_MISSING_BACKEND_VENV=1`
explicitly (for a local/interactive environment that genuinely does not
have the backend venv set up yet) to get the old skip behavior back.

Spec v1.1 pass 3, correction 3: which Python interpreter launches the
backend subprocess is no longer hardcoded to `backend/.venv/bin/python`
(a Unix-only, name-specific assumption). See
`scripts/testing_support/resolve_backend_python.py` for the full
resolution order (PIT_WALL_BACKEND_PYTHON env var, then a platform-
appropriate `.venv`, then the current interpreter if it can import the
backend's dependencies, then a clear failure) — this file only wires that
shared resolver in and reports the outcome.

    PIT_WALL_BACKEND_PYTHON=/absolute/path/to/python python -m pytest

works on any platform. On Windows PowerShell:

    $env:PIT_WALL_BACKEND_PYTHON = "C:\path\to\python.exe"
    python -m pytest
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

MCP_SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = MCP_SERVER_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing_support.resolve_backend_python import (  # noqa: E402
    BackendPythonNotFoundError,
    resolve_backend_python,
)

# Spec v1.1 repair 11: "Local test credentials must be generated or
# supplied only within test fixtures" — generated fresh per test session
# and passed to the backend subprocess via DEMO_ACCESS_TOKEN, exactly
# like backend/tests/conftest.py's own TEST_DEMO_ACCESS_TOKEN. There is
# deliberately no hardcoded token anywhere in this file anymore.
TEST_DEMO_ACCESS_TOKEN = secrets.token_urlsafe(16)
AUTH_HEADERS = {"X-Demo-Access-Token": TEST_DEMO_ACCESS_TOKEN}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:  # noqa: ARG001
    """Spec v1.1 pass 3, correction 10: every test in this suite spins up a
    real backend subprocess (and, in test_streamable_http_transport.py, a
    real Streamable HTTP server on a real TCP port too) — tagged
    `integration` uniformly rather than per-function."""
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def backend_base_url():
    try:
        backend_python = resolve_backend_python(BACKEND_DIR)
    except BackendPythonNotFoundError as exc:
        if os.environ.get("MCP_TESTS_ALLOW_MISSING_BACKEND_VENV") == "1":
            pytest.skip(
                f"{exc} — MCP_TESTS_ALLOW_MISSING_BACKEND_VENV=1 is set, so "
                "skipping instead of failing. Unset it (or supply a usable "
                "interpreter) to get real coverage."
            )
        pytest.fail(
            f"{exc} This suite requires a real running backend (see this "
            "file's module docstring) and FAILS rather than skips by "
            "default, per spec v1.1 repair 7 ('CI must fail if MCP "
            "integration tests are skipped unexpectedly'). Set "
            "MCP_TESTS_ALLOW_MISSING_BACKEND_VENV=1 to explicitly opt into "
            "skipping in an environment that knows it doesn't have a usable "
            "interpreter yet."
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


@pytest_asyncio.fixture
async def reset_race(backend_base_url):
    """Resets the demo race before each test so watches/alerts/laps don't
    leak between tests sharing the one session-scoped backend process."""
    async with httpx.AsyncClient(base_url=backend_base_url, timeout=10.0) as client:
        await client.post("/v1/demo/reset", headers=AUTH_HEADERS)
    return backend_base_url


@pytest_asyncio.fixture
async def mcp_server(reset_race):
    from server import create_mcp_server

    async with httpx.AsyncClient(base_url=reset_race, timeout=10.0) as client:
        yield create_mcp_server(client=client, base_url=reset_race, demo_access_token=TEST_DEMO_ACCESS_TOKEN)
