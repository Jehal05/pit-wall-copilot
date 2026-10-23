"""Real distributed tracing (AWS hackathon-final pass, phase 5).

*** HONEST STATUS ***
infrastructure/template.yaml has deployed an X-Ray daemon sidecar since
an earlier pass, but nothing in this application ever actually created a
span or exported one to it — the daemon ran, permanently idle, and
X-Ray/CloudWatch ServiceLens showed nothing for this service. This module
is the fix: real OpenTelemetry instrumentation, exported over OTLP/HTTP to
an ADOT (AWS Distro for OpenTelemetry) collector sidecar, which re-exports
to X-Ray in AWS's own wire format. The X-Ray daemon's raw UDP protocol
(`AWS_XRAY_DAEMON_ADDRESS`, still used by the Lambda functions' built-in
X-Ray SDK integration via `Tracing: Active` — see template.yaml's
`Globals.Function`) is NOT what this module speaks; ECS's own daemon
sidecar is left in place for that Lambda-side tracing, and a SEPARATE
ADOT collector sidecar (also added by this pass — see
infrastructure/template.yaml's `OTelCollectorImage`/
`EnableOtelTracing` parameters) is what this module's OTLP/HTTP exporter
actually talks to. This has NOT been verified against a real AWS
account or a real X-Ray/CloudWatch ServiceLens console — there is no AWS
access in this development environment (the same honest-status caveat
template.yaml's own module docstring already applies to the rest of this
stack). What HAS been verified here: the OTLP exporter is correctly
configured and spans are correctly created/attributed/propagated in
-process (see tests/test_tracing.py, which captures exported spans via an
in-memory span exporter rather than a real OTLP collector).

Trace propagation path this instruments:
  Alexa+/MCP request -> MCP server -> Backend API -> DynamoDB -> Bedrock

  - FastAPI (inbound HTTP into the backend) — `FastAPIInstrumentor`.
  - httpx (the MCP server's own outbound calls to the backend, and the
    Alexa+ simulated client's calls to the MCP server) — `HTTPXClientInstrumentor`.
  - botocore (every boto3 call this backend makes — DynamoDB, Bedrock,
    Secrets Manager) — `BotocoreInstrumentor`, which auto-instruments
    every boto3 client constructed after `configure_tracing()` runs,
    including inside `DynamoDBRaceStateRepository`/`BedrockLLMClient`/etc
    with NO code change needed in any of those modules.

AWS X-Ray-compatible trace IDs: uses `AwsXRayIdGenerator` (128-bit trace
IDs whose first 32 bits encode a Unix timestamp, matching X-Ray's own
trace-id format) and `AwsXRayPropagator` (the `X-Amzn-Trace-Id` header X-
Ray/ALB/API Gateway already use) rather than OpenTelemetry's own default
W3C `traceparent` propagator — this is what lets a trace continue
correctly across an ALB (which already injects `X-Amzn-Trace-Id` on every
request) and appear as one connected trace in X-Ray, not two disconnected
ones.

Correlation with the existing structured audit log
(app/observability.py): `current_trace_id()` returns the active span's
trace id (or None when tracing is disabled/no active span), and
`CorrelationIdMiddleware` (observability.py) includes it in
`request.state` alongside the existing `correlation_id`, so a person
debugging a production incident can pivot from one CloudWatch Logs line
straight into the matching X-Ray trace.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import Settings

logger = logging.getLogger(__name__)

_tracing_configured = False


def configure_tracing(settings: Settings) -> None:
    """Idempotent — safe to call more than once (e.g. from tests that
    call `create_app()` repeatedly); only the FIRST call with
    `otel_tracing_enabled=True` actually installs a TracerProvider and
    instruments FastAPI/httpx/botocore. A no-op entirely when
    `otel_tracing_enabled` is False (the default — see config.py), so
    every existing test and local-dev run is completely unaffected."""
    global _tracing_configured
    if not settings.otel_tracing_enabled or _tracing_configured:
        return

    # Imported lazily so the opentelemetry packages are only required at
    # runtime when tracing is actually enabled (mirrors this codebase's
    # existing convention for boto3/redis — see event_broker.py/
    # dynamodb.py's own lazy imports).
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.aws.aws_xray_propagator import AwsXRayPropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())
    exporter = OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    set_global_textmap(AwsXRayPropagator())

    # FastAPI instrumentation is applied per-app in `instrument_app()`
    # below (it needs the actual `FastAPI` instance) — httpx/botocore are
    # instrumented globally here since they patch the underlying
    # client/session classes, not a specific instance.
    HTTPXClientInstrumentor().instrument()
    BotocoreInstrumentor().instrument()

    _tracing_configured = True
    logger.info("OpenTelemetry tracing configured: service=%s endpoint=%s", settings.otel_service_name, settings.otel_exporter_otlp_endpoint)


def instrument_app(app, settings: Settings) -> None:
    """Instruments the FastAPI app instance itself (inbound-request spans).
    Must be called AFTER `configure_tracing` (so a TracerProvider already
    exists) and is itself a no-op when tracing is disabled."""
    if not settings.otel_tracing_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def current_trace_id() -> Optional[str]:
    """The active span's trace id as a 32-hex-char string (X-Ray/OTel's
    own hex representation), or None when tracing is disabled or there is
    no active span (e.g. a background task with no inbound request span).
    Never raises — safe to call unconditionally from logging code."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        context = span.get_span_context()
        if context is None or not context.is_valid:
            return None
        return format(context.trace_id, "032x")
    except Exception:  # noqa: BLE001 - tracing is diagnostic, never allowed to break a request
        return None


def shutdown_tracing() -> None:
    """Flushes and shuts down the TracerProvider — called from `main.py`'s
    lifespan on application shutdown so buffered spans are not silently
    dropped when a task is stopped/replaced. A no-op when tracing was
    never configured."""
    global _tracing_configured
    if not _tracing_configured:
        return
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is not None:
        shutdown()
    _tracing_configured = False
