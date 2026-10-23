"""Pit Wall Copilot MCP server — spec v1.1 repair 7.

A thin MCP adapter over the backend REST API (`backend/app/api/routes.py`).
Every tool below is a direct HTTP call to the already-tested, already
deterministic FastAPI backend — this module contains **no strategy math,
no watch/alert logic, no regulation matching, and no LLM prompting of its
own**. All backend guarantees (deterministic calculations, idempotent
alerts, grounded explanations, the get_regulation found/evidence contract)
apply completely unchanged here, because nothing here re-implements them.

Spec v1.1 repair 7 rewrites this server around three requirements that
the earlier (Gate 4) version did not meet:

1. **Exactly 5 P0 domain tools.** `Alexa+`'s (and any other MCP host's)
   PRIMARY integration surface is exactly: `get_race_state`,
   `calculate_undercut`, `simulate_strategy`, `create_strategy_watch`,
   `get_regulation`. The earlier version additionally advertised
   `list_strategy_watches`, `list_alerts`, `get_alert` and
   `explain_alert` as MCP tools — those REST endpoints still exist and
   are still useful to a human debugging via curl or a future Fire TV
   feature, but per the spec they "must not be advertised as additional
   P0 MCP domain tools," so they are deliberately NOT registered here.
2. **Streamable HTTP transport, not stdio.** `create_mcp_server()` still
   returns a `FastMCP` instance usable in-process (tests call
   `.call_tool()` directly, matching the previous test suite's pattern),
   but `main()` now serves it over Streamable HTTP
   (`mcp.server.fastmcp.FastMCP.run(transport="streamable-http")`),
   which is what makes this server usable as a real network service
   AgentCore (or any other Streamable-HTTP-speaking MCP host, including
   Alexa+) can reach — a stdio server can only be launched as a local
   subprocess by whatever process started it, which an AgentCore Gateway
   or a hosted Alexa+ skill cannot do.
3. **Protocol version, health/readiness, auth.** The installed `mcp` SDK
   (pinned in requirements.txt) implements protocol version 2025-11-25 —
   see `mcp.types.LATEST_PROTOCOL_VERSION`, asserted in
   `tests/test_mcp_tools.py`. `/health` and `/ready` are added as plain
   HTTP routes alongside the MCP endpoint (`custom_route`, below) so a
   load balancer / AgentCore Gateway can probe this service without
   needing to speak MCP. See `_AuthMiddleware`'s docstring for the auth
   design and its honestly-documented scope limit.

Run standalone (talks to a backend already running on PIT_WALL_API_BASE_URL):
    PIT_WALL_API_BASE_URL=http://localhost:8000 python server.py
"""

# NOTE: deliberately no `from __future__ import annotations` here, unlike
# the rest of this repository. The installed `mcp` SDK inspects each tool
# function's real parameter annotations at registration time rather than
# resolving PEP 563 string annotations first; with postponed evaluation
# on, every annotation becomes a plain string and that inspection raises
# TypeError. Keeping annotations un-stringified here is a requirement of
# the SDK, not a style inconsistency.
import hmac
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tracing import configure_tracing, instrument_app, shutdown_tracing

DEFAULT_BASE_URL = os.environ.get("PIT_WALL_API_BASE_URL", "http://localhost:8000")
# Spec v1.1 repair 11's "no bundled credential" principle applies here
# too: there is no default value baked into source for the token this
# server forwards to the backend's mutating endpoints. A caller MUST
# supply PIT_WALL_DEMO_ACCESS_TOKEN (matching whatever the backend itself
# was configured with — see backend/app/main.py's LOCAL_DEMO_AUTH_MODE)
# or create_strategy_watch will fail closed with the backend's own
# auth_not_configured/unauthorized error, surfaced as a structured
# ToolError — never silently proceed with a guessed value.
DEFAULT_DEMO_ACCESS_TOKEN = os.environ.get("PIT_WALL_DEMO_ACCESS_TOKEN")

DEFAULT_MCP_HOST = os.environ.get("PIT_WALL_MCP_HOST", "127.0.0.1")
DEFAULT_MCP_PORT = int(os.environ.get("PIT_WALL_MCP_PORT", "9000"))

P0_TOOL_NAMES = frozenset(
    {"get_race_state", "calculate_undercut", "simulate_strategy", "create_strategy_watch", "get_regulation"}
)


