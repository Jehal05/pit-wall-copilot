"""FastAPI dependency providers.

The SimulatorService instance lives on `app.state` (one instance per running
application process, matching the "no two concurrent loops for the same
race" requirement) and is created once at startup from the configured
scenario + repository. Tests override `get_simulator_service` with a
per-test instance via `app.dependency_overrides`.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Header, Request, status

from app.config import Settings, get_settings
from app.observability import audit_log
from app.repositories.watch_base import AlertRepository, WatchRepository
from app.services.agent_service import AgentQueryService, RaceChangeTracker
from app.services.client_ip import get_client_ip
from app.services.device_pairing import DevicePairingService
from app.services.event_broadcaster import RaceEventBroadcaster
from app.services.event_broker import EventBroker
from app.services.explanation_engine import ExplanationEngine
from app.services.rate_limiter import FixedWindowRateLimiter
from app.services.regulation_service import RegulationService
from app.services.simulator import SimulatorService
from app.services.strategy_engine import StrategyEngine
from app.services.watch_evaluator import WatchEvaluator


def get_simulator_service(request: Request) -> SimulatorService:
    service: SimulatorService | None = getattr(request.app.state, "simulator_service", None)
    if service is None:
        raise RuntimeError("SimulatorService was not initialized on application startup")
    return service


def get_event_broker(request: Request) -> EventBroker:
    # AWS hackathon-final pass, phase 4/5: used by GET /health to report
    # (and, when EVENT_BROKER=redis, gate readiness on) whether the
    # configured cross-instance event broker is actually connected.
    broker: EventBroker | None = getattr(request.app.state, "event_broker", None)
    if broker is None:
        raise RuntimeError("EventBroker was not initialized on application startup")
    return broker


def get_strategy_engine(request: Request) -> StrategyEngine:
    engine: StrategyEngine | None = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise RuntimeError("StrategyEngine was not initialized on application startup")
    return engine


def get_watch_evaluator(request: Request) -> WatchEvaluator:
    evaluator: WatchEvaluator | None = getattr(request.app.state, "watch_evaluator", None)
    if evaluator is None:
        raise RuntimeError("WatchEvaluator was not initialized on application startup")
    return evaluator


def get_watch_repository(request: Request) -> WatchRepository:
    repo: WatchRepository | None = getattr(request.app.state, "watch_repository", None)
    if repo is None:
        raise RuntimeError("WatchRepository was not initialized on application startup")
    return repo


def get_alert_repository(request: Request) -> AlertRepository:
    repo: AlertRepository | None = getattr(request.app.state, "alert_repository", None)
    if repo is None:
        raise RuntimeError("AlertRepository was not initialized on application startup")
    return repo


def get_explanation_engine(request: Request) -> ExplanationEngine:
    engine: ExplanationEngine | None = getattr(request.app.state, "explanation_engine", None)
    if engine is None:
        raise RuntimeError("ExplanationEngine was not initialized on application startup")
    return engine


def get_regulation_service(request: Request) -> RegulationService:
    service: RegulationService | None = getattr(request.app.state, "regulation_service", None)
    if service is None:
        raise RuntimeError("RegulationService was not initialized on application startup")
    return service


def get_event_broadcaster(request: Request) -> RaceEventBroadcaster:
    broadcaster: RaceEventBroadcaster | None = getattr(request.app.state, "event_broadcaster", None)
    if broadcaster is None:
        raise RuntimeError("RaceEventBroadcaster was not initialized on application startup")
    return broadcaster


def get_agent_query_service(request: Request) -> AgentQueryService:
    service: AgentQueryService | None = getattr(request.app.state, "agent_query_service", None)
    if service is None:
        raise RuntimeError("AgentQueryService was not initialized on application startup")
    return service


def get_race_change_tracker(request: Request) -> RaceChangeTracker:
    tracker: RaceChangeTracker | None = getattr(request.app.state, "race_change_tracker", None)
    if tracker is None:
        raise RuntimeError("RaceChangeTracker was not initialized on application startup")
    return tracker


def get_explain_rate_limiter(request: Request) -> FixedWindowRateLimiter:
    limiter: FixedWindowRateLimiter | None = getattr(request.app.state, "explain_rate_limiter", None)
    if limiter is None:
        raise RuntimeError("FixedWindowRateLimiter was not initialized on application startup")
    return limiter


def get_device_pairing_service(request: Request) -> DevicePairingService:
    service: DevicePairingService | None = getattr(request.app.state, "device_pairing_service", None)
    if service is None:
        raise RuntimeError("DevicePairingService was not initialized on application startup")
    return service


def enforce_explain_rate_limit(
    request: Request,
    limiter: FixedWindowRateLimiter = Depends(get_explain_rate_limiter),
    settings: Settings = Depends(get_settings),
) -> None:
    """Dependency for GET .../explain (spec 52 only requires the shared
    demo-access-token on *mutating* endpoints, so this read route has no
    per-user identity to key on — client IP is the practical key here;
    see rate_limiter.py's module docstring for why that's an honestly
    scoped choice, not a stand-in for real per-account throttling).

    AWS hackathon-final pass, P1-10: `get_client_ip` (not
    `request.client.host` directly) resolves the real originating client
    behind the ALB — see app/services/client_ip.py's module docstring.
    Without this, every client behind the deployed stack's ALB shares
    ONE rate-limit budget (the ALB's own peer address), so one busy
    client could exhaust every other client's Bedrock-call allowance."""
    client_host = get_client_ip(request, settings)
    allowed, retry_after = limiter.check(client_host)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "message": "Too many explanation requests. Please slow down and try again shortly.",
            },
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def credential_is_valid(app_state, token: str | None) -> bool:
    """Shared core of `require_demo_access_token`'s credential check —
    factored out (AWS hackathon-final pass, P2-12) so the WebSocket route
    (`app/api/routes.py`'s `race_websocket`, which cannot use a regular
    FastAPI `Depends()` HTTP dependency — see that route's own docstring)
    can apply the EXACT SAME rule rather than reimplementing it, or —
    the actual state of the code before this pass — not checking it at
    all. Accepts either the master token (constant-time compared) or a
    live, paired device token; see `require_demo_access_token`'s
    docstring for why both are accepted."""
    if token is None:
        return False
    configured_token: str | None = getattr(app_state, "effective_demo_access_token", None)
    if configured_token is not None and hmac.compare_digest(token, configured_token):
        return True
    pairing_service: DevicePairingService | None = getattr(app_state, "device_pairing_service", None)
    if pairing_service is not None and pairing_service.verify_device_token(token):
        return True
    return False


