from __future__ import annotations

import asyncio
import secrets
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from pathlib import Path

from app.api.routes import router
from app.config import DEFAULT_KNOWLEDGE_BASE_DIR, DEFAULT_REGULATIONS_DIR, Settings, get_settings
from app.domain.models import RaceEvent, RaceEventType
from app.domain.scenario import ScenarioValidationError, load_scenario
from app.observability import CorrelationIdMiddleware, configure_logging, validate_production_configuration
from app.tracing import configure_tracing, instrument_app, shutdown_tracing
from app.security_middleware import RequestBodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.repositories.factory import build_lease_repository, build_repository, build_transition_writer, build_watch_repositories
from app.services.agent_service import AgentQueryService, RaceChangeTracker
from app.repositories.factory import (
    build_pairing_exchange_coordinator,
    build_pairing_rate_limiter,
    build_pairing_repositories,
)
from app.services.device_pairing import DevicePairingService
from app.services.event_broadcaster import RaceEventBroadcaster
from app.services.event_broker import build_event_broker
from app.services.explanation_engine import ExplanationEngine
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm_client import BedrockLLMClient, LLMClient, TemplateLLMClient
from app.services.rate_limiter import FixedWindowRateLimiter
from app.services.regulation_service import RegulationService
from app.services.simulator import SimulatorService
from app.services.strategy_engine import StrategyEngine
from app.services.watch_evaluator import WatchEvaluator


