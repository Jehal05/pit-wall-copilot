from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from starlette.concurrency import run_in_threadpool

from pydantic import BaseModel, Field

from app.api.deps import (
    credential_is_valid,
    enforce_explain_rate_limit,
    get_agent_query_service,
    get_alert_repository,
    get_device_pairing_service,
    get_event_broadcaster,
    get_event_broker,
    get_explanation_engine,
    get_race_change_tracker,
    get_regulation_service,
    get_simulator_service,
    get_strategy_engine,
    get_watch_evaluator,
    get_watch_repository,
    require_demo_access_token,
    require_master_demo_access_token,
)
from app.api.schemas import (
    AgentQueryRequest,
    CreateWatchRequest,
    DemoStatusResponse,
    ErrorResponse,
    HealthResponse,
    SimulateStrategyRequest,
    UndercutRequest,
)
from app.config import Settings, get_settings
from app.domain.agent_models import AgentQueryResult, RaceChangesResult
from app.domain.explanation_models import ExplanationResult
from app.domain.models import DemoControlState, RaceEvent, RaceEventType, RaceState, SimulatorStatus, utc_now
from app.domain.regulation_models import RegulationAnswer
from app.domain.strategy_models import StrategySimulationResult, UndercutResult
from app.domain.watch_models import Alert, Watch
from app.metrics import emit_pairing_outcome
from app.observability import audit_log
from app.repositories.pairing_dynamodb import DynamoDBPairingRateLimiter, DynamoDBPairingRateLimiterExceeded
from app.services.event_broker import EventBroker
from app.repositories.watch_base import AlertRepository, WatchRepository
from app.services.agent_service import AgentQueryService, RaceChangeTracker, build_race_changes_result
from app.services.client_ip import get_client_ip
from app.services.device_pairing import (
    DevicePairingService,
    PairingAttemptRateLimiter,
    PairingCodeGenerationExhaustedError,
    PairingCodeRateLimitedError,
)
from app.services.event_broadcaster import RaceEventBroadcaster
from app.services.explanation_engine import ExplanationEngine
from app.services.regulation_service import RegulationService
from app.services.simulator import SimulatorService, UnknownRaceError
from app.services.strategy_engine import StrategyEngine, StrategyValidationError
from app.services.watch_evaluator import WatchEvaluator

# The rate limiter injected via `_pairing_rate_limiter` is either the
# in-process `PairingAttemptRateLimiter` (STATE_REPOSITORY=memory) or the
# distributed `DynamoDBPairingRateLimiter` (STATE_REPOSITORY=dynamodb) --
# see `app/repositories/factory.py:build_pairing_rate_limiter`. Both
# expose the same `check_and_record(key)` interface but raise different
# exception types, so routes.py types against their union and catches
# both exceptions as one 429 outcome below.
#
# Spec v1.1 pass 4, mandatory correction 11 (security-scan action item):
# this block was previously interleaved between two halves of the import
# list, which `ruff` (newly added to the CI security-scan gate this pass
# — see .github/workflows/ci.yml) correctly flags as E402 ("module level
# import not at top of file") for every import statement below it. Moved
# below the full import list, with no change to the values themselves.
PairingRateLimiter = Union[PairingAttemptRateLimiter, DynamoDBPairingRateLimiter]
_PAIRING_RATE_LIMIT_EXCEPTIONS = (PairingCodeRateLimitedError, DynamoDBPairingRateLimiterExceeded)

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(
    response: Response,
    settings: Settings = Depends(get_settings),
    event_broker: EventBroker = Depends(get_event_broker),
) -> HealthResponse:
    """AWS hackathon-final pass, phase 4/5: this is the ECS task's own
    ALB health-check target AND the source `BackendService`'s
    `DeploymentCircuitBreaker` reads to decide whether a rolling
    deployment reached a healthy steady state (see
    infrastructure/template.yaml). Pre-phase-4 this endpoint reported
    "ok" unconditionally, regardless of whether the required
    cross-instance event broker (EVENT_BROKER=redis, whenever
    BackendDesiredCount > 1 — see the BackendDesiredCountRequiresCrossInstanceBroker
    Rule) was actually connected. "do not mark backend ready for
    multi-instance traffic while required broker is unavailable" (phase 4
    requirement) is enforced HERE: when EVENT_BROKER=redis and the broker
    is currently disconnected, this now returns HTTP 503 with
    `status="degraded"` rather than 200/"ok" — a real ECS target group
    then correctly stops routing traffic to this task, and (with more
    than one task running) other healthy tasks keep serving while this
    one either reconnects or is replaced. EVENT_BROKER=memory (single
    -instance/local-dev — no cross-instance requirement to fail) always
    reports connected (`InMemoryEventBroker.is_connected` is trivially
    True — see event_broker.py)."""
    connected = event_broker.is_connected
    broker_required = settings.event_broker == "redis"
    if broker_required and not connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="degraded",
            app_version=settings.app_version,
            timestamp=utc_now(),
            event_broker=settings.event_broker,
            event_broker_connected=connected,
        )
    return HealthResponse(
        status="ok",
        app_version=settings.app_version,
        timestamp=utc_now(),
        event_broker=settings.event_broker,
        event_broker_connected=connected,
    )