class PitWallApiError(RuntimeError):
    """Raised when the backend REST API returns an error response, or is
    unreachable/times out. Carries a machine-readable `error` code and a
    `detail` payload through to the MCP tool caller as a structured
    error, rather than leaking a raw exception message — FastMCP
    surfaces this as a `ToolError` whose message is this exception's
    `str()`, which includes both fields below."""

    def __init__(self, error: str, detail: Any, *, status_code: Optional[int] = None):
        self.error = error
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"Pit Wall Copilot API error [{error}] (status={status_code}): {detail}")


class _AuthMiddleware(BaseHTTPMiddleware):
    """Bearer-token check on the MCP endpoint itself (spec v1.1
    correction 3: "require bearer auth on the MCP endpoint").

    In a real AgentCore (or API Gateway) deployment, identity and
    authorization may ADDITIONALLY be enforced by the gateway/runtime
    layer in front of this service — this server cannot honestly claim to
    implement or have tested that gateway-level auth (no AgentCore
    account is available in this environment; see
    IMPLEMENTATION_STATUS.md). What IS implemented and tested here is
    this server's own half, which is required and non-optional whenever
    `PIT_WALL_MCP_ACCESS_TOKEN` is configured (as
    infrastructure/template.yaml now always does for the deployed ECS
    task — see McpTaskDefinition's `Secrets`, correction 3): every
    request to the MCP endpoint (not `/health`/`/ready`, which must stay
    probable by an ALB health check without credentials — and which never
    echo the configured token in their response bodies) must present it
    as `Authorization: Bearer <token>`, or the request is refused with
    401 before it reaches any tool, MCP initialize, or tool listing.

    If the env var is unset (no deployed secret at all — only possible in
    local/dev use of this module directly, never through the shipped
    template), this check is a documented no-op, matching the posture
    `LOCAL_DEMO_AUTH_MODE` already documents for the backend's own
    equivalent check. A real deployment always sets this env var (from
    Secrets Manager, never a literal in source/template) AND MAY
    additionally terminate auth at an AgentCore Gateway / API Gateway in
    front of this service.

    Every header comparison below uses `hmac.compare_digest` (constant-
    time) rather than `==`, so response timing cannot be used to guess the
    token byte-by-byte. No branch of `dispatch` ever includes the
    configured token — or any substring of the presented header — in a
    response body, log line, or exception message; only the fixed literal
    strings below are ever returned to the caller.
    """

    _BEARER_PREFIX = "Bearer "

    def __init__(self, app, *, required_token: Optional[str], mcp_path: str):
        super().__init__(app)
        self._required_token = required_token
        self._mcp_path = mcp_path

    def _unauthorized(self, message: str) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": message})

    async def dispatch(self, request: Request, call_next):
        if self._required_token is None or request.url.path != self._mcp_path:
            return await call_next(request)

        header = request.headers.get("authorization")
        if header is None:
            return self._unauthorized("Missing Authorization header. Expected: Authorization: Bearer <token>.")
        if not header.startswith(self._BEARER_PREFIX):
            return self._unauthorized("Malformed Authorization header. Expected: Authorization: Bearer <token>.")

        presented_token = header[len(self._BEARER_PREFIX):]
        # Constant-time comparison: an ordinary `==`/`!=` on strings short-
        # circuits at the first differing byte, which is (in principle)
        # measurable and can leak the correct token one byte at a time
        # over many requests. `hmac.compare_digest` always takes the same
        # time regardless of where (or whether) the strings differ.
        if not hmac.compare_digest(presented_token, self._required_token):
            return self._unauthorized("Invalid bearer token.")

        return await call_next(request)