def require_demo_access_token(
    request: Request,
    x_demo_access_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Auth check for mutating endpoints (spec section 52 / spec v1.1 pass
    3 correction 5). Accepts EITHER of two credentials in the same
    `X-Demo-Access-Token` header:

      1. The master `DEMO_ACCESS_TOKEN` — the shared secret an
         administrator/CI holds, compared with `hmac.compare_digest` for a
         constant-time check (spec v1.1 repair 3's MCP bearer-token
         comparison uses the same pattern, for the same reason: an
         ordinary `!=` string comparison is not constant-time and leaks
         how many leading characters matched via timing).
      2. A device-specific token issued by a real pairing-code exchange
         (see `app/services/device_pairing.py` and the
         `/v1/auth/pairing-codes` / `/v1/auth/pair` routes) — this is what
         a paired Fire TV actually sends; it NEVER holds a copy of the
         master token (spec v1.1 pass 3 correction 5: "the backend
         master/demo token must NEVER be entered on the Fire TV").

    Spec v1.1 repair 11 — FAILS CLOSED, not open: if no master token was
    ever configured at all (no DEMO_ACCESS_TOKEN env var/secret, and
    LOCAL_DEMO_AUTH_MODE was not explicitly turned on), every mutating
    request is refused regardless of what header is sent — there is no
    way to accidentally deploy this backend with mutation endpoints open.
    A device token can still authorize requests even when no master token
    is configured (pairing/device credentials are independent secrets) —
    the 503 below applies specifically to the master-token path being
    unconfigured, matching the historical contract this dependency's
    tests already assert on.
    """
    configured_token: str | None = getattr(request.app.state, "effective_demo_access_token", None)
    if credential_is_valid(request.app.state, x_demo_access_token):
        return

    # Spec v1.1 pass 4, mandatory correction 7: audit every authentication
    # failure. Deliberately logs only the request path/client host/outcome
    # -- never the presented header value itself (whether it was absent,
    # garbage, or a near-miss token), since that value is exactly the kind
    # of secret-shaped data correction 7 says must never reach a log line.
    client_host = get_client_ip(request, settings)  # P1-10: trusted-proxy-aware, not the ALB's own peer address
    audit_log(
        "authentication_failed",
        correlation_id=getattr(request.state, "correlation_id", None),
        path=request.url.path,
        client_host=client_host,
        credential_presented=x_demo_access_token is not None,
    )

    if configured_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth_not_configured",
                "message": (
                    "No demo access token is configured on this server, so mutating endpoints are "
                    "disabled. Set DEMO_ACCESS_TOKEN (from a real secret store in a deployment) or, "
                    "for local development only, LOCAL_DEMO_AUTH_MODE=true."
                ),
            },
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": "Missing or invalid X-Demo-Access-Token header."},
    )


def require_master_demo_access_token(
    request: Request,
    x_demo_access_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """AWS hackathon-final pass, P0-1: administrator-only authorization.

    `require_demo_access_token` (above) deliberately accepts EITHER the
    master token OR a paired device's own token, because most mutating
    endpoints (start/pause/reset, watch creation) are things a paired Fire
    TV is meant to do on its own. But two operations are administrator
    actions a paired device must NEVER be able to perform on its own:

      1. `POST /v1/auth/pairing-codes` — minting a new pairing code. A
         device token accepted here would let an already-paired Fire TV
         (or anyone who stole one device's token) mint pairing codes for
         OTHER devices — privilege escalation from "one paired device" to
         "can pair arbitrary new devices," never intended.
      2. Administrative device revocation (`POST /v1/auth/revoke` with an
         explicit `device_id` — the "revoke someone else's device" path;
         self-revoke, which does not need this dependency, is unaffected).

    This dependency accepts ONLY the master token, compared with
    `hmac.compare_digest` for the same constant-time reasoning as
    `require_demo_access_token`. A syntactically valid, currently-active
    DEVICE token is explicitly REJECTED with 403 (not 401) — the
    credential is understood and valid for what it is, it simply does not
    authorize this operation, which is the correct HTTP semantics
    distinction between "who are you" (401) and "you are known, but not
    allowed to do this" (403). A missing, malformed, expired, or revoked
    credential still gets 401 (indistinguishable from "no credential"),
    and an unconfigured master token still fails closed with 503 —
    identical behavior to `require_demo_access_token` for those cases, so
    this dependency does not introduce a new way to accidentally expose a
    mutating endpoint.
    """
    configured_token: str | None = getattr(request.app.state, "effective_demo_access_token", None)
    client_host = get_client_ip(request, settings)  # P1-10: trusted-proxy-aware, not the ALB's own peer address
    correlation_id = getattr(request.state, "correlation_id", None)

    if configured_token is None:
        audit_log(
            "authentication_failed",
            correlation_id=correlation_id,
            path=request.url.path,
            client_host=client_host,
            credential_presented=x_demo_access_token is not None,
            reason="master_token_not_configured",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth_not_configured",
                "message": (
                    "No demo access token is configured on this server, so administrator-only "
                    "endpoints are disabled. Set DEMO_ACCESS_TOKEN (from a real secret store in a "
                    "deployment) or, for local development only, LOCAL_DEMO_AUTH_MODE=true."
                ),
            },
        )

    if x_demo_access_token is not None and hmac.compare_digest(x_demo_access_token, configured_token):
        return

    # A syntactically-presented credential that verifies as a valid DEVICE
    # token (but is not the master token) is a known, currently-active
    # credential — just not one authorized for an administrator-only
    # operation. 403, not 401, and audited distinctly so a device
    # attempting privilege escalation is visible in the audit trail as
    # exactly that, not conflated with an ordinary auth failure.
    if x_demo_access_token is not None:
        pairing_service: DevicePairingService | None = getattr(request.app.state, "device_pairing_service", None)
        if pairing_service is not None and pairing_service.verify_device_token(x_demo_access_token):
            audit_log(
                "administrator_authorization_denied",
                correlation_id=correlation_id,
                path=request.url.path,
                client_host=client_host,
                reason="valid_device_token_is_not_authorized_for_admin_operation",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "message": "A device token is not authorized for this administrator-only operation.",
                },
            )

    audit_log(
        "authentication_failed",
        correlation_id=correlation_id,
        path=request.url.path,
        client_host=client_host,
        credential_presented=x_demo_access_token is not None,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": "Missing or invalid X-Demo-Access-Token header."},
    )