@router.get(
    "/v1/races/{race_id}/state",
    response_model=RaceState,
    responses={404: {"model": ErrorResponse}},
    tags=["race"],
)
async def get_race_state(
    race_id: str, service: SimulatorService = Depends(get_simulator_service)
) -> RaceState:
    try:
        state = await service.get_state(race_id)
    except UnknownRaceError:
        raise _not_found(race_id)
    if state is None:
        state = await service.ensure_initialized()
    return state


@router.websocket("/v1/races/{race_id}/ws")
async def race_websocket(websocket: WebSocket, race_id: str, token: str | None = None) -> None:
    """Spec v1.1 repair 3: pushes RACE_STATE/RACE_RESET/STRATEGY_UPDATE/
    WATCH_ALERT/AGENT_RESPONSE events for `race_id` as they happen — see
    app/services/event_broadcaster.py and app/main.py's lifespan wiring
    for where each event type is produced. This route itself has no
    application logic beyond registering/deregistering the connection: it
    accepts, sends one immediate RACE_STATE snapshot (so a client that
    connects mid-race doesn't have to wait for the next natural state
    change to see anything), then simply waits — this is a push-only
    channel, so `receive_text()` exists only to detect the client closing
    the connection (any inbound text is ignored, not a command channel).

    A client MUST still poll GET .../state every 2s as a fallback ("When
    WebSocket is unavailable, poll race state every two seconds" — this
    route does not replace that; see fire-tv/src/hooks/useRaceState.ts).

    `service`/`broadcaster` are read directly from `websocket.app.state`
    rather than via `Depends(get_simulator_service)` /
    `Depends(get_event_broadcaster)`: those dependency functions are
    typed to take a `fastapi.Request`, which FastAPI cannot supply to a
    WebSocket-scoped dependency (a WebSocket connection is a different
    ASGI scope, not a Request) — attempting to `Depends()` an HTTP-typed
    dependency here raises at connection time, not at import time, so
    this is deliberate, not an oversight.

    AWS hackathon-final pass, P2-12: authentication. Before this pass,
    this route had NO credential check at all, while every HTTP route
    that reads the same race/strategy/watch data requires
    `X-Demo-Access-Token` — meaning race state, strategy alerts, and
    explanations were reachable by anyone who could open a WebSocket to
    this URL, master-token-less and device-pairing-less, completely
    bypassing the auth model the rest of the API enforces. A browser's
    native `WebSocket` cannot set a custom header on the handshake
    request, so the credential travels as a `?token=` query parameter
    instead — the standard workaround for this well-known WebSocket API
    limitation (see fire-tv/src/hooks/useRaceSocket.ts, which now
    appends the same token `api/client.ts` already attaches to every
    HTTP request as `X-Demo-Access-Token`). `credential_is_valid` (see
    app/api/deps.py) is the SAME check `require_demo_access_token` uses
    for the HTTP routes, so a master token or any live paired-device
    token both work here exactly as they do everywhere else. An invalid
    or missing token is rejected with WebSocket close code 4401 BEFORE
    `broadcaster.connect` ever calls `websocket.accept()` — the
    connection is refused at the handshake, not accepted and then
    dropped, and no race/strategy data is ever sent.
    """
    if not credential_is_valid(websocket.app.state, token):
        settings: Settings = get_settings()
        client_host = get_client_ip(websocket, settings)  # type: ignore[arg-type]
        audit_log(
            "authentication_failed",
            path=websocket.url.path,
            client_host=client_host,
            credential_presented=token is not None,
            transport="websocket",
        )
        await websocket.close(code=4401)
        return

    service: SimulatorService = websocket.app.state.simulator_service
    broadcaster: RaceEventBroadcaster = websocket.app.state.event_broadcaster
    await broadcaster.connect(race_id, websocket)
    try:
        try:
            initial_state = await service.get_state(race_id)
        except UnknownRaceError:
            initial_state = None
        if initial_state is not None:
            # AWS hackathon-final pass, P0-7: `send_direct` (not a bare
            # `websocket.send_json`) shares its per-connection lock with
            # `deliver_local`'s own broadcast send loop -- see
            # RaceEventBroadcaster's docstring for why an unsynchronized
            # send here could previously interleave with, or be raced
            # by, a concurrent live broadcast to this same connection.
            await broadcaster.send_direct(
                race_id,
                websocket,
                RaceEvent(
                    type=RaceEventType.RACE_STATE, race_id=initial_state.race_id, run_id=initial_state.run_id,
                    state_version=initial_state.state_version, generated_at=initial_state.generated_at,
                    payload=initial_state.model_dump(mode="json"),
                ),
            )
        while True:
            await websocket.receive_text()  # ignored — see docstring; raises WebSocketDisconnect on close
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(race_id, websocket)


