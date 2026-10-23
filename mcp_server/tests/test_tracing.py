"""Tests for tracing.py (repair pass, mandatory correction 4; tracing-
test-reliability redesign pass).

Verifies: (1) tracing is a strict no-op when disabled -- the honest
default every existing test in this suite already runs under; (2) real
OpenTelemetry span creation and the AWS X-Ray-compatible id
generator/propagator, mirroring backend/tests/test_tracing.py; (3) trace
context entering this MCP server actually propagates into its own
outbound call to the backend -- the specific end-to-end property mandatory
correction 4 exists to prove, not merely "a span gets created somewhere";
(4) no sensitive header value (the MCP bearer token, the demo-access
token this server forwards), pairing codes, request bodies, or sensitive
query-string parameters are ever attached to a span attribute, via a real
`InMemorySpanExporter` inspection of actually-exported spans, not a review
of configuration alone; (5) a failing/unreachable span exporter never
fails the actual request being traced; (6) `shutdown()` flushes exactly
once and repeated calls are harmless.

Redesign note: every test below that needs "tracing enabled with a real,
inspectable provider" constructs its OWN `TracerProvider` (with an
`InMemorySpanExporter`) and passes it explicitly into
`tracing.configure_tracing(tracer_provider=...)` /
`tracing.instrument_app(..., tracer_provider=...)`, then tears down via
that SAME runtime's own `.shutdown()`. None of this relies on resetting
OpenTelemetry's private one-shot-guard internals
(`trace._TRACER_PROVIDER_SET_ONCE`/`trace._TRACER_PROVIDER`) -- each
test's provider is independent of the real global and of every other
test, so test order and repeat-count cannot leak state between tests.
The one exception is `TestProductionConfigurationPath`, which exercises
the real "no injected provider" path end-to-end -- that necessarily
touches the real global `trace.set_tracer_provider()`, so it runs in a
subprocess, isolated from this test file's own process-wide OTel state.

None of these tests need the real backend subprocess `tests/conftest.py`
spins up for the rest of this suite -- a tiny in-process Starlette
double stands in for "the backend" wherever one is needed, connected via
a real background-thread uvicorn server (see `_running_fake_backend`),
so this file stays fast and has no dependency on `backend/.venv` being
set up.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

import tracing

pytestmark = pytest.mark.integration  # matches this suite's existing blanket tagging (see conftest.py)

_MCP_SERVER_DIR = Path(__file__).resolve().parent.parent


def _in_memory_provider():
    """A fresh, fully isolated TracerProvider + InMemorySpanExporter for
    one test. Never installed as the real global -- passed explicitly into
    `configure_tracing`/`instrument_app` instead, so it cannot leak into
    (or be affected by) any other test in this file or this process."""
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    # `sampler=ALWAYS_ON` is passed explicitly (rather than relying on
    # `TracerProvider`'s own default) so every span-count assertion in this
    # file is deterministic regardless of any ambient `OTEL_TRACES_SAMPLER`/
    # `OTEL_TRACES_SAMPLER_ARG` env var: without an explicit `sampler=`,
    # `TracerProvider.__init__` falls back to
    # `opentelemetry.sdk.trace.sampling._get_from_env_or_default()`, which
    # reads those env vars (e.g. `OTEL_TRACES_SAMPLER=traceidratio` with
    # `OTEL_TRACES_SAMPLER_ARG=0` would silently drop every span here).
    provider = TracerProvider(id_generator=AwsXRayIdGenerator(), sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture
def traced():
    """Yields (provider, exporter); tears down via the runtime's own
    `shutdown()` -- not via any OpenTelemetry global reset."""
    provider, exporter = _in_memory_provider()
    runtimes: list[tracing.TracingRuntime] = []

    def _configure() -> tracing.TracingRuntime:
        runtime = tracing.configure_tracing(enabled=True, tracer_provider=provider)
        runtimes.append(runtime)
        return runtime

    yield provider, exporter, _configure
    for runtime in runtimes:
        runtime.shutdown()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_backend_app(captured_headers: list[dict]) -> Starlette:
    """Stands in for the real backend service: records the headers of
    every request it receives (so a test can assert a propagated trace
    header actually arrived) and returns a trivial JSON body."""

    async def _handler(request: Request) -> JSONResponse:
        captured_headers.append(dict(request.headers))
        return JSONResponse({"lap": 24})

    return Starlette(routes=[Route("/v1/races/demo-race-001/state", _handler)])


@asynccontextmanager
async def _running_fake_backend(app: Starlette):
    """Serves `app` over a REAL TCP socket (a background-thread uvicorn,
    same pattern `tests/test_streamable_http_transport.py` already uses
    for the real MCP app) rather than `httpx.ASGITransport`.

    This matters specifically for these tracing tests:
    `HTTPXClientInstrumentor` patches `httpx`'s DEFAULT transport classes
    (`HTTPTransport`/`AsyncHTTPTransport`, used whenever an
    `httpx.AsyncClient` is constructed with no explicit `transport=`
    argument) -- it does NOT patch `ASGITransport`, which is a completely
    separate class used to talk to an ASGI app in-process without any
    socket at all. A test built around `ASGITransport` would never see
    trace-context headers injected, not because propagation is broken,
    but because that test would not be exercising the code path
    `HTTPXClientInstrumentor` actually instruments -- a real production
    request (this server calling the real backend over a real socket)
    always goes through the default transport, so a real socket is what
    this test needs to genuinely prove the propagation property, not an
    artifact of test plumbing."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = asyncio.get_event_loop().time() + 10
        while not server.started and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn server did not start within timeout"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class TestConfigureTracing:
    def test_disabled_by_default_current_trace_id_is_none_outside_any_span(self):
        runtime = tracing.configure_tracing(enabled=False)
        assert runtime.enabled is False
        assert runtime.tracer_provider is None
        assert tracing.current_trace_id() is None
        runtime.shutdown()  # must be a harmless no-op

    def test_enabling_tracing_with_an_injected_provider_uses_that_exact_provider(self, traced):
        provider, _exporter, configure = traced
        runtime = configure()
        assert runtime.enabled is True
        assert runtime.tracer_provider is provider

    def test_calling_configure_tracing_twice_with_the_same_injected_provider_is_a_safe_no_op(self, traced):
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        provider, _exporter, configure = traced
        first = configure()
        assert first.owns_httpx_instrumentation is True
        second = configure()
        # The second call must not raise, must not warn about "already
        # instrumented" in a way that breaks anything, and must not
        # attempt (or need) to re-instrument httpx.
        assert second.tracer_provider is provider
        assert second.owns_httpx_instrumentation is False
        assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True

    def test_current_trace_id_reflects_the_active_span_inside_a_span_and_none_outside_it(self, traced):
        provider, _exporter, configure = traced
        configure()
        tracer = provider.get_tracer("test")
        assert tracing.current_trace_id() is None
        with tracer.start_as_current_span("a-span") as span:
            trace_id_inside = tracing.current_trace_id()
            assert trace_id_inside is not None
            assert len(trace_id_inside) == 32  # 128-bit trace id, hex-encoded
            assert trace_id_inside == format(span.get_span_context().trace_id, "032x")
        assert tracing.current_trace_id() is None

    def test_uses_the_aws_x_ray_id_generator_so_trace_ids_are_x_ray_compatible(self, traced):
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

        provider, _exporter, configure = traced
        configure()
        assert isinstance(provider.id_generator, AwsXRayIdGenerator)