def build_llm_client(settings: Settings) -> LLMClient:
    """Selects the LLM backend by LLM_PROVIDER, mirroring
    repositories/factory.py's STATE_REPOSITORY selection pattern."""
    if settings.llm_provider == "bedrock":
        return BedrockLLMClient(
            model_id=settings.bedrock_model_id,
            region_name=settings.bedrock_region,
            endpoint_url=settings.bedrock_endpoint_url,
        )
    if settings.llm_provider == "template":
        return TemplateLLMClient()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (expected 'template' or 'bedrock')")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # AWS hackathon-final pass, phase 5: configure real distributed
    # tracing before anything else — botocore/httpx instrumentation must
    # be installed before the first boto3 client (DynamoDB, Bedrock,
    # Secrets Manager) is constructed below, or that client's calls would
    # never be wrapped in a span. A no-op entirely when
    # OTEL_TRACING_ENABLED is unset (the default) — see
    # app/tracing.py's own module docstring for the full honest-status
    # note on what has/hasn't been verified against a real collector.
    configure_tracing(settings)
    # Spec v1.1 pass 4, mandatory correction 7: fail CLOSED, before
    # building a single repository or service, if ENVIRONMENT=production
    # but the rest of the configuration is not actually production-safe
    # (in-process demo auth left on, non-durable storage, no master
    # token). A no-op in development — see validate_production_configuration's
    # own docstring. Raising here means the ASGI server never finishes
    # starting up, so an ECS deployment's health check fails immediately
    # and its own deployment circuit breaker/rollback (see
    # infrastructure/template.yaml) reverts to the last known-good task
    # definition instead of serving traffic from a misconfigured one.
    validate_production_configuration(settings)
    scenario = load_scenario(settings.scenario_path)
    # Repair pass: built BEFORE `repository` (reordered from this
    # lifespan's earlier form, where the two were unrelated) so
    # `build_repository` can wire the SAME lease_repository instance in
    # — see that function's own docstring for why this is what lets
    # `RaceStateRepository.save_state_from_simulator`/
    # `save_control_from_simulator` verify a `LeaseProof` against the
    # authoritative lease record rather than a separately-maintained
    # floor.
    lease_repository = build_lease_repository(settings)
    repository = build_repository(settings, lease_repository)

    strategy_engine = StrategyEngine(config=scenario.strategy_model, model_seed=scenario.model_seed)

    # Gate 2: watch/alert persistence now follows the same STATE_REPOSITORY
    # switch as race state (memory | dynamodb) — see
    # repositories/factory.py's module docstring for why this fixes the
    # previously-documented "in-memory only" limitation.
    watch_repository, alert_repository = build_watch_repositories(settings)
    # Spec v1.1 correction 9: atomic alert+watch commit — see
    # TransitionWriter's class docstring (watch_base.py).
    transition_writer = build_transition_writer(settings, watch_repository, alert_repository)
    watch_evaluator = WatchEvaluator(
        watch_repository=watch_repository,
        alert_repository=alert_repository,
        strategy_engine=strategy_engine,
        transition_writer=transition_writer,
    )

    # Spec v1.1 repair 9: "What Changed?" needs the last two committed
    # states per race to diff, so it is wired as a second
    # on_state_committed hook alongside the watch evaluator.
    race_change_tracker = RaceChangeTracker()

    # Spec v1.1 repair 3: WebSocket push. `event_broadcaster` is shared by
    # the WS route (app/api/routes.py) and every server-side event source
    # below.
    event_broadcaster = RaceEventBroadcaster()
    # AWS hackathon-final pass, P0-3: attach the cross-instance broker
    # (memory locally/in tests, Redis/ElastiCache in a real multi-task
    # deployment -- selected by EVENT_BROKER, see config.py and
    # app/services/event_broker.py) so `broadcast` below fans out to
    # every backend task, not just this process.
    event_broker = build_event_broker(settings)
    await event_broadcaster.attach_broker(event_broker)

    async def _on_state_committed(state):
        alerts = await watch_evaluator.evaluate(state)
        await race_change_tracker.record(state)

        # A reset always re-baselines state_version to 0 (see
        # SimulatorService.reset / the run/state-version tests) — the
        # same, already-true invariant repair 4/6 relies on elsewhere is
        # reused here rather than tracking "was this a reset" separately.
        event_type = RaceEventType.RACE_RESET if state.state_version == 0 else RaceEventType.RACE_STATE
        await event_broadcaster.broadcast(
            state.race_id,
            RaceEvent(
                type=event_type, race_id=state.race_id, run_id=state.run_id,
                state_version=state.state_version, generated_at=state.generated_at,
                payload=state.model_dump(mode="json"),
            ),
        )
        for alert in alerts:
            await event_broadcaster.broadcast(
                state.race_id,
                RaceEvent(
                    type=RaceEventType.WATCH_ALERT, race_id=state.race_id, run_id=state.run_id,
                    state_version=state.state_version, generated_at=alert.created_at,
                    payload=alert.model_dump(mode="json"),
                ),
            )

    # AWS hackathon-final pass, P0-4: distributed simulator leadership
    # (mirrors STATE_REPOSITORY, same as every other durability choice —
    # see build_lease_repository's own docstring). Built above, before
    # `repository`, so both point at the same lease state — see that
    # earlier comment.
    service = SimulatorService(
        repository=repository,
        scenario=scenario,
        # Stands in for a DynamoDB-Streams-triggered Lambda (spec 18) —
        # every accepted state write synchronously invokes both hooks above.
        on_state_committed=_on_state_committed,
        lease_repository=lease_repository,
    )
    await service.ensure_initialized()

    # AWS hackathon-final pass, phase 2: every backend process runs its own
    # supervisor loop, independent of whether THIS process ever received
    # an HTTP /start call — see SimulatorService.run_supervisor's own
    # docstring for why `start()` alone left a gap (only the one task that
    # handled /start ever had a local `_run_loop`; if it crashed with no
    # new /start call landing on a different task, nothing else ever
    # noticed). Cancelled alongside the simulator loop itself on shutdown,
    # below.
    supervisor_task = asyncio.create_task(service.run_supervisor(service.race_id))

    # Gate 3: LLM-grounded explanation of alerts. The knowledge base and
    # LLM client are both selected behind an interface (see
    # knowledge_base.py / llm_client.py docstrings for why each is a
    # documented simplification rather than live Bedrock infrastructure).
    knowledge_base_dir = Path(settings.knowledge_base_dir) if settings.knowledge_base_dir else DEFAULT_KNOWLEDGE_BASE_DIR
    knowledge_base = KnowledgeBaseService(knowledge_base_dir, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)
    llm_client = build_llm_client(settings)
    explanation_engine = ExplanationEngine(knowledge_base=knowledge_base, llm_client=llm_client)

    # Fixes the other previously-documented Gate 3 limitation ("... or
    # rate-limiting on /explain") — see rate_limiter.py's module docstring.
    explain_rate_limiter = FixedWindowRateLimiter(limit=settings.explain_rate_limit_per_minute, window_seconds=60.0)

    # Spec v1.1 repair 8: get_regulation's strict found/evidence contract.
    # See app/services/regulation_service.py's module docstring for why
    # this is a separate, stricter service from the Gate 3 knowledge base
    # above, and DEFAULT_REGULATIONS_DIR's comment in config.py.
    regulations_dir = Path(settings.regulations_dir) if settings.regulations_dir else DEFAULT_REGULATIONS_DIR
    regulation_service = RegulationService(regulations_dir, repo_root=DEFAULT_KNOWLEDGE_BASE_DIR.parent.parent)
    agent_query_service = AgentQueryService(regulation_service=regulation_service)

    # Spec v1.1 repair 11: no default demo-access-token is baked into
    # source anymore (config.py). If nothing was supplied via a real env
    # var / secret store AND local_demo_auth_mode was explicitly turned
    # on, generate one fresh, random, ephemeral token for this process
    # only, and print it once — never persisted to a file, never checked
    # into anything, never the same value twice. If local_demo_auth_mode
    # is off and no token was configured, `effective_demo_access_token`
    # stays None and `require_demo_access_token` (api/deps.py) fails
    # closed on every mutating request, which is the deliberate
    # "release builds SHALL refuse mutation requests when secure
    # authorization is not configured" behavior applied to the backend
    # itself, not just the Fire TV client.
    effective_demo_access_token = settings.demo_access_token
    if effective_demo_access_token is None and settings.local_demo_auth_mode:
        effective_demo_access_token = secrets.token_urlsafe(24)
        print(  # noqa: T201 — deliberate: this is the ONLY place this token is ever surfaced
            "\n"
            "=================================================================\n"
            " LOCAL_DEMO_AUTH_MODE is on and no DEMO_ACCESS_TOKEN was set.\n"
            " Generated a fresh local-only token for this process:\n"
            f"   {effective_demo_access_token}\n"
            " Paste this into your local Fire TV dev build's local auth\n"
            " override (fire-tv/src/config/authToken.ts) to enable Start/\n"
            " Pause/Reset locally. This token is NOT persisted anywhere and\n"
            " changes every time the backend restarts.\n"
            "=================================================================\n",
            file=sys.stderr,
        )

    app.state.simulator_service = service
    app.state.strategy_engine = strategy_engine
    app.state.watch_evaluator = watch_evaluator
    app.state.watch_repository = watch_repository
    app.state.alert_repository = alert_repository
    app.state.knowledge_base = knowledge_base
    app.state.explanation_engine = explanation_engine
    app.state.explain_rate_limiter = explain_rate_limiter
    app.state.regulation_service = regulation_service
    app.state.agent_query_service = agent_query_service
    app.state.race_change_tracker = race_change_tracker
    app.state.event_broadcaster = event_broadcaster
    app.state.event_broker = event_broker
    app.state.effective_demo_access_token = effective_demo_access_token
    # Spec v1.1 pass 3, correction 5 / pass 4, mandatory correction 1: real,
    # durable device-pairing exchange — see
    # app/services/device_pairing.py's module docstring. Storage is
    # selected by `settings.effective_pairing_repository` (mirrors
    # STATE_REPOSITORY unless PAIRING_REPOSITORY overrides it): DynamoDB in
    # every real (STATE_REPOSITORY=dynamodb) deployment, so pairing survives
    # a restart and is shared across every ECS task; in-process `dict`s
    # only in STATE_REPOSITORY=memory local development.
    pairing_code_repository, device_token_repository = build_pairing_repositories(settings)
    # AWS hackathon-final pass, phase 3: the atomic pairing-exchange
    # coordinator — built from the SAME repository instances above (see
    # build_pairing_exchange_coordinator's own docstring for why that
    # matters in memory mode).
    pairing_exchange_coordinator = build_pairing_exchange_coordinator(
        settings, pairing_code_repository, device_token_repository
    )
    app.state.device_pairing_service = DevicePairingService(
        pairing_code_repository, device_token_repository, pairing_exchange_coordinator
    )
    app.state.pairing_rate_limiter = build_pairing_rate_limiter(settings, max_attempts=10, window_seconds=60.0)
    yield
    # Gate 0 has no external connections that require explicit teardown
    # beyond cancelling any running simulator loop. P0-3: the event
    # broker DOES hold an external connection when EVENT_BROKER=redis
    # (a background subscriber task + a Redis client) -- stop it
    # explicitly so shutdown doesn't leak a task or a live socket.
    await event_broker.stop()
    supervisor_task.cancel()
    try:
        await supervisor_task
    except asyncio.CancelledError:
        pass
    await service._cancel_active_loop()  # noqa: SLF001 - internal, intentional shutdown hook
    shutdown_tracing()  # AWS hackathon-final pass, phase 5: flush buffered spans