@router.get(
    "/v1/auth/verify",
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    tags=["auth"],
    dependencies=[Depends(require_demo_access_token)],
)
async def verify_auth_token() -> dict:
    """Spec v1.1 correction 5: the server-side half of the Fire TV
    release-build device-pairing flow. Deliberately non-mutating (no
    Simulator/repository call at all) so a Fire TV client can validate a
    token a person just entered on the pairing screen — BEFORE persisting
    it to Android-Keystore-backed secure storage — without side effects
    on race state, and can safely re-check an already-stored token's
    continued validity (e.g. after the app resumes from background)
    without accidentally starting/pausing/resetting the demo as a
    byproduct of merely checking whether the credential still works.

    Reuses the exact same `require_demo_access_token` dependency every
    other mutating endpoint already enforces — there is no separate,
    parallel auth mechanism to keep in sync: a token this endpoint
    accepts is, by construction, a token every mutating endpoint accepts,
    and a token it rejects (401 invalid, 503 auth_not_configured on a
    backend with no token configured at all) is rejected everywhere else
    identically.
    """
    return {"valid": True}


class PairingCodeResponse(BaseModel):
    code: str
    expires_in_seconds: int


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    device_name: str = Field(default="Fire TV", max_length=100)


class PairResponse(BaseModel):
    device_id: str
    device_token: str
    expires_in_seconds: int


class RevokeRequest(BaseModel):
    device_id: Optional[str] = None


def _pairing_rate_limiter(request: Request) -> PairingRateLimiter:  # noqa: ANN001
    limiter: PairingRateLimiter | None = getattr(request.app.state, "pairing_rate_limiter", None)
    if limiter is None:
        raise RuntimeError("Pairing rate limiter was not initialized on application startup")
    return limiter


@router.post(
    "/v1/auth/pairing-codes",
    response_model=PairingCodeResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    tags=["auth"],
    dependencies=[Depends(require_master_demo_access_token)],
)
async def create_pairing_code(
    request: Request,
    pairing_service: DevicePairingService = Depends(get_device_pairing_service),
) -> PairingCodeResponse:
    """Spec v1.1 pass 3, correction 5, step 1: an authorized administrator
    (anyone who already holds the master demo-access token) requests a
    short-lived, one-time, 6-digit pairing code to read aloud/type into a
    Fire TV's pairing screen. The Fire TV itself never calls this endpoint
    and never sees the master token used to call it.

    AWS hackathon-final pass, P0-1: this now uses
    `require_master_demo_access_token`, not the general
    `require_demo_access_token` — a device token (which the general
    dependency accepts, since most mutating endpoints ARE things a paired
    Fire TV does on its own) must NOT be able to mint pairing codes for
    OTHER devices. See that dependency's own docstring
    (`app/api/deps.py`) for the full 401-vs-403 reasoning.

    AWS hackathon-final pass, P1-11: `generate_pairing_code` retries
    internally on a collision with another still-active code (see
    `PairingCodeRepository.create_code`'s docstring) and only raises
    `PairingCodeGenerationExhaustedError` in the vanishingly unlikely
    case every retry attempt also collided — surfaced here as 503
    (a transient capacity condition, not a caller error) rather than an
    unhandled 500."""
    try:
        issued = pairing_service.generate_pairing_code()
    except PairingCodeGenerationExhaustedError as exc:
        audit_log(
            "pairing_code_generation_exhausted",
            correlation_id=getattr(request.state, "correlation_id", None),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "pairing_code_generation_exhausted", "message": str(exc)},
        ) from exc
    from time import time as _time

    expires_in_seconds = max(0, int(issued.expires_at - _time()))
    # Spec v1.1 pass 4, mandatory correction 7: audit pairing-code
    # generation — never the code itself (see audit_log's own
    # _assert_fields_are_safe guard, which would refuse a `code` field
    # outright), only the fact that one was issued and for how long.
    audit_log(
        "pairing_code_generated",
        correlation_id=getattr(request.state, "correlation_id", None),
        expires_in_seconds=expires_in_seconds,
    )
    return PairingCodeResponse(code=issued.code, expires_in_seconds=expires_in_seconds)