class TestProductionConfigurationPath:
    """Exercises the real "no injected provider" code path -- production's
    actual call shape (`configure_tracing()` with no arguments) -- which
    installs the real global `trace.set_tracer_provider()`. Run in a
    subprocess so this necessarily-global, one-shot-per-process behavior
    is isolated from every other test in this file/process."""

    def test_configure_tracing_with_no_injected_provider_installs_the_real_global_provider(self):
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {mcp_server_dir!r})
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
            import tracing

            runtime = tracing.configure_tracing(
                enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
            )
            assert runtime.enabled is True
            assert runtime.owns_global_provider is True
            provider = trace.get_tracer_provider()
            assert isinstance(provider, TracerProvider), provider
            assert isinstance(provider.id_generator, AwsXRayIdGenerator)
            assert provider is runtime.tracer_provider

            # A second, no-argument call must return the SAME runtime
            # rather than installing a second, competing provider.
            runtime_again = tracing.configure_tracing(
                enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
            )
            assert runtime_again is runtime
            assert trace.get_tracer_provider() is provider

            runtime.shutdown()
            runtime.shutdown()  # repeated shutdown must be harmless
            print("OK")
            """
        ).format(mcp_server_dir=str(_MCP_SERVER_DIR))
        # `configure_tracing()`'s own production path no longer pins any
        # explicit `sampler=` (see tracing.py -- that pin was deliberately
        # removed so production once again honors the standard
        # `OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` env vars). This
        # subprocess therefore DOES need `OTEL_TRACES_SAMPLER` set explicitly
        # to behave deterministically regardless of the ambient environment
        # this test happens to run under; the other tests in this class
        # exercise the env-var-driven behavior itself.
        subprocess_env = {**os.environ, "OTEL_TRACES_SAMPLER": "always_on"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout

    def test_test_providers_always_create_spans_deterministically_regardless_of_ambient_sampler_env(
        self, monkeypatch
    ):
        """Requirement (1): test-constructed providers must never depend on
        the ambient `OTEL_TRACES_SAMPLER*` env vars. Set the ambient env to
        something that would drop every span (`always_off`) and confirm
        `_in_memory_provider()`'s explicit `sampler=ALWAYS_ON` still produces
        spans -- proving the explicit test-side pin, not the environment,
        controls test sampling."""
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0")
        provider, exporter = _in_memory_provider()
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("should-still-be-recorded"):
            pass
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        assert finished[0].name == "should-still-be-recorded"

    def test_production_configuration_honors_always_off_sampler_env_var(self):
        """Requirement (2): with no explicit `sampler=` pin, the production
        (no-injected-provider) path must honor `OTEL_TRACES_SAMPLER=always_off`
        -- i.e. must create NO recording spans at all."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {mcp_server_dir!r})
            import tracing

            runtime = tracing.configure_tracing(
                enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
            )
            tracer = runtime.tracer_provider.get_tracer("test")
            with tracer.start_as_current_span("root") as span:
                assert span.is_recording() is False, "expected an unsampled (non-recording) span"
                assert span.get_span_context().trace_flags.sampled is False
            runtime.shutdown()
            print("OK")
            """
        ).format(mcp_server_dir=str(_MCP_SERVER_DIR))
        subprocess_env = {**os.environ, "OTEL_TRACES_SAMPLER": "always_off"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout

    def test_production_configuration_honors_traceidratio_sampler_env_var(self):
        """Requirement (3): with no explicit `sampler=` pin, the production
        path must honor `OTEL_TRACES_SAMPLER=parentbased_traceidratio` together
        with `OTEL_TRACES_SAMPLER_ARG` -- ratio 0.0 drops every root span,
        ratio 1.0 keeps every root span. Proves the ratio argument itself is
        read from the environment, not just the sampler name."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {mcp_server_dir!r})
            import tracing

            runtime = tracing.configure_tracing(
                enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
            )
            tracer = runtime.tracer_provider.get_tracer("test")
            with tracer.start_as_current_span("root") as span:
                recorded = span.is_recording()
            runtime.shutdown()
            print("RECORDED" if recorded else "NOT_RECORDED")
            """
        ).format(mcp_server_dir=str(_MCP_SERVER_DIR))

        zero_ratio_env = {
            **os.environ,
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "0.0",
        }
        zero_result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=zero_ratio_env
        )
        assert zero_result.returncode == 0, f"stdout={zero_result.stdout!r} stderr={zero_result.stderr!r}"
        assert "NOT_RECORDED" in zero_result.stdout

        full_ratio_env = {
            **os.environ,
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "1.0",
        }
        full_result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=full_ratio_env
        )
        assert full_result.returncode == 0, f"stdout={full_result.stdout!r} stderr={full_result.stderr!r}"
        assert "RECORDED" in full_result.stdout
        assert "NOT_RECORDED" not in full_result.stdout

    def test_production_configuration_honors_parentbased_sampling_and_an_unsampled_parent_stays_unsampled(self):
        """Requirement (4): with `OTEL_TRACES_SAMPLER=parentbased_always_on`
        (the SDK's own default when neither env var is set -- restored by
        removing the explicit `sampler=` pin), a span whose PARENT context is
        already unsampled must itself remain unsampled -- `ParentBased`
        defers to an existing parent's sampling decision rather than always
        sampling, which is what would happen if a bare `ALWAYS_ON` (not
        `ParentBased(ALWAYS_ON)`) were pinned instead."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {mcp_server_dir!r})
            from opentelemetry import trace
            from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
            import tracing

            runtime = tracing.configure_tracing(
                enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
            )
            tracer = runtime.tracer_provider.get_tracer("test")

            unsampled_parent_context = SpanContext(
                trace_id=0x{trace_id:032x},
                span_id=0x{span_id:016x},
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.DEFAULT),  # not sampled
            )
            ctx = trace.set_span_in_context(NonRecordingSpan(unsampled_parent_context))
            with tracer.start_as_current_span("child-of-unsampled-parent", context=ctx) as span:
                assert span.is_recording() is False, "child of an unsampled parent must stay unsampled"
                assert span.get_span_context().trace_flags.sampled is False
            runtime.shutdown()
            print("OK")
            """
        ).format(mcp_server_dir=str(_MCP_SERVER_DIR), trace_id=0x1234567890ABCDEF1234567890ABCDEF, span_id=0x1234567890ABCDEF)
        subprocess_env = {**os.environ, "OTEL_TRACES_SAMPLER": "parentbased_always_on"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout

    def test_tracing_disabled_creates_no_provider_and_no_spans_even_with_production_sampler_env_set(self):
        """Requirement (5): `enabled=False` must be a strict no-op -- no
        global provider installed, no spans created -- REGARDLESS of what
        `OTEL_TRACES_SAMPLER*` happens to be set to in the environment. This
        specifically guards against a regression where removing the explicit
        `sampler=` pin accidentally made the disabled path env-sensitive too."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {mcp_server_dir!r})
            from opentelemetry import trace
            from opentelemetry.trace import ProxyTracerProvider
            import tracing

            runtime = tracing.configure_tracing(enabled=False)
            assert runtime.enabled is False
            assert runtime.tracer_provider is None
            assert tracing.current_trace_id() is None
            # No real TracerProvider was ever installed as the global.
            assert isinstance(trace.get_tracer_provider(), ProxyTracerProvider)
            runtime.shutdown()
            print("OK")
            """
        ).format(mcp_server_dir=str(_MCP_SERVER_DIR))
        subprocess_env = {**os.environ, "OTEL_TRACES_SAMPLER": "always_on", "OTEL_TRACES_SAMPLER_ARG": "1.0"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout

    async def test_no_sensitive_data_in_spans_under_the_restored_env_driven_production_sampler_path(self):
        """Requirement (6): re-confirms `TestNoSensitiveDataInSpans`'s
        guarantee still holds under the now-restored env-driven production
        sampler path (`OTEL_TRACES_SAMPLER=parentbased_always_on`, i.e. every
        span sampled) -- no bearer token, pairing code, demo token, secret
        header or sensitive query-string value is ever attached to a span
        attribute, verified via a real `InMemorySpanExporter` inspection in
        a subprocess so the production (no-injected-provider) code path is
        the one actually exercised, not the test-injected-provider path the
        rest of `TestNoSensitiveDataInSpans` uses."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, "__MCP_SERVER_DIR__")
            import asyncio
            import httpx
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            from starlette.applications import Starlette
            from starlette.responses import PlainTextResponse
            from starlette.routing import Route
            import tracing

            async def _handler(request):
                _ = request.headers.get("authorization")
                return PlainTextResponse("ok")

            async def main():
                runtime = tracing.configure_tracing(
                    enabled=True, exporter_otlp_endpoint="http://127.0.0.1:4318"
                )
                exporter = InMemorySpanExporter()
                runtime.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

                app = Starlette(routes=[Route("/probe", _handler)])
                wrapped = tracing.instrument_app(app, enabled=True, tracer_provider=runtime.tracer_provider)
                transport = httpx.ASGITransport(app=wrapped)
                async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
                    response = await client.get(
                        "/probe?token=SHOULD-NOT-APPEAR",
                        headers={
                            "authorization": "Bearer SUPER-SECRET-TOKEN",
                            "x-pairing-code": "123456",
                            "x-demo-access-token": "DEMO-SECRET",
                        },
                    )
                assert response.status_code == 200

                finished = exporter.get_finished_spans()
                assert len(finished) >= 1
                forbidden = ("SUPER-SECRET-TOKEN", "123456", "DEMO-SECRET", "SHOULD-NOT-APPEAR")
                for span in finished:
                    for key, value in (span.attributes or {}).items():
                        text = str(value)
                        for needle in forbidden:
                            assert needle not in text, f"leaked {needle!r} in span attribute {key}={text!r}"
                runtime.shutdown()

            asyncio.run(main())
            print("OK")
            """
        ).replace("__MCP_SERVER_DIR__", str(_MCP_SERVER_DIR))
        subprocess_env = {**os.environ, "OTEL_TRACES_SAMPLER": "parentbased_always_on"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout


class TestInstrumentApp:
    def test_returns_the_app_unchanged_when_tracing_is_disabled(self):
        app = Starlette(routes=[])
        assert tracing.instrument_app(app, enabled=False) is app

    async def test_no_spans_are_produced_for_a_request_when_tracing_is_disabled(self):
        import httpx

        async def _handler(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/probe", _handler)])
        wrapped = tracing.instrument_app(app, enabled=False)
        assert wrapped is app

        _provider, exporter = _in_memory_provider()  # never installed/used -- disabled means no spans anywhere
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
            response = await client.get("/probe")
        assert response.status_code == 200
        assert exporter.get_finished_spans() == ()

    def test_wraps_the_app_in_the_asgi_middleware_when_enabled(self, traced):
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        provider, _exporter, configure = traced
        configure()
        app = Starlette(routes=[])
        wrapped = tracing.instrument_app(app, enabled=True, tracer_provider=provider)
        assert isinstance(wrapped, OpenTelemetryMiddleware)

    async def test_enabling_tracing_creates_a_real_server_span_for_an_inbound_request(self, traced):
        import httpx

        provider, exporter, configure = traced
        configure()

        async def _handler(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/probe", _handler)])
        wrapped = tracing.instrument_app(app, enabled=True, tracer_provider=provider)

        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
            response = await client.get("/probe")
        assert response.status_code == 200

        finished_spans = exporter.get_finished_spans()
        assert len(finished_spans) >= 1
        from opentelemetry.trace import SpanKind

        assert any(span.kind == SpanKind.SERVER for span in finished_spans)


class TestOutboundHttpxSpans:
    async def test_outbound_httpx_calls_create_client_spans(self, traced):
        import httpx
        from opentelemetry.trace import SpanKind

        provider, exporter, configure = traced
        configure()

        captured_headers: list[dict] = []
        async with _running_fake_backend(_fake_backend_app(captured_headers)) as backend_base_url:
            async with httpx.AsyncClient(base_url=backend_base_url) as client:
                response = await client.get("/v1/races/demo-race-001/state")
        assert response.status_code == 200

        finished_spans = exporter.get_finished_spans()
        client_spans = [span for span in finished_spans if span.kind == SpanKind.CLIENT]
        assert len(client_spans) >= 1, f"expected at least one CLIENT span, got: {finished_spans}"


class TestTraceContextPropagatesFromMcpIntoTheBackendRequest:
    """The specific end-to-end property mandatory correction 4 requires:
    a trace entering this MCP server continues, as the SAME trace, into
    this server's own outbound HTTP call to the backend -- not two
    disconnected traces (or none)."""

    async def test_a_span_created_inside_this_process_is_propagated_to_an_outbound_httpx_call(self, traced):
        import httpx

        provider, _exporter, configure = traced
        configure()

        captured_headers: list[dict] = []
        tracer = provider.get_tracer("test")
        async with _running_fake_backend(_fake_backend_app(captured_headers)) as backend_base_url:
            async with httpx.AsyncClient(base_url=backend_base_url) as client:
                with tracer.start_as_current_span("mcp-tool-call") as span:
                    expected_trace_id = format(span.get_span_context().trace_id, "032x")
                    response = await client.get("/v1/races/demo-race-001/state")

        assert response.status_code == 200
        assert len(captured_headers) == 1
        # AwsXRayPropagator's own wire header -- see tracing.py's module
        # docstring for why this (not W3C traceparent) is what's used.
        propagated = captured_headers[0].get("x-amzn-trace-id")
        assert propagated is not None, f"no trace header reached the backend request: {captured_headers[0]}"
        # The header's trace id (format: "Root=1-<8 hex>-<24 hex>;...")
        # must decode back to the SAME trace id the originating span
        # carried -- proving this is genuinely ONE continued trace, not an
        # unrelated new one the backend would start fresh.
        root_field = next(part for part in propagated.split(";") if part.startswith("Root="))
        _version, timestamp_hex, unique_hex = root_field[len("Root="):].split("-")
        propagated_trace_id = timestamp_hex + unique_hex
        assert propagated_trace_id == expected_trace_id

    async def test_an_inbound_trace_header_continues_as_the_same_trace_into_the_outbound_call(self, traced):
        """The other half of the same property: a trace that ENTERS this
        MCP server (e.g. from Alexa+/AgentCore, carrying its own
        `X-Amzn-Trace-Id`) must be picked up by the inbound ASGI
        instrumentation and continued into this server's own outbound
        call to the backend -- full round trip, not just "a span exists
        somewhere in this process."""
        import httpx

        provider, _exporter, configure = traced
        configure()

        captured_backend_headers: list[dict] = []
        inbound_trace_id = "1-6553f200-" + "a" * 24  # arbitrary but well-formed X-Ray trace id
        inbound_header = f"Root={inbound_trace_id};Parent=1234567890abcdef;Sampled=1"

        async with _running_fake_backend(_fake_backend_app(captured_backend_headers)) as backend_base_url:

            async def _mcp_route(request: Request) -> PlainTextResponse:
                # Exactly what server.py's `_request` helper does: make an
                # outbound call to the backend from inside a handler
                # running under the inbound request's own (propagated)
                # span context. A real socket to the fake backend above --
                # see `_running_fake_backend`'s own docstring for why
                # `ASGITransport` would not exercise the same
                # instrumentation a real deployment's httpx call does.
                async with httpx.AsyncClient(base_url=backend_base_url) as client:
                    await client.get("/v1/races/demo-race-001/state")
                return PlainTextResponse("ok")

            inner_app = Starlette(routes=[Route("/mcp-probe", _mcp_route)])
            instrumented_app = tracing.instrument_app(inner_app, enabled=True, tracer_provider=provider)

            # The MCP app's OWN inbound side is exercised via
            # `ASGITransport` here, which is fine: `OpenTelemetryMiddleware`
            # (unlike `HTTPXClientInstrumentor`) instruments at the ASGI
            # protocol layer itself, not a specific transport class, so it
            # behaves identically regardless of what transport delivered
            # the inbound ASGI `scope`/`receive`/`send` call.
            mcp_transport = httpx.ASGITransport(app=instrumented_app)
            async with httpx.AsyncClient(transport=mcp_transport, base_url="http://mcp.test") as client:
                response = await client.get("/mcp-probe", headers={"X-Amzn-Trace-Id": inbound_header})

        assert response.status_code == 200
        assert len(captured_backend_headers) == 1
        forwarded = captured_backend_headers[0].get("x-amzn-trace-id")
        assert forwarded is not None
        forwarded_root = next(part for part in forwarded.split(";") if part.startswith("Root="))
        assert forwarded_root == f"Root={inbound_trace_id}", (
            "the trace that entered this MCP server was not the SAME trace propagated to its "
            f"own outbound backend call: inbound={inbound_trace_id!r} forwarded={forwarded_root!r}"
        )


class TestNoSensitiveDataInSpans:
    """Mandatory correction 4: 'never put tokens/pairing codes/
    authorization headers/sensitive bodies/sensitive query params in
    spans.' Verified against REAL exported spans (captured via
    `InMemorySpanExporter`), not by inspecting instrumentor configuration
    alone. Exercises the project's actual shipped
    `configure_tracing`/`instrument_app` functions end to end, not ad hoc
    middleware built for the test."""

    async def test_no_span_attribute_contains_the_bearer_token_or_forwarded_demo_access_token(self, traced):
        import httpx

        provider, exporter, configure = traced
        configure()

        secret_bearer_token = "super-secret-mcp-bearer-token-do-not-leak"  # noqa: S105 - test fixture value
        secret_demo_access_token = "super-secret-demo-access-token-do-not-leak"  # noqa: S105

        captured_backend_headers: list[dict] = []

        async with _running_fake_backend(_fake_backend_app(captured_backend_headers)) as backend_base_url:

            async def _mcp_route(request: Request) -> PlainTextResponse:
                async with httpx.AsyncClient(base_url=backend_base_url) as client:
                    await client.get(
                        "/v1/races/demo-race-001/state",
                        headers={"X-Demo-Access-Token": secret_demo_access_token},
                    )
                return PlainTextResponse("ok")

            inner_app = Starlette(routes=[Route("/mcp-probe", _mcp_route)])
            wrapped = tracing.instrument_app(inner_app, enabled=True, tracer_provider=provider)

            mcp_transport = httpx.ASGITransport(app=wrapped)
            async with httpx.AsyncClient(transport=mcp_transport, base_url="http://mcp.test") as client:
                response = await client.get("/mcp-probe", headers={"Authorization": f"Bearer {secret_bearer_token}"})
            assert response.status_code == 200

            # Sanity check: the secret values really were sent over
            # the wire (otherwise this test would trivially pass for
            # the wrong reason) -- captured directly by the fake
            # backend/the test route above, outside of any
            # span/tracing machinery.
            assert captured_backend_headers[0].get("x-demo-access-token") == secret_demo_access_token

        finished_spans = exporter.get_finished_spans()
        assert len(finished_spans) > 0, "expected at least one span to have been exported"
        for span in finished_spans:
            for attribute_value in span.attributes.values():
                rendered = str(attribute_value)
                assert secret_bearer_token not in rendered, (
                    f"span {span.name!r} attribute leaked the MCP bearer token: {rendered!r}"
                )
                assert secret_demo_access_token not in rendered, (
                    f"span {span.name!r} attribute leaked the forwarded demo-access token: {rendered!r}"
                )

    async def test_pairing_codes_and_request_bodies_are_not_captured_as_span_attributes(self, traced):
        """This app's own request bodies (e.g. `create_strategy_watch`'s
        candidate/target driver JSON body, or a pairing-code exchange
        body) are never captured as span attributes -- neither
        instrumentor is configured to capture request/response bodies at
        all, only method/route/status-code metadata."""
        import httpx

        provider, exporter, configure = traced
        configure()

        secret_pairing_code = "PAIR-7f3c9a-do-not-leak"  # noqa: S105
        captured_backend_bodies: list[bytes] = []

        async def _backend_handler(request: Request) -> JSONResponse:
            captured_backend_bodies.append(await request.body())
            return JSONResponse({"lap": 24})

        backend_app = Starlette(routes=[Route("/v1/races/demo-race-001/state", _backend_handler, methods=["POST"])])

        async with _running_fake_backend(backend_app) as backend_base_url:

            async def _mcp_route(request: Request) -> PlainTextResponse:
                async with httpx.AsyncClient(base_url=backend_base_url) as client:
                    await client.post(
                        "/v1/races/demo-race-001/state",
                        json={"pairing_code": secret_pairing_code},
                    )
                return PlainTextResponse("ok")

            inner_app = Starlette(routes=[Route("/mcp-probe", _mcp_route)])
            wrapped = tracing.instrument_app(inner_app, enabled=True, tracer_provider=provider)

            mcp_transport = httpx.ASGITransport(app=wrapped)
            async with httpx.AsyncClient(transport=mcp_transport, base_url="http://mcp.test") as client:
                response = await client.get("/mcp-probe")
            assert response.status_code == 200

            # Sanity check the body really was sent, and captured by the
            # test's own plain HTTP handler, not tracing machinery.
            assert captured_backend_bodies and secret_pairing_code.encode() in captured_backend_bodies[0]

        finished_spans = exporter.get_finished_spans()
        assert len(finished_spans) > 0
        for span in finished_spans:
            for attribute_value in span.attributes.values():
                assert secret_pairing_code not in str(attribute_value), (
                    f"span {span.name!r} attribute leaked the pairing code / request body: {attribute_value!r}"
                )

    async def test_sensitive_query_string_parameters_are_absent_from_span_attributes(self, traced):
        """This app's own real calls never put a token/code in a URL query
        string (see tracing.py's module docstring), so this test
        constructs a request that DOES have one, proving the general
        sanitization behavior is actually exercised rather than being
        vacuously true. Neither instrumentor captures raw query-string
        content as a span attribute value -- only the URL's scheme/host/
        path/method/status-code are recorded by default."""
        import httpx

        provider, exporter, configure = traced
        configure()

        secret_query_token = "super-secret-query-token-do-not-leak"  # noqa: S105

        async def _handler(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/probe", _handler)])
        wrapped = tracing.instrument_app(app, enabled=True, tracer_provider=provider)

        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
            response = await client.get("/probe", params={"access_token": secret_query_token})
        assert response.status_code == 200

        finished_spans = exporter.get_finished_spans()
        assert len(finished_spans) > 0
        for span in finished_spans:
            for attribute_value in span.attributes.values():
                assert secret_query_token not in str(attribute_value), (
                    f"span {span.name!r} attribute leaked a sensitive query-string parameter: {attribute_value!r}"
                )


class TestExporterFailureNeverBreaksTheRequest:
    """Item 7: an unreachable/failing span exporter must never fail the
    actual HTTP request being traced. The underlying
    `SimpleSpanProcessor`/`BatchSpanProcessor` already swallow exporter
    exceptions by design (OTel exporters return an `ExportResult` rather
    than raising, and the processors additionally catch any exception a
    misbehaving exporter DOES raise) -- this proves that end to end for
    this app's own `configure_tracing`/`instrument_app`."""

    async def test_a_raising_exporter_does_not_fail_the_instrumented_request(self):
        import httpx
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

        export_attempts: list[int] = []

        class _RaisingExporter:
            def export(self, spans):
                export_attempts.append(len(spans))
                raise RuntimeError("simulated exporter failure (network error / unreachable collector)")

            def shutdown(self):
                return None

            def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover - not exercised
                return True

        # See `_in_memory_provider()`'s comment: `sampler=ALWAYS_ON` is
        # explicit so this test's own span-export assertions cannot be
        # affected by an ambient `OTEL_TRACES_SAMPLER*` env var either.
        provider = TracerProvider(id_generator=AwsXRayIdGenerator(), sampler=ALWAYS_ON)
        provider.add_span_processor(SimpleSpanProcessor(_RaisingExporter()))
        runtime = tracing.configure_tracing(enabled=True, tracer_provider=provider)
        try:

            async def _handler(_request: Request) -> PlainTextResponse:
                return PlainTextResponse("ok")

            app = Starlette(routes=[Route("/probe", _handler)])
            wrapped = tracing.instrument_app(app, enabled=True, tracer_provider=provider)

            transport = httpx.ASGITransport(app=wrapped)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
                response = await client.get("/probe")

            # The request must succeed normally despite the exporter
            # raising on every export attempt.
            assert response.status_code == 200
            assert response.text == "ok"
            assert len(export_attempts) >= 1, "the raising exporter was never actually invoked by this test"
        finally:
            runtime.shutdown()

        del SpanExportResult  # imported only to document the real (non-raising) exporter contract


class TestShutdownTracing:
    def test_shutdown_is_a_no_op_when_tracing_was_never_configured(self):
        tracing.shutdown_tracing()  # must not raise

    def test_shutdown_flushes_the_configured_provider_exactly_once(self, traced):
        provider, _exporter, configure = traced
        runtime = configure()

        flush_calls: list[bool] = []
        original_shutdown = provider.shutdown

        def _recording_shutdown(*args, **kwargs):
            flush_calls.append(True)
            return original_shutdown(*args, **kwargs)

        provider.shutdown = _recording_shutdown

        runtime.shutdown()
        runtime.shutdown()  # repeated call must be harmless and NOT flush again

        assert flush_calls == [True]

    def test_repeated_shutdown_calls_are_harmless(self, traced):
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        _provider, _exporter, configure = traced
        runtime = configure()
        assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True

        runtime.shutdown()
        assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False
        runtime.shutdown()  # must not raise, must not try to uninstrument again
        runtime.shutdown()