def create_app() -> FastAPI:
    # Idempotent (see configure_logging's own docstring) -- safe to call on
    # every create_app() invocation, including repeated calls from tests.
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    # AWS hackathon-final pass, phase 5: inbound-request spans. Safe to
    # call before `lifespan` has run (and therefore before
    # `configure_tracing` has installed a real TracerProvider) — this is
    # a no-op entirely when OTEL_TRACING_ENABLED is unset (the default),
    # and FastAPIInstrumentor itself only wraps request handling; it does
    # not require a TracerProvider to already exist at instrumentation
    # time, only at actual request time (by which point `lifespan` has
    # already run `configure_tracing`).
    instrument_app(app, settings)

    # Spec v1.1 pass 4, mandatory correction 7: "Add request correlation
    # IDs where practical." Added before CORS so the ID is present on the
    # response CORSMiddleware still processes afterwards (Starlette
    # middleware executes in LIFO order relative to add_middleware calls,
    # so this being added first means it is the outermost layer and runs
    # last on the way out -- exactly where a response header addition
    # belongs).
    app.add_middleware(CorrelationIdMiddleware)

    # AWS hackathon-final pass, P2-12: API/security hardening sweep.
    # Order matters here exactly as much as it did for
    # CorrelationIdMiddleware above (Starlette executes added middleware
    # in LIFO order on the way out) — SecurityHeadersMiddleware is added
    # before CORS so it is the OUTERMOST layer and its headers land on
    # every response, including one CORSMiddleware itself short-circuits
    # (e.g. a disallowed-origin preflight). RequestBodySizeLimitMiddleware
    # is added last (innermost) so it wraps the actual route handlers
    # most closely, rejecting an oversized body before any application
    # code ever sees it.
    app.add_middleware(SecurityHeadersMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.add_middleware(RequestBodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

    app.include_router(router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            body = detail
        else:
            body = {"error": "http_error", "message": str(detail)}
        # Preserve any headers the raising code attached to the HTTPException
        # (e.g. enforce_explain_rate_limit's Retry-After on 429) — the
        # previous version of this handler silently dropped them.
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "message": exc.errors()},
        )

    @app.exception_handler(ScenarioValidationError)
    async def scenario_error_handler(request: Request, exc: ScenarioValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": "scenario_invalid", "message": str(exc)},
        )

    return app


app = create_app()