@router.post(
    "/v1/auth/pair",
    response_model=PairResponse,
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    tags=["auth"],
)
async def pair_device(
    request: Request,
    body: PairRequest,
    pairing_service: DevicePairingService = Depends(get_device_pairing_service),
    limiter: PairingRateLimiter = Depends(_pairing_rate_limiter),
    settings: Settings = Depends(get_settings),
) -> PairResponse:
    """Spec v1.1 pass 3, correction 5, step 2: the Fire TV's own half of
    the pairing exchange. Takes the SHORT pairing code a person just
    entered on the pairing screen — never the master token — and, if it
    is valid, unexpired, and not already used, returns a brand-new
    device-specific token this Fire TV stores in Android-Keystore-backed
    secure storage (see fire-tv/src/config/secureTokenStore.ts) and sends
    on every subsequent mutating request instead of ever holding the
    master secret. Deliberately NOT protected by
    `require_demo_access_token` — the whole point is that a Fire TV with
    no credential yet can reach this endpoint to obtain one.

    Rate-limited by client IP (spec v1.1 pass 3 correction 5: "pairing
    codes must be rate-limited") to slow down brute-forcing a 6-digit
    code, on top of the code's own 5-minute expiry and one-time use.

    AWS hackathon-final pass, P1-10: this is the exact scenario the
    requirement names -- a pairing brute-force attacker behind the ALB
    would otherwise share the SAME rate-limit bucket as every other
    device attempting to pair, since `request.client.host` is always the
    ALB's own peer address. `get_client_ip` resolves the real originating
    client instead (see app/services/client_ip.py).
    """
    client_host = get_client_ip(request, settings)
    correlation_id = getattr(request.state, "correlation_id", None)
    try:
        limiter.check_and_record(client_host)
    except _PAIRING_RATE_LIMIT_EXCEPTIONS as exc:
        audit_log(
            "device_pairing_rate_limited",
            correlation_id=correlation_id,
            client_host=client_host,
        )
        emit_pairing_outcome(outcome="rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "message": str(exc)},
        ) from exc

    try:
        paired = pairing_service.exchange_pairing_code(body.code, body.device_name)
    except PairingCodeGenerationExhaustedError as exc:
        # AWS hackathon-final pass, phase 3: DEVICE_EXCHANGE_MAX_ATTEMPTS
        # exhausted on repeated device_id/token_hash collisions — see that
        # constant's own comment for why this is expected to be
        # vanishingly rare, not a caller error; surfaced as 503 exactly
        # like generate_pairing_code's own exhaustion case above.
        audit_log(
            "device_pairing_exchange_exhausted",
            correlation_id=correlation_id,
            client_host=client_host,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "device_pairing_exchange_exhausted", "message": str(exc)},
        ) from exc
    if paired is None:
        audit_log(
            "device_pairing_failed",
            correlation_id=correlation_id,
            client_host=client_host,
            reason="invalid_or_expired_or_used_code",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_pairing_code",
                "message": "This pairing code is invalid, expired, or has already been used. Generate a new one.",
            },
        )
    from time import time as _time

    audit_log(
        "device_pairing_succeeded",
        correlation_id=correlation_id,
        client_host=client_host,
        device_id=paired.device_id,
    )
    return PairResponse(
        device_id=paired.device_id,
        device_token=paired.device_token,
        expires_in_seconds=max(0, int(paired.expires_at - _time())),
    )


