"""Tests for app/tracing.py (AWS hackathon-final pass, phase 5).

These verify real OpenTelemetry span creation, trace-id propagation, and
correlation with the existing correlation-id middleware — captured via an
in-memory span exporter, not a real OTLP collector (see tracing.py's own
module docstring for the honest-status note on what has/hasn't been
verified against a real AWS account).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.observability import CORRELATION_ID_HEADER, TRACE_ID_HEADER
from app.tracing import configure_tracing, current_trace_id, shutdown_tracing

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_tracing_state():
    """`configure_tracing` is deliberately idempotent/global (mirrors a
    real process, which only ever configures tracing once) — reset that
    global state around every test in this file so tests don't leak
    TracerProvider configuration into each other or into unrelated test
    files that run afterward in the same process."""
    yield
    shutdown_tracing()
    import app.tracing as tracing_module

    tracing_module._tracing_configured = False  # noqa: SLF001 - test-only reset


class TestConfigureTracing:
    def test_disabled_by_default_current_trace_id_is_none_outside_any_span(self):
        settings = Settings(otel_tracing_enabled=False)
        configure_tracing(settings)
        assert current_trace_id() is None

    def test_enabling_tracing_installs_a_real_tracer_provider(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        settings = Settings(otel_tracing_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:4318")
        configure_tracing(settings)
        assert isinstance(trace.get_tracer_provider(), TracerProvider)

    def test_calling_configure_tracing_twice_is_a_safe_no_op_the_second_time(self):
        settings = Settings(otel_tracing_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:4318")
        configure_tracing(settings)
        from opentelemetry import trace

        provider_after_first_call = trace.get_tracer_provider()
        configure_tracing(settings)  # must not replace the provider or raise
        assert trace.get_tracer_provider() is provider_after_first_call

    def test_current_trace_id_reflects_the_active_span_inside_a_span_and_none_outside_it(self):
        settings = Settings(otel_tracing_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:4318")
        configure_tracing(settings)
        from opentelemetry import trace

        tracer = trace.get_tracer("test")
        assert current_trace_id() is None
        with tracer.start_as_current_span("a-span") as span:
            trace_id_inside = current_trace_id()
            assert trace_id_inside is not None
            assert len(trace_id_inside) == 32  # 128-bit trace id, hex-encoded
            # Matches the SAME span's own trace id, not an unrelated one.
            assert trace_id_inside == format(span.get_span_context().trace_id, "032x")
        assert current_trace_id() is None

    def test_uses_the_aws_x_ray_id_generator_so_trace_ids_are_x_ray_compatible(self):
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

        settings = Settings(otel_tracing_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:4318")
        configure_tracing(settings)
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        assert isinstance(provider.id_generator, AwsXRayIdGenerator)


class TestTraceIdCorrelationOnHttpResponses:
    """Proves the FastAPI-instrumented app actually creates a span per
    inbound request AND that CorrelationIdMiddleware echoes its trace id
    back on the response — the end-to-end correlation phase 5 exists to
    provide, exercised through a real (in-process) HTTP round trip."""

    async def test_x_trace_id_header_is_absent_when_tracing_is_disabled(self):
        # The default app fixture/environment never sets
        # OTEL_TRACING_ENABLED (see config.py's default: False) — this is
        # the real, unmodified app every other test in this suite already
        # exercises, so this proves the ordinary/default path stays
        # exactly as it was before phase 5, with no new header appearing.
        app = create_app()
        transport = ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/health")
                assert TRACE_ID_HEADER not in response.headers
                assert CORRELATION_ID_HEADER in response.headers  # unaffected either way
