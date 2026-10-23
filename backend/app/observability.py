"""Production configuration validation, structured audit logging, and
request correlation IDs (spec v1.1 pass 4, mandatory correction 7).

Three separate, small concerns live here rather than scattered across
`main.py`/`routes.py`, so each is independently testable:

1. `validate_production_configuration(settings)` — a fail-CLOSED startup
   check. Called once, in `main.py`'s lifespan, before the app starts
   accepting traffic. A misconfigured PRODUCTION deployment (the
   in-process demo escape hatches left on, non-durable storage, no
   master token at all) raises `ProductionConfigurationError` with an
   actionable message rather than starting up in a silently-degraded or
   insecure state — in a real ECS deployment this means the task fails
   its health check and the deployment's own circuit breaker/rollback
   (see infrastructure/template.yaml's `DeploymentConfiguration`) reverts
   to the last known-good task definition instead of serving traffic from
   a broken one. `ENVIRONMENT=development` (the local default) skips
   these checks entirely — they exist to catch a production deployment
   mistake, not to get in the way of local development.

2. `audit_log(event, **fields)` — one structured (JSON-line) log record
   per security-relevant event: pairing-code generation, a successful
   pairing exchange, a failed/rate-limited pairing attempt, a device
   revocation, and an authentication failure. Every field passed here is
   asserted, by this module's own tests, to never be one of the
   raw-secret-shaped keys (`code`, `token`, `device_token`, `password`,
   `secret`, `authorization`, or anything ending `_hash`) — see
   `_FORBIDDEN_FIELD_NAMES` below. This is deliberately a static
   allowlist-adjacent guard (checked at the CALL SITE via an assertion,
   not just "please remember not to") specifically because spec v1.1
   pass 4's own instruction is "do not log raw pairing codes, tokens,
   hashes, or authorization headers" — a rule that is easy to violate by
   accident in a single hurried `logger.info(f"...{token}...")` call.

3. `CorrelationIdMiddleware` — attaches a per-request correlation ID
   (from an inbound `X-Request-ID` header if the caller/load balancer
   already set one, otherwise a freshly generated UUID4) to
   `request.state.correlation_id` and echoes it back on the response, so
   a person debugging a production incident can grep CloudWatch Logs for
   one ID and see every audit-log line and application log line for that
   one request, across this backend and (once forwarded) any downstream
   call.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.tracing import current_trace_id

_AUDIT_LOGGER_NAME = "pit_wall.audit"


class ProductionConfigurationError(RuntimeError):
    """Raised by `validate_production_configuration` when
    `ENVIRONMENT=production` but the rest of the configuration is not
    actually safe to run in production. The message is written to be
    read directly off a CloudWatch Logs entry by whoever is on call, not
    just by a developer reading this source file."""


def validate_production_configuration(settings: Settings) -> None:
    """Fail-closed startup validation. A no-op unless
    `settings.environment == "production"` — see this module's own
    docstring for why local/dev configurations are never subject to
    these checks."""
    if settings.environment != "production":
        return

    problems: list[str] = []

    if settings.local_demo_auth_mode:
        problems.append(
            "LOCAL_DEMO_AUTH_MODE=true is set, but ENVIRONMENT=production. This "
            "generates a random token and prints it to stdout at every startup "
            "instead of requiring a real, durable, Secrets-Manager-backed "
            "DEMO_ACCESS_TOKEN — turn it off (unset LOCAL_DEMO_AUTH_MODE, or set "
            "it to false) and configure DEMO_ACCESS_TOKEN via a real secret store."
        )

    if not settings.demo_access_token:
        problems.append(
            "DEMO_ACCESS_TOKEN is not set, but ENVIRONMENT=production. A "
            "production deployment must supply this from a real secret store "
            "(see infrastructure/template.yaml's DemoAccessTokenSecret) — "
            "starting with no token configured means every mutating endpoint "
            "fails closed with 503 for every real user, not just an attacker."
        )

    if settings.state_repository != "dynamodb":
        problems.append(
            f"STATE_REPOSITORY={settings.state_repository!r}, but ENVIRONMENT=production. "
            "Production must use STATE_REPOSITORY=dynamodb: durable, shared "
            "state across every ECS task. STATE_REPOSITORY=memory loses race "
            "state, watches, alerts, pairing codes, and device tokens on every "
            "restart and cannot be shared between multiple running tasks "
            "(spec v1.1 pass 4, mandatory correction 1)."
        )

    if settings.effective_pairing_repository != "dynamodb":
        problems.append(
            f"The effective pairing repository resolved to {settings.effective_pairing_repository!r}, "
            "but ENVIRONMENT=production. Set PAIRING_REPOSITORY=dynamodb explicitly "
            "(or leave it unset with STATE_REPOSITORY=dynamodb, which it mirrors by "
            "default) so paired Fire TV devices survive a restart and work "
            "across every ECS task."
        )

    # AWS hackathon-final pass, phase 4, Gap A: the app-side half of the
    # same contradictory-Bedrock-config check infrastructure/template.yaml's
    # BedrockProviderRequiresModelArn Rule enforces at deploy time.
    # `validate_production_configuration` cannot see whether the ECS task
    # role was actually granted bedrock:InvokeModel (that's an IAM fact
    # this process has no visibility into), but it CAN catch the
    # unambiguous case where the app's own configuration is internally
    # contradictory: LLM_PROVIDER=bedrock with no model id at all to
    # invoke. A real deployment that goes through the infra Rule above
    # can never actually hit this (BEDROCK_MODEL_ID has a non-empty
    # default — see config.py — and the Rule already refuses
    # LlmProvider=bedrock with BedrockModelArn blank), so this exists as
    # defense-in-depth against a hand-edited/non-template deployment
    # path, not as the primary enforcement mechanism.
    if settings.llm_provider not in ("template", "bedrock"):
        problems.append(
            f"LLM_PROVIDER={settings.llm_provider!r} is not a recognized value, but "
            "ENVIRONMENT=production. Expected 'template' (the deterministic "
            "fallback narrator) or 'bedrock' (see infrastructure/template.yaml's "
            "LlmProvider parameter)."
        )
    elif settings.llm_provider == "bedrock" and not settings.bedrock_model_id.strip():
        problems.append(
            "LLM_PROVIDER=bedrock but BEDROCK_MODEL_ID is empty, and "
            "ENVIRONMENT=production. A Bedrock model id is required to actually "
            "invoke a model — see infrastructure/template.yaml's BedrockModelId "
            "parameter (must name the same model BedrockModelArn grants IAM "
            "access to)."
        )

    if problems:
        joined = "\n  - ".join(problems)
        raise ProductionConfigurationError(
            "Refusing to start: ENVIRONMENT=production but this configuration "
            f"is not production-safe:\n  - {joined}"
        )


# Field names that must never be passed to `audit_log` — see this
# module's own docstring, item 2. Matched case-insensitively, and by
# suffix for the `_hash`/`_token`/`_code`/`_secret` families so a new
# field like `pairing_code` or `master_token` is caught even though it
# is not spelled out verbatim below.
_FORBIDDEN_FIELD_SUFFIXES = ("_token", "_code", "_secret", "_hash", "_password", "_key")
_FORBIDDEN_FIELD_NAMES = {
    "code",
    "token",
    "secret",
    "password",
    "authorization",
    "x-demo-access-token",
}


class AuditFieldViolation(ValueError):
    """Raised by `audit_log` itself when a call site passes a
    secret-shaped field name — a programming error to fix at the call
    site, not a runtime condition to catch and continue past."""


def _assert_fields_are_safe(fields: dict[str, Any]) -> None:
    for name in fields:
        lowered = name.lower()
        if lowered in _FORBIDDEN_FIELD_NAMES or lowered.endswith(_FORBIDDEN_FIELD_SUFFIXES):
            raise AuditFieldViolation(
                f"audit_log() was called with a field named {name!r}, which looks like it "
                "could hold a raw secret, token, code, or hash. Spec v1.1 pass 4, mandatory "
                "correction 7: 'do not log raw pairing codes, tokens, hashes, or authorization "
                "headers.' Pass a non-sensitive derived fact instead (e.g. device_id, "
                "outcome, or a boolean), never the raw value itself."
            )


def audit_log(event: str, *, correlation_id: str | None = None, **fields: Any) -> None:
    """Emits one structured JSON-line audit record via the standard
    `logging` module (so it is captured by whatever log driver/handler
    the deployment already has configured — CloudWatch Logs via the ECS
    `awslogs` driver in this repository's own template.yaml) at INFO
    level. `event` is a short, stable, machine-greppable name (e.g.
    `"pairing_code_generated"`, `"device_pairing_succeeded"`,
    `"device_pairing_rate_limited"`, `"device_revoked"`,
    `"authentication_failed"`); `**fields` are additional non-sensitive,
    already-validated context (device_id, client IP, reason, HTTP
    status) — see `_assert_fields_are_safe` for what is refused outright.
    """
    _assert_fields_are_safe(fields)
    record = {"audit_event": event, **({"correlation_id": correlation_id} if correlation_id else {}), **fields}
    logging.getLogger(_AUDIT_LOGGER_NAME).info(json.dumps(record, sort_keys=True, default=str))


def configure_logging() -> None:
    """Idempotent basic logging configuration: JSON-friendly single-line
    records to stdout, which the ECS `awslogs` log driver already
    forwards to CloudWatch Logs with no further agent/sidecar needed
    (see infrastructure/template.yaml's `LogConfiguration`). Safe to call
    more than once (`main.py` calls it once at import time); does not
    force a specific level on third-party loggers."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


CORRELATION_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-Id"  # AWS hackathon-final pass, phase 5

# AWS hackathon-final pass, P2-12: "correlation-ID validation." The
# original version of this middleware trusted an inbound `X-Request-ID`
# header VERBATIM -- any anonymous caller (this header is read before any
# authentication check ever runs) could set it to an arbitrarily long
# string, or one containing control characters (CR/LF, NUL, ...), and
# that value was then (a) echoed back unmodified as a literal HTTP
# response header, where a non-conforming value can trip the ASGI
# server's own header-encoding validation and turn an ordinary request
# into a 500, and (b) written into every audit_log() call on this
# request's path as the `correlation_id` field. `json.dumps` already
# makes that JSON-safe (no injection into the log record's STRUCTURE),
# but an unbounded-length or control-character-laden value is still a
# log-volume/log-readability abuse vector an anonymous caller could
# trigger for free, and it is not why X-Request-ID exists (a real
# correlation ID -- one this service's own ALB might set, or a client's
# own tracing ID -- is a short, printable token, never an arbitrary
# multi-kilobyte blob). A caller-supplied value is now accepted ONLY when
# it matches `_VALID_CORRELATION_ID_PATTERN` (printable ASCII,
# conservatively restricted to the characters real correlation/trace IDs
# actually use: alphanumerics, hyphen, underscore, period) and is no
# longer than `_MAX_CORRELATION_ID_LENGTH`; anything else is treated
# exactly like a MISSING header -- a fresh one is minted -- rather than
# being rejected with an error (this is a correlation convenience, not an
# authenticated input, so silently falling back to a safe default is the
# right failure mode, not a 400).
_VALID_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_CORRELATION_ID_LENGTH = 128


def _sanitize_correlation_id(raw: str | None) -> str:
    if raw is not None and _VALID_CORRELATION_ID_PATTERN.match(raw):
        return raw
    return str(uuid.uuid4())


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Spec v1.1 pass 4, mandatory correction 7: "Add request correlation
    IDs where practical." Reuses an inbound `X-Request-ID` (e.g. one an
    ALB or API Gateway in front of this service already set) if present
    AND well-formed (see `_sanitize_correlation_id`), otherwise mints a
    fresh one; always echoes it back on the response so a client-side log
    and this service's own logs can be correlated by the same value
    either way."""

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        correlation_id = _sanitize_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        # AWS hackathon-final pass, phase 5: echoes the active OpenTelemetry
        # trace id (when tracing is enabled — see app/tracing.py) alongside
        # the existing correlation id, so a person debugging a production
        # incident can pivot from one CloudWatch Logs line straight into
        # the matching X-Ray/ServiceLens trace. A no-op header (absent
        # entirely) when tracing is disabled or this request has no active
        # span — `current_trace_id()` never raises either way.
        trace_id = current_trace_id()
        if trace_id is not None:
            response.headers[TRACE_ID_HEADER] = trace_id
        return response