@router.post(
    "/v1/auth/revoke",
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    tags=["auth"],
)
async def revoke_device(
    request: Request,
    body: RevokeRequest,
    x_demo_access_token: str | None = Header(default=None),
    pairing_service: DevicePairingService = Depends(get_device_pairing_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Spec v1.1 pass 3, correction 5: revokes a device token, in one of
    two ways:

      1. SELF-revoke ("unpair"): the caller sends its OWN device token in
         `X-Demo-Access-Token` and no `device_id` — the Fire TV's own
         "Unpair this device" action uses this, and never needs (or
         holds) the master secret to do it.
      2. ADMIN-revoke: the caller sends the master token plus an explicit
         `device_id` — for revoking a lost/compromised Fire TV remotely,
         independent of every other device's token (spec v1.1 pass 3
         correction 5's "independent revocation" requirement).

    A revoked device token is immediately rejected by every mutating
    endpoint and by `/v1/auth/verify` (both go through
    `DevicePairingService.verify_device_token`) — the paired Fire TV
    itself detects this via a 401 on its next request and returns to the
    pairing screen (see fire-tv/src/hooks/useDeviceAuth.ts)."""
    configured_token: str | None = getattr(request.app.state, "effective_demo_access_token", None)
    correlation_id = getattr(request.state, "correlation_id", None)
    client_host = get_client_ip(request, settings)  # P1-10: trusted-proxy-aware, not the ALB's own peer address

    if body.device_id is not None:
        # Admin path — requires the master token specifically (a device
        # token must not be usable to revoke a DIFFERENT device).
        # AWS hackathon-final pass, P0-1: a syntactically valid DEVICE
        # token presented here is now denied with 403 (known credential,
        # not authorized for this admin operation), not lumped in with
        # 401 (no/garbage credential) — same distinction
        # `require_master_demo_access_token` makes for the pairing-code
        # endpoint, applied inline here since this route's dual-path
        # shape doesn't fit a single `dependencies=[Depends(...)]`.
        import hmac

        if configured_token is not None and x_demo_access_token is not None and hmac.compare_digest(
            x_demo_access_token, configured_token
        ):
            pass  # master token confirmed — proceed to the admin action below.
        elif x_demo_access_token is not None and pairing_service.verify_device_token(x_demo_access_token):
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
                    "message": "A device token is not authorized to revoke a different device_id.",
                },
            )
        else:
            audit_log(
                "authentication_failed",
                correlation_id=correlation_id,
                path=request.url.path,
                client_host=client_host,
                credential_presented=x_demo_access_token is not None,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "message": "Revoking a specific device_id requires the master token."},
            )
        revoked = pairing_service.revoke_device_by_id(body.device_id)
        audit_log(
            "device_revoked",
            correlation_id=correlation_id,
            device_id=body.device_id,
            revocation_path="admin",
            revoked=revoked,
        )
        return {"revoked": revoked}

    # Self-revoke path — the presented token must be a valid device token.
    if x_demo_access_token is None or not pairing_service.verify_device_token(x_demo_access_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Missing or invalid device token."},
        )
    revoked = pairing_service.revoke_device_token(x_demo_access_token)
    # Self-revoke deliberately does not log the device_id here: at this
    # point in the flow the only identifier available is the presented
    # token itself (secret-shaped -- audit_log's own guard would refuse
    # it), and looking the device_id up before revoking would trade a
    # simple revoke for an extra internal lookup with no real audit value
    # over "self-revoke occurred, succeeded/failed."
    audit_log(
        "device_revoked",
        correlation_id=correlation_id,
        revocation_path="self",
        revoked=revoked,
    )
    return {"revoked": revoked}


@router.post(
    "/v1/demo/start",
    response_model=DemoStatusResponse,
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
    tags=["demo"],
    dependencies=[Depends(require_demo_access_token)],
)
async def start_demo(service: SimulatorService = Depends(get_simulator_service)) -> DemoStatusResponse:
    control = await service.start(service.race_id)
    return _to_status_response(control)


@router.post(
    "/v1/demo/pause",
    response_model=DemoStatusResponse,
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
    tags=["demo"],
    dependencies=[Depends(require_demo_access_token)],
)
async def pause_demo(service: SimulatorService = Depends(get_simulator_service)) -> DemoStatusResponse:
    try:
        control = await service.pause(service.race_id)
    except UnknownRaceError:
        raise _not_found(service.race_id)
    return _to_status_response(control)


@router.post(
    "/v1/demo/reset",
    response_model=RaceState,
    responses={401: {"model": ErrorResponse}},
    tags=["demo"],
    dependencies=[Depends(require_demo_access_token)],
)
async def reset_demo(service: SimulatorService = Depends(get_simulator_service)) -> RaceState:
    return await service.reset(service.race_id)


@router.get(
    "/v1/demo/status",
    response_model=DemoStatusResponse,
    responses={404: {"model": ErrorResponse}},
    tags=["demo"],
)
async def demo_status(service: SimulatorService = Depends(get_simulator_service)) -> DemoStatusResponse:
    control = await service.get_control(service.race_id)
    if control is None:
        control = DemoControlState(
            race_id=service.race_id,
            run_id="",
            status=SimulatorStatus.STOPPED,
            scenario_id="",
            current_lap=0,
            state_version=0,
            updated_at=utc_now(),
        )
    return _to_status_response(control)


def _strategy_engine_unavailable(exc: Exception) -> HTTPException:
    """Spec v1.1 repair 14 / Section 29 "Strategy engine failure": any
    exception from the deterministic strategy engine OTHER than a
    validation error (the caller's fault, handled separately with 422)
    must fail explicitly with this exact literal display text rather than
    leaking a raw traceback as a generic 500 — and, per the spec, the
    system must not substitute LLM reasoning to paper over the failure."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "strategy_engine_unavailable", "message": "STRATEGY MODEL UNAVAILABLE"},
    )


@router.post(
    "/v1/races/{race_id}/strategy/undercut",
    response_model=UndercutResult,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    tags=["strategy"],
)
async def calculate_undercut(
    race_id: str,
    body: UndercutRequest,
    service: SimulatorService = Depends(get_simulator_service),
    engine: StrategyEngine = Depends(get_strategy_engine),
    broadcaster: RaceEventBroadcaster = Depends(get_event_broadcaster),
) -> UndercutResult:
    race_state = await _get_race_state_or_404(race_id, service)
    try:
        result = engine.calculate_undercut(race_state, body.candidate_driver, body.target_driver)
    except StrategyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "strategy_validation_error", "message": str(exc)},
        )
    except Exception as exc:
        raise _strategy_engine_unavailable(exc) from exc
    # Spec v1.1 repair 3: an on-demand calculation (triggered via REST,
    # MCP, or a voice request) is pushed to every other Fire TV client
    # watching the same race, not just returned to the caller — so a
    # shared-viewing screen reflects a calculation someone else just
    # triggered without waiting for its own next poll.
    await broadcaster.broadcast(
        race_id,
        RaceEvent(
            type=RaceEventType.STRATEGY_UPDATE, race_id=race_id, run_id=race_state.run_id,
            state_version=race_state.state_version, generated_at=utc_now(),
            payload={"kind": "undercut", "result": result.model_dump(mode="json")},
        ),
    )
    return result


