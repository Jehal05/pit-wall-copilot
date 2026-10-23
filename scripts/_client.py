"""Small shared HTTP client helper for the scripts in this directory
(spec v1.1 repair 15). Not a test fixture — these scripts run against a
REAL, already-running backend process (local or deployed), unlike
backend/tests/*, which spin up their own subprocess per test session.

Every script here is read/observe-heavy and only calls the small set of
mutating endpoints the golden demo itself needs (demo start/reset,
create_strategy_watch) — none of them touch DynamoDB, Lambda, or any
other AWS resource directly; they only ever speak to the backend's own
public HTTP API, exactly like Fire TV, the Alexa clients, and mcp_server
all do.
"""

from __future__ import annotations

import os
import sys

import httpx

DEFAULT_BASE_URL = os.environ.get("PIT_WALL_API_BASE_URL", "http://localhost:8000")
DEFAULT_RACE_ID = os.environ.get("PIT_WALL_RACE_ID", "demo-race-001")


def build_client(base_url: str = DEFAULT_BASE_URL, *, demo_access_token: str | None = None) -> httpx.Client:
    headers = {}
    if demo_access_token:
        headers["X-Demo-Access-Token"] = demo_access_token
    return httpx.Client(base_url=base_url, headers=headers, timeout=10.0)


def require_demo_access_token() -> str:
    """Spec v1.1 repair 11: no default/bundled token anywhere in this repo.
    These scripts call mutating endpoints (demo start/reset, create
    watch), so they need a real token exactly like any other caller —
    read from PIT_WALL_DEMO_ACCESS_TOKEN, never hardcoded, never with a
    fallback default."""
    token = os.environ.get("PIT_WALL_DEMO_ACCESS_TOKEN")
    if not token:
        print(
            "PIT_WALL_DEMO_ACCESS_TOKEN is not set. Set it to the running "
            "backend's configured demo access token (see .env.example / "
            "docker-compose.yml's LOCAL_DEMO_AUTH_MODE note for how a "
            "local backend prints one at startup) before running this "
            "script.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)
