"""Application configuration, sourced from environment variables.

Nothing here contains a real secret. See ../.env.example for the documented
variable list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_SCENARIO_PATH = REPO_ROOT / "scenarios" / "scenario-demo-01.json"
DEFAULT_KNOWLEDGE_BASE_DIR = REPO_ROOT / "docs" / "sporting-rules"
# Spec v1.1 repair 8: a separate, finer-grained corpus for
# RegulationService's strict found/evidence contract — see
# app/services/regulation_service.py's module docstring for why this is
# deliberately not the same corpus/service as the Gate 3 knowledge base.
DEFAULT_REGULATIONS_DIR = REPO_ROOT / "docs" / "sporting-rules" / "regulations"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Pit Wall Copilot API"
    app_version: str = "0.1.0-gate0"

    # ENVIRONMENT: "development" (default) or "production". Only
    # "production" triggers `app.observability.validate_production_configuration`'s
    # fail-closed startup checks (spec v1.1 pass 4, mandatory correction 7)
    # -- a real AWS deployment (see infrastructure/template.yaml) sets this
    # explicitly via an ECS task-definition environment variable. Local
    # development, CI unit/integration test runs, and moto-mocked tests are
    # never affected: they simply never set ENVIRONMENT=production.
    environment: str = "development"

    # STATE_REPOSITORY: "memory" (default, no AWS required) or "dynamodb"
    state_repository: str = "memory"

    dynamodb_table_name: str = "pit-wall-race-state"
    aws_region: str = "us-east-1"
    dynamodb_endpoint_url: str | None = None  # set for local DynamoDB Local, e.g. http://localhost:8000

    # PAIRING_REPOSITORY: durable storage for device-pairing codes/tokens
    # (spec v1.1 pass 4, mandatory correction 1: "a real DynamoDB-backed
    # pairing repository ... the AWS deployment must use DynamoDB, not
    # memory"). Left unset (None) by default, in which case it MIRRORS
    # `state_repository` -- one deployment decision ("are we durable/
    # multi-instance or not") governs both, exactly like
    # `build_watch_repositories` already does for watches/alerts. Set this
    # explicitly only if pairing storage genuinely needs to diverge from
    # race-state storage (e.g. a migration window) -- see
    # `effective_pairing_repository` below and `app/repositories/factory.py`.
    pairing_repository: str | None = None

    @property
    def effective_pairing_repository(self) -> str:
        return self.pairing_repository or self.state_repository

    # EVENT_BROKER: "memory" (default) or "redis". AWS hackathon-final
    # pass, P0-3. `RaceEventBroadcaster` (app/services/event_broadcaster.py)
    # only ever knew about WebSocket clients connected to its OWN process,
    # so with `BackendService.DesiredCount` raised above 1 (the very thing
    # correction 8's own scaling story calls for) an event committed by
    # the simulator/strategy/watch code on one ECS task would simply never
    # reach a client connected to a different task -- no error, just
    # silent, permanent non-delivery for roughly half of connected
    # clients. "memory" keeps today's exact behavior for local dev/unit
    # tests (single process, no external dependency). "redis" fans events
    # out through a shared Redis/Valkey pub/sub channel (Amazon
    # ElastiCache Serverless in the deployed stack -- see
    # infrastructure/template.yaml's ElastiCacheServerlessCache and
    # docs/adr/0001-cross-instance-event-broker.md) so every task
    # subscribes to the same channel and can deliver events regardless of
    # which task actually produced them. Mirrors the state_repository /
    # pairing_repository pattern above: one explicit, fail-visible
    # deployment decision, never inferred from other settings.
    event_broker: str = "memory"
    redis_url: str | None = None  # e.g. rediss://<elasticache-endpoint>:6379

    # AWS hackathon-final pass, P1-10: comma-separated CIDR list of
    # trusted proxy addresses (the deployed stack sets this to the VPC
    # CIDR the ALB's own ENIs live in). Empty by default -- X-Forwarded-For
    # is never trusted unless this is explicitly configured, so local
    # dev/tests are unaffected. See app/services/client_ip.py's module
    # docstring for the full trust model this implements.
    trusted_proxy_cidrs: str = ""

    scenario_path: str = str(DEFAULT_SCENARIO_PATH)

    # Comma-separated list of allowed CORS origins for local Fire TV / React
    # Native development. Deliberately does NOT default to "*".
    cors_allowed_origins: str = "http://localhost:8081,http://10.0.2.2:8081,http://localhost:19006"

    # Simple shared-secret token required for mutating endpoints (demo
    # start/pause/reset). Not a production auth system — sufficient to
    # satisfy "mutation operations SHALL require an authorized demo client
    # or deployment-specific access token" for a hackathon demo (spec 52).
    #
    # Spec v1.1 repair 11: "No production credential may be stored in ...
    # source code, .env.example, tests or documentation." There is
    # therefore deliberately NO default value here anymore (it used to be
    # the literal string "local-dev-demo-token", committed to source,
    # which is exactly what repair 11 forbids). Leaving this unset means:
    #   - a real deployment MUST supply its own token via a real secret
    #     store (Secrets Manager / deployment parameter — see
    #     infrastructure/), never a value baked into this file, and
    #   - local development has NO token at all unless `local_demo_auth_mode`
    #     (below) is explicitly turned on, in which case one is generated
    #     fresh at process startup rather than hardcoded — see
    #     `app/main.py`'s lifespan and `api/deps.py`'s
    #     `require_demo_access_token`.
    demo_access_token: Optional[str] = None

    # Explicit, opt-in local-development auth mode (spec v1.1 repair 11:
    # "Keep local development possible through an explicitly enabled
    # local-demo authentication mode."). Defaults to False — i.e. by
    # default, with no token configured, mutating endpoints refuse ALL
    # requests (fail closed), matching "release builds/deployments SHALL
    # refuse mutation requests when secure authorization is not
    # configured." Set LOCAL_DEMO_AUTH_MODE=true for local development
    # only; combined with `demo_access_token` left unset, the backend then
    # generates a random token once at startup and prints it to the
    # console (never persisted to a file, never a fixed value) for a
    # developer to copy into their own local Fire TV dev config.
    local_demo_auth_mode: bool = False

    # LLM_PROVIDER: "template" (default, deterministic, no AWS required) or
    # "bedrock" (real Anthropic-on-Bedrock model via boto3; see
    # app/services/llm_client.py's module docstring for why this is
    # implemented-but-unverified against a real AWS account here).
    llm_provider: str = "template"
    bedrock_model_id: str = "anthropic.claude-3-haiku-20240307-v1:0"
    bedrock_region: str = "us-east-1"

    # AWS hackathon-final pass, phase 5: real distributed tracing (see
    # app/tracing.py's own module docstring). Off by default (False) —
    # local dev and every existing unit/integration test run with no
    # tracing exporter configured, unchanged from every prior pass;
    # infrastructure/template.yaml sets OTEL_TRACING_ENABLED=true and
    # OTEL_EXPORTER_OTLP_ENDPOINT to the ADOT collector sidecar's local
    # endpoint for every real deployment.
    otel_tracing_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318"
    otel_service_name: str = "pit-wall-backend"
    bedrock_endpoint_url: str | None = None

    # Where the Gate 3 Knowledge-Base stand-in loads its fictional sporting
    # rules documents from. Defaults to the repository's docs/sporting-rules.
    knowledge_base_dir: str | None = None

    # Where RegulationService (spec v1.1 repair 8's get_regulation) loads
    # its fact corpus from. Defaults to
    # docs/sporting-rules/regulations relative to the repository root.
    regulations_dir: str | None = None

    # AWS hackathon-final pass, P2-12: hard cap on any request body this
    # API will read, enforced by app/security_middleware.py's
    # RequestBodySizeLimitMiddleware before the body is buffered into
    # memory. Every real request body here is a small, bounded JSON
    # object (a watch definition, a pairing code, a device name) — 1 MiB
    # is generously above anything a legitimate caller sends, while still
    # bounding the worst case for an anonymous, unauthenticated POST.
    max_request_body_bytes: int = 1_000_000

    # Per-client-IP fixed-window rate limit on GET .../explain (the one
    # route that can trigger a real, paid Bedrock call per request) — see
    # app/services/rate_limiter.py. <= 0 disables rate-limiting entirely.
    explain_rate_limit_per_minute: int = 30

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