@router.post(
    "/v1/races/{race_id}/strategy/simulate",
    response_model=StrategySimulationResult,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    tags=["strategy"],
)
async def simulate_strategy(
    race_id: str,
    body: SimulateStrategyRequest,
    service: SimulatorService = Depends(get_simulator_service),
    engine: StrategyEngine = Depends(get_strategy_engine),
    broadcaster: RaceEventBroadcaster = Depends(get_event_broadcaster),
) -> StrategySimulationResult:
    race_state = await _get_race_state_or_404(race_id, service)
    try:
        result = engine.simulate_strategy(
            race_state, body.driver_id, wait_laps=body.wait_laps, compound_override=body.compound
        )
    except StrategyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "strategy_validation_error", "message": str(exc)},
        )
    except Exception as exc:
        raise _strategy_engine_unavailable(exc) from exc
    await broadcaster.broadcast(
        race_id,
        RaceEvent(
            type=RaceEventType.STRATEGY_UPDATE, race_id=race_id, run_id=race_state.run_id,
            state_version=race_state.state_version, generated_at=utc_now(),
            payload={"kind": "simulate", "result": result.model_dump(mode="json")},
        ),
    )
    return result


@router.post(
    "/v1/races/{race_id}/watches",
    response_model=Watch,
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    tags=["watches"],
    dependencies=[Depends(require_demo_access_token)],
)
async def create_strategy_watch(
    race_id: str,
    body: CreateWatchRequest,
    service: SimulatorService = Depends(get_simulator_service),
    evaluator: WatchEvaluator = Depends(get_watch_evaluator),
) -> Watch:
    """create_strategy_watch (spec 22): establishes a persistent watch and
    returns its watch ID and current status, computed from the live race
    state (spec: "Returns watch ID and current state.")."""
    race_state = await _get_race_state_or_404(race_id, service)
    try:
        return await evaluator.create_watch(race_state, body.candidate_driver, body.target_driver, body.watch_type)
    except StrategyValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "strategy_validation_error", "message": str(exc)},
        )
    except Exception as exc:
        raise _strategy_engine_unavailable(exc) from exc


@router.get(
    "/v1/races/{race_id}/watches",
    response_model=list[Watch],
    tags=["watches"],
)
async def list_strategy_watches(
    race_id: str,
    active_only: bool = True,
    service: SimulatorService = Depends(get_simulator_service),
    watch_repository: WatchRepository = Depends(get_watch_repository),
) -> list[Watch]:
    """Lists watches for a race. By default only watches belonging to the
    CURRENT run are returned (active_only=true) — a watch from a previous
    run was invalidated by reset (spec 48) and is excluded, though it is
    not deleted from storage. Pass active_only=false to see full history."""
    if active_only:
        state = await _get_race_state_or_404(race_id, service)
        watches = await watch_repository.list_active_watches(race_id, state.run_id)
    else:
        watches = await watch_repository.list_watches(race_id)
    return list(watches)