def create_mcp_server(
    *,
    client: Optional[httpx.AsyncClient] = None,
    base_url: str = DEFAULT_BASE_URL,
    demo_access_token: Optional[str] = DEFAULT_DEMO_ACCESS_TOKEN,
) -> FastMCP:
    """Builds the MCP server and registers exactly the 5 P0 domain tools.

    `client` is injectable specifically so tests can point this server at
    an in-process ASGI test app (via `httpx.ASGITransport`) instead of a
    real HTTP port — see `tests/conftest.py`.
    """
    http_client = client or httpx.AsyncClient(base_url=base_url, timeout=10.0)
    auth_headers = {"X-Demo-Access-Token": demo_access_token} if demo_access_token else {}

    mcp = FastMCP(
        "pit-wall-copilot",
        host=DEFAULT_MCP_HOST,
        port=DEFAULT_MCP_PORT,
        instructions=(
            "Tools for Pit Wall Copilot, a deterministic race-strategy calculator. "
            "Every numeric result (margins, probabilities, deltas) is produced by "
            "seeded, reproducible calculations on the backend — these tools never "
            "invent race state or strategy numbers themselves. get_regulation only "
            "ever returns evidence that is actually present in the sporting-rules "
            "corpus; if a query is not supported by that corpus, it returns "
            "found=false with no evidence rather than guessing at a rule."
        ),
    )

    async def _unwrap(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            error_code = detail.get("error", "api_error") if isinstance(detail, dict) else "api_error"
            raise PitWallApiError(error_code, detail, status_code=response.status_code)
        return response.json()

    async def _request(method: str, path: str, **kwargs) -> Any:
        """Wraps every outbound HTTP call so a network failure becomes a
        structured PitWallApiError (-> ToolError) instead of an
        unstructured raw exception — spec v1.1 repair 7's "structured
        errors" requirement, applied uniformly rather than per-tool."""
        try:
            response = await http_client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PitWallApiError("upstream_timeout", str(exc)) from exc
        except httpx.ConnectError as exc:
            raise PitWallApiError("upstream_unreachable", str(exc)) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - catch-all for other transport failures
            raise PitWallApiError("upstream_request_failed", str(exc)) from exc
        return await _unwrap(response)

    @mcp.tool()
    async def get_race_state(race_id: str) -> dict[str, Any]:
        """Returns the current canonical race state: lap, per-driver gaps,
        tire compound/age, and track status (green/yellow/safety car/red).
        Use this first to find current candidate/target driver ids."""
        return await _request("GET", f"/v1/races/{race_id}/state")

    @mcp.tool()
    async def calculate_undercut(race_id: str, candidate_driver: str, target_driver: str) -> dict[str, Any]:
        """Deterministically calculates whether candidate_driver can undercut
        target_driver right now: pits candidate next lap, target responds the
        lap after. Returns expected_margin_ms (positive = candidate ends up
        ahead), window ("OPEN" or "CLOSED"), and a seeded 200-trial Monte
        Carlo confidence result. Purely a calculation over current race
        state — never invents or guesses a number."""
        return await _request(
            "POST",
            f"/v1/races/{race_id}/strategy/undercut",
            json={"candidate_driver": candidate_driver, "target_driver": target_driver},
        )

    @mcp.tool()
    async def simulate_strategy(
        race_id: str, driver_id: str, wait_laps: int = 3, compound: Optional[str] = None
    ) -> dict[str, Any]:
        """Simulates PIT NOW vs WAIT wait_laps more laps for one driver.
        Returns delta_ms (positive means pitting now is faster by that
        margin) and a confidence result. Optionally override the compound
        fitted at the stop ("SOFT", "MEDIUM" or "HARD")."""
        payload: dict[str, Any] = {"driver_id": driver_id, "wait_laps": wait_laps}
        if compound is not None:
            payload["compound"] = compound
        return await _request("POST", f"/v1/races/{race_id}/strategy/simulate", json=payload)

    @mcp.tool()
    async def create_strategy_watch(race_id: str, candidate_driver: str, target_driver: str) -> dict[str, Any]:
        """Creates a persistent undercut watch on candidate_driver vs
        target_driver. The watch is automatically re-evaluated on every
        race-state change and raises exactly one alert the moment the
        undercut window opens or closes (hysteresis prevents oscillation).
        Returns the watch's id and its current (baseline) status. Requires
        a configured demo-access token (PIT_WALL_DEMO_ACCESS_TOKEN) — this
        is the only one of the 5 P0 tools that mutates backend state."""
        return await _request(
            "POST",
            f"/v1/races/{race_id}/watches",
            json={"candidate_driver": candidate_driver, "target_driver": target_driver},
            headers=auth_headers,
        )

    @mcp.tool()
    async def get_regulation(query: str) -> dict[str, Any]:
        """Answers a direct factual question about this demo's fictional
        sporting rules. Returns {"found": bool, "query": str, "evidence":
        [...], "available": bool}: when found=true, evidence contains one
        or more items each with document_id/section_id/title/version/
        support_status/excerpt that concretely support the answer. When
        found=false, evidence is always empty — this tool NEVER fabricates
        a rule it wasn't given; a caller (including an LLM narrating a
        voice response) must not state a rule as fact when found=false,
        and should say the information is unavailable instead.

        `available=false` (spec v1.1 repair 14, Section 29 "Knowledge Base
        unavailable") is a distinct, stronger signal than an ordinary
        found=false: it means the regulation source itself could not be
        reached at all, not just that this particular query isn't
        supported by it. A caller SHOULD render the literal degraded-mode
        message "Regulation source unavailable. I won't infer the rule."
        in that case rather than the generic unsupported-query wording."""
        return await _request("GET", "/v1/regulations", params={"query": query})

    return mcp


def build_streamable_http_app(mcp: FastMCP, *, backend_base_url: str = DEFAULT_BASE_URL) -> Starlette:
    """Wraps `mcp.streamable_http_app()` with the auth middleware and adds
    `/health`/`/ready` — see `_AuthMiddleware`'s docstring. `/health` is a
    pure liveness probe (this process is up); `/ready` additionally
    confirms the configured backend is reachable, since a healthy-but-
    backend-unreachable MCP server should not be reported ready to route
    traffic to."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "tools": sorted(P0_TOOL_NAMES)})

    @mcp.custom_route("/ready", methods=["GET"])
    async def ready(_request: Request) -> Response:
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                response = await probe.get(f"{backend_base_url}/health")
            backend_ok = response.status_code == 200
        except httpx.HTTPError:
            backend_ok = False
        if not backend_ok:
            return JSONResponse({"status": "not_ready", "backend": "unreachable"}, status_code=503)
        return JSONResponse({"status": "ready", "backend": "reachable"})

    app = mcp.streamable_http_app()
    required_token = os.environ.get("PIT_WALL_MCP_ACCESS_TOKEN")
    app.add_middleware(_AuthMiddleware, required_token=required_token, mcp_path=mcp.settings.streamable_http_path)
    # Repair pass, mandatory correction 4: real end-to-end tracing.
    # `configure_tracing()` is idempotent/no-op unless
    # PIT_WALL_MCP_OTEL_TRACING_ENABLED is set (see tracing.py), so every
    # existing caller of this function — including every test in this
    # suite, and a bare `python server.py` with no tracing env vars set —
    # is completely unaffected. `instrument_app` must run AFTER
    # `configure_tracing` (it needs a TracerProvider already installed)
    # and wraps `app` in an ASGI middleware rather than mutating it, so
    # the wrapped value is what must actually be served — see
    # `main()` below and `instrument_app`'s own docstring.
    runtime = configure_tracing()
    return instrument_app(app, enabled=runtime.enabled, tracer_provider=runtime.tracer_provider)


def main() -> None:
    import uvicorn

    server = create_mcp_server()
    # Streamable HTTP, not stdio — see module docstring point 2. Served
    # via `build_streamable_http_app` (not `FastMCP.run()` directly) so
    # the /health, /ready and auth-middleware wrapping above are actually
    # part of what's served, not bypassed by FastMCP's own app-building.
    app = build_streamable_http_app(server, backend_base_url=DEFAULT_BASE_URL)
    # Spec v1.1 pass 4, mandatory correction 8: graceful shutdown — same
    # reasoning as backend/Dockerfile's own uvicorn CMD (this process is
    # also PID 1 in mcp_server/Dockerfile's container, with no separate
    # init process), bounded below infrastructure/template.yaml's ECS
    # task-definition StopTimeout for this service.
    try:
        uvicorn.run(app, host=DEFAULT_MCP_HOST, port=DEFAULT_MCP_PORT, timeout_graceful_shutdown=25)
    finally:
        # Repair pass, mandatory correction 4: flush any buffered spans
        # before this process actually exits — a no-op when tracing was
        # never enabled/configured (see tracing.py:shutdown_tracing).
        # Mirrors backend/app/main.py's lifespan calling
        # `shutdown_tracing()` on application shutdown; this module has no
        # FastAPI-style lifespan hook of its own (uvicorn.run is a
        # blocking call, not an ASGI lifespan event this app defines), so
        # the equivalent point is simply after `uvicorn.run` returns.
        shutdown_tracing()


if __name__ == "__main__":
    main()