@router.get(
    "/v1/races/{race_id}/watches/{watch_id}",
    response_model=Watch,
    responses={404: {"model": ErrorResponse}},
    tags=["watches"],
)
async def get_strategy_watch(
    race_id: str,
    watch_id: str,
    watch_repository: WatchRepository = Depends(get_watch_repository),
) -> Watch:
    watch = await watch_repository.get_watch(race_id, watch_id)
    if watch is None:
        raise _watch_not_found(race_id, watch_id)
    return watch


@router.delete(
    "/v1/races/{race_id}/watches/{watch_id}",
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
    tags=["watches"],
    dependencies=[Depends(require_demo_access_token)],
)
async def delete_strategy_watch(
    race_id: str,
    watch_id: str,
    watch_repository: WatchRepository = Depends(get_watch_repository),
) -> dict:
    deleted = await watch_repository.delete_watch(race_id, watch_id)
    if not deleted:
        raise _watch_not_found(race_id, watch_id)
    return {"deleted": True, "watch_id": watch_id}


@router.get(
    "/v1/races/{race_id}/alerts",
    response_model=list[Alert],
    tags=["watches"],
)
async def list_alerts(
    race_id: str,
    watch_id: Optional[str] = None,
    alert_repository: AlertRepository = Depends(get_alert_repository),
) -> list[Alert]:
    """Lists alerts for a race (newest first), optionally filtered to one
    watch. Fire TV polls this to surface proactive alerts — see the
    README's Gate 2 scope note on why this is polling, not a WebSocket
    push, for now."""
    alerts = await alert_repository.list_alerts(race_id, watch_id=watch_id)
    return list(alerts)


@router.get(
    "/v1/races/{race_id}/alerts/{alert_id}",
    response_model=Alert,
    responses={404: {"model": ErrorResponse}},
    tags=["watches"],
)
async def get_alert(
    race_id: str,
    alert_id: str,
    alert_repository: AlertRepository = Depends(get_alert_repository),
) -> Alert:
    """'Why now?' (spec 49): returns the ALERT'S STORED TRIGGERING SNAPSHOT,
    never the live current state — that snapshot is immutable once
    written. A client that also wants the current state should call
    GET /v1/races/{race_id}/state separately and show it as CURRENT STATE,
    never substituting it for this alert's evidence."""
    alert = await alert_repository.get_alert(race_id, alert_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "alert_not_found", "message": f"Unknown alert_id: {alert_id}"},
        )
    return alert


@router.get(
    "/v1/races/{race_id}/alerts/{alert_id}/explain",
    response_model=ExplanationResult,
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    tags=["explanation"],
    dependencies=[Depends(enforce_explain_rate_limit)],
)
async def explain_alert(
    race_id: str,
    alert_id: str,
    alert_repository: AlertRepository = Depends(get_alert_repository),
    explanation_engine: ExplanationEngine = Depends(get_explanation_engine),
) -> ExplanationResult:
    """Gate 3: narrates one alert's deterministic result in natural
    language, grounded entirely in that alert's own immutable
    triggering_snapshot / deterministic_result (spec sections 19, 50).
    The LLM never computes a number here — see explanation_engine.py.
    Per-alert results are cached (explanation_engine.py) and this route is
    rate-limited per client IP (deps.enforce_explain_rate_limit) — both
    fix previously-documented Gate 3 limitations."""
    alert = await alert_repository.get_alert(race_id, alert_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "alert_not_found", "message": f"Unknown alert_id: {alert_id}"},
        )
    # AWS hackathon-final pass, P2-12: `explain_alert` is a plain
    # SYNCHRONOUS method, and with LLM_PROVIDER=bedrock it makes a real,
    # blocking boto3 `invoke_model` network call under the hood (see
    # llm_client.py's BedrockLLMClient.explain). Calling it directly from
    # this `async def` route ran that blocking call ON the asyncio event
    # loop thread itself -- for the whole duration of a real Bedrock
    # round trip (bounded, but still real network latency, and
    # previously unbounded by any explicit timeout -- see
    # BedrockLLMClient's own Config below), every OTHER concurrent
    # request this same uvicorn worker was serving (every other
    # in-flight HTTP request, every open WebSocket's message pump) was
    # frozen too. `run_in_threadpool` moves the call onto Starlette's own
    # worker thread pool, exactly like FastAPI already does automatically
    # for a plain (non-`async def`) route handler -- this is that same
    # well-established pattern applied explicitly here, since this
    # particular route can't just BE a sync `def` (it needs `await` for
    # the alert lookup above).
    return await run_in_threadpool(explanation_engine.explain_alert, alert)


@router.get(
    "/v1/regulations",
    response_model=RegulationAnswer,
    responses={422: {"model": ErrorResponse}},
    tags=["regulations"],
)
async def get_regulation(
    query: str,
    regulation_service: RegulationService = Depends(get_regulation_service),
) -> RegulationAnswer:
    """Spec v1.1 repair 8: the REST mirror of the 5th MCP domain tool,
    `get_regulation`. Answers a direct factual question about this demo's
    fictional sporting rules with either `found=True` and one or more
    structured `evidence` items that concretely support the query, or
    `found=False` and an empty `evidence` list — the service never
    fabricates a rule it wasn't given (see regulation_service.py). This is
    the backing data source for the Fire TV Evidence/Sources screen."""
    if not query or not query.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": "query must not be empty"},
        )
    return regulation_service.get_regulation(query)


@router.post(
    "/v1/agent/query",
    response_model=AgentQueryResult,
    responses={404: {"model": ErrorResponse}},
    tags=["agent"],
)
async def agent_query(
    body: AgentQueryRequest,
    service: SimulatorService = Depends(get_simulator_service),
    agent_query_service: AgentQueryService = Depends(get_agent_query_service),
    broadcaster: RaceEventBroadcaster = Depends(get_event_broadcaster),
) -> AgentQueryResult:
    """Spec v1.1 repair 9: answers a free-text question grounded in the
    CURRENT race state and/or RegulationService, returning structured
    `evidence` independent of the narrative prose (see agent_service.py).
    Read-only: this is a query, not a demo-control mutation, so it does
    not require X-Demo-Access-Token. Also pushed as an AGENT_RESPONSE
    WebSocket event (spec v1.1 repair 3) so a Fire TV screen shows an
    Alexa+-triggered answer live, without polling for it."""
    try:
        race_state = await service.get_state(body.race_id)
    except UnknownRaceError:
        raise _not_found(body.race_id)
    if race_state is None:
        race_state = await service.ensure_initialized()
    result = agent_query_service.query(race_state, body.query)
    await broadcaster.broadcast(
        body.race_id,
        RaceEvent(
            type=RaceEventType.AGENT_RESPONSE, race_id=body.race_id, run_id=race_state.run_id,
            state_version=race_state.state_version, generated_at=result.created_at,
            payload=result.model_dump(mode="json"),
        ),
    )
    return result


@router.get(
    "/v1/races/{race_id}/changes",
    response_model=RaceChangesResult,
    responses={404: {"model": ErrorResponse}},
    tags=["agent"],
)
async def get_race_changes(
    race_id: str,
    service: SimulatorService = Depends(get_simulator_service),
    tracker: RaceChangeTracker = Depends(get_race_change_tracker),
) -> RaceChangesResult:
    """Spec v1.1 repair 9's "What Changed?" endpoint. Diffs the two most
    recently committed states for `race_id` — structured deltas computed
    FIRST, ranked by significance, with only the top 3 explained in the
    narrative (see agent_service.py's module docstring for why this order
    matters: "Do not give two complete states to the LLM and ask it to
    find changes")."""
    try:
        current = await service.get_state(race_id)
    except UnknownRaceError:
        raise _not_found(race_id)
    if current is None:
        current = await service.ensure_initialized()
    previous, tracked_current = tracker.get_previous_and_current(race_id)
    # Prefer the tracker's own view of "current" when available (it only
    # advances on real, non-duplicate commits — see RaceChangeTracker.record)
    # so a change computed here always matches a real committed transition.
    effective_current = tracked_current if tracked_current is not None else current
    return build_race_changes_result(race_id, previous, effective_current)


def _watch_not_found(race_id: str, watch_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "watch_not_found", "message": f"Unknown watch_id: {watch_id} for race {race_id}"},
    )


async def _get_race_state_or_404(race_id: str, service: SimulatorService) -> RaceState:
    try:
        state = await service.get_state(race_id)
    except UnknownRaceError:
        raise _not_found(race_id)
    if state is None:
        raise _not_found(race_id)
    return state


def _to_status_response(control: DemoControlState) -> DemoStatusResponse:
    return DemoStatusResponse(
        status=control.status,
        race_id=control.race_id,
        run_id=control.run_id,
        current_lap=control.current_lap,
        state_version=control.state_version,
    )


def _not_found(race_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "race_not_found", "message": f"Unknown race_id: {race_id}"},
    )
