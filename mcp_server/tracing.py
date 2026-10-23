"""Real distributed tracing for the MCP server (repair pass, mandatory
correction 4; tracing-test-reliability redesign pass).

*** HONEST STATUS BEFORE THIS PASS ***
`JUDGING_EVIDENCE.md`/`AWS_ARCHITECTURE.md` claimed BOTH ECS services (the
backend and this MCP server) had X-Ray/OpenTelemetry tracing. That was
untrue for this service: `backend/app/tracing.py` existed and was wired
into `backend/app/main.py`'s lifespan, but nothing in `mcp_server/`
ever created a span, instrumented an HTTP client, or exported anything --
there was no telemetry sidecar, no IAM permission, and no env var wired
into `infrastructure/template.yaml`'s `McpTaskDefinition` for it either.
A trace entering this service (from Alexa+/AgentCore, or from a judge's
own curl against `/mcp`) had nowhere to go, and the SAME request's
downstream call to the backend (`server.py`'s `http_client.request(...)`)
carried no propagated trace context at all -- two disconnected traces (or
zero, with tracing off) rather than the one connected
Alexa+/MCP-server->backend->DynamoDB/Bedrock trace the architecture docs
described. This module, plus this pass's changes to `server.py` (calling
`configure_tracing`/`instrument_app`/`shutdown_tracing`) and
`infrastructure/template.yaml` (the MCP task's own ADOT collector sidecar,
env vars, and least-privilege X-Ray IAM permissions), is the fix.

Deliberately mirrors `backend/app/tracing.py`'s design point for point --
same AWS X-Ray-compatible trace-id generator, same `X-Amzn-Trace-Id`
propagator, same OTLP/HTTP-to-an-ADOT-collector-sidecar export path, same
disabled-by-default/idempotent/never-break-the-request posture -- so a
trace started in this process and one started in the backend process use
the exact same ID format and propagation header, and a single logical
request (Alexa+ -> this MCP server -> the backend -> DynamoDB/Bedrock)
appears as ONE connected trace in X-Ray, not two unrelated ones. See that
module's own docstring for the full rationale this one does not repeat,
and for the same "not verified against a real AWS account -- no AWS access
in this development environment" honest-status caveat, which applies
here identically: what HAS been verified is real span creation, trace
propagation into the (test-double) backend, and correct disable/shutdown
behavior -- see `tests/test_tracing.py`, which captures exported spans via
an in-memory span exporter rather than a real OTLP collector.

Trace propagation path this instruments, for this process specifically:
  inbound MCP request (Alexa+/AgentCore/a judge's curl) -> this ASGI app
  (`opentelemetry-instrumentation-asgi`'s `OpenTelemetryMiddleware`,
  wrapped around the Starlette app `server.py:build_streamable_http_app`
  returns) -> this server's own outbound `httpx.AsyncClient` call to the
  backend (`opentelemetry-instrumentation-httpx`'s `HTTPXClientInstrumentor`,
  which both creates a CLIENT span for that call AND injects the
  propagated trace context as an outbound header) -> the backend's own
  `FastAPIInstrumentor`-created SERVER span (see backend/app/tracing.py)
  picks that same trace up and continues it, exactly matching what the
  now-corrected architecture docs describe.

No dedicated `opentelemetry-instrumentation-starlette` package is used
(the general-purpose `opentelemetry-instrumentation-asgi` package --
already a transitive dependency of `opentelemetry-instrumentation-httpx`
and every other web-framework instrumentor -- wraps ANY ASGI app,
including a Starlette one, at the ASGI protocol layer itself; there is no
FastAPI-specific dependency-injection or route-matching machinery here
the way there is in the backend, only a Starlette app FastMCP builds, so
the generic ASGI-layer wrapping is not a lesser substitute -- it is the
correct instrumentation point for this specific app).

*** No sensitive data in spans ***
Neither `OpenTelemetryMiddleware` (inbound) nor `HTTPXClientInstrumentor`
(outbound) is configured to capture ANY request/response HEADER VALUES --
`http_capture_headers_server_request`/`_server_response` are left at
their default of `None` below, and the httpx instrumentor takes no
header-capture parameter at all. Both instrumentors' default span
attributes are limited to method, route/URL (host+path, not query
string contents in a way that would leak an access token passed as a
header -- this service takes its own bearer token via header, never a URL
query parameter, and the backend's own equivalent
`PIT_WALL_DEMO_ACCESS_TOKEN`/pairing-code exchange bodies are likewise
never captured), and status code. This means the `Authorization: Bearer
<PIT_WALL_MCP_ACCESS_TOKEN>` header this service's own `_AuthMiddleware`
checks, the `X-Demo-Access-Token` header `server.py` forwards to the
backend, and every tool call's request/response BODY (which could
contain a pairing code or other race data, though nothing in the 5 P0
tools' payloads is itself a secret) are never attached to a span as an
attribute. `tests/test_tracing.py` asserts this directly against real
captured spans, not by inspecting configuration alone.

*** Dependency-injection redesign (this pass) ***
An earlier version of this module's test suite made tests reliable by
directly poking OpenTelemetry's PRIVATE one-shot globals
(`trace._TRACER_PROVIDER_SET_ONCE`/`trace._TRACER_PROVIDER`) between
tests, since `trace.set_tracer_provider()` is a documented one-shot-per-
process operation. That is fragile: it depends on implementation details
that are not part of OpenTelemetry's public contract, and can produce
"passes alone, fails in the full suite" or "passes today, fails under a
different run order" symptoms if any test leaves that private state in an
unexpected shape.

`configure_tracing()` and `instrument_app()` now both accept an explicit
`tracer_provider` parameter. When a caller (a test) supplies one, this
module NEVER touches the real global tracer provider at all -- the
provider is threaded explicitly into `HTTPXClientInstrumentor().instrument
(tracer_provider=...)` and `OpenTelemetryMiddleware(app, tracer_provider=
...)`, both of which support this directly. Only when no explicit
provider is given (the real production call site, `server.py`, which only
ever calls this once per process) does this module fall back to
constructing its own provider and calling the real
`trace.set_tracer_provider()` -- ordinary, correct OTel usage for a real
process, not a test-time crutch.

`configure_tracing()` returns a `TracingRuntime` -- a small object holding
the provider/exporter actually used, whether this specific call performed
the (possibly no-op) httpx instrumentation, and a `shutdown()` method that
cleanly flushes the provider and uninstruments httpx exactly once, no
matter how many times `shutdown()` is called or whether instrumentation
ever actually happened. Tests construct their OWN `TracerProvider` (with
an `InMemorySpanExporter` for fast, deterministic assertions), pass it in
explicitly, and tear down via that SAME runtime's own `shutdown()` --
never via resetting OpenTelemetry's private internals.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Mirrors backend/app/config.py's Settings fields (otel_tracing_enabled,
# otel_exporter_otlp_endpoint, otel_service_name) as plain env-var reads --
# this module has no pydantic Settings class of its own (server.py itself
# reads every other env var directly with `os.environ.get`, and this
# stays consistent with that existing, deliberately lightweight style
# rather than introducing a new config framework for three values).
OTEL_TRACING_ENABLED = os.environ.get("PIT_WALL_MCP_OTEL_TRACING_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("PIT_WALL_MCP_OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
OTEL_SERVICE_NAME = os.environ.get("PIT_WALL_MCP_OTEL_SERVICE_NAME", "pit-wall-mcp-server")


@dataclass
class TracingRuntime:
    """Everything `configure_tracing()` set up, handed back explicitly so
    callers (production code and tests alike) never need to reach into
    OpenTelemetry's global/module state to find or tear it down.

    `enabled=False` (tracing turned off) is represented as a `TracingRuntime`
    too, with every other field left at its default -- so callers can
    always treat the return value uniformly (`runtime.shutdown()` is always
    safe to call) instead of special-casing "tracing was never configured".
    """

    enabled: bool
    tracer_provider: Optional[Any] = None
    exporter: Optional[Any] = None
    # True only when THIS call to configure_tracing() actually performed
    # `HTTPXClientInstrumentor().instrument(...)` -- False when tracing is
    # disabled, or when httpx was already instrumented (e.g. by an earlier
    # configure_tracing() call whose runtime is responsible for
    # uninstrumenting it instead). Used so `shutdown()` only ever
    # uninstruments httpx when this specific runtime is the one that
    # instrumented it, and never raises when it did not.
    owns_httpx_instrumentation: bool = False
    # True only when this runtime constructed its OWN TracerProvider and
    # installed it as the real global (the no-`tracer_provider`-argument,
    # production call path). A runtime built from an explicitly-injected
    # provider (the test path) never touches the real global and this
    # stays False.
    owns_global_provider: bool = False
    _shutdown_called: bool = field(default=False, repr=False, compare=False)

    def shutdown(self) -> None:
        """Flushes and shuts down the tracer provider and uninstruments
        httpx if this runtime is the one that instrumented it -- exactly
        once, no matter how many times this is called. Never raises: a
        telemetry teardown problem must never fail whatever code path
        (a test teardown, `server.py`'s shutdown handler) called this."""
        if self._shutdown_called:
            return
        self._shutdown_called = True

        if self.owns_httpx_instrumentation:
            try:
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

                instrumentor = HTTPXClientInstrumentor()
                if instrumentor.is_instrumented_by_opentelemetry:
                    instrumentor.uninstrument()
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.exception("Failed to uninstrument HTTPXClientInstrumentor during tracing shutdown")

        if self.tracer_provider is not None:
            shutdown_fn = getattr(self.tracer_provider, "shutdown", None)
            if shutdown_fn is not None:
                try:
                    shutdown_fn()
                except Exception:  # noqa: BLE001 - teardown must never raise
                    logger.exception("Failed to shut down TracerProvider")


# The runtime returned by the most recent no-explicit-provider (production)
# `configure_tracing()` call -- what `shutdown_tracing()` acts on. A test
# that injects its own `tracer_provider` gets back its OWN `TracingRuntime`
# and is expected to call that object's `shutdown()` directly rather than
# going through this module-level singleton, so tests never share or fight
# over this value.
_module_runtime: Optional[TracingRuntime] = None


def _strip_query_string_from_span_url(span: Any) -> None:
    """Removes any query string from this span's `http.url`/`url.full`
    attribute, in place.

    Both `OpenTelemetryMiddleware` (inbound) and `HTTPXClientInstrumentor`
    (outbound) capture a full request URL -- INCLUDING its query string --
    as a span attribute by default; OpenTelemetry's own built-in
    `redact_url` only scrubs a short, fixed list of known signed-URL
    parameter names (`AWSAccessKeyId`, `Signature`, `sig`,
    `X-Goog-Signature`), not an arbitrary token/code parameter name an
    application might use. This service's own real calls never place a
    token, pairing code or other sensitive value in a URL query string
    (every credential travels as a header -- see this module's "No
    sensitive data in spans" docstring section, and `get_regulation`'s
    only query parameter is a plain, non-sensitive search string) -- but
    this hook removes the query string unconditionally rather than
    special-casing known parameter names, so the protection holds even if
    a future caller (or a misbehaving client) ever did put something
    sensitive in a query string."""
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return
    attributes = getattr(span, "attributes", None)
    if not attributes:
        return
    for attribute_key in ("http.url", "url.full"):
        current_value = attributes.get(attribute_key)
        if isinstance(current_value, str) and "?" in current_value:
            span.set_attribute(attribute_key, current_value.split("?", 1)[0])


def _asgi_server_request_hook(span: Any, _scope: Any) -> None:
    _strip_query_string_from_span_url(span)


def _httpx_request_hook(span: Any, _request_info: Any) -> None:
    _strip_query_string_from_span_url(span)


async def _httpx_async_request_hook(span: Any, _request_info: Any) -> None:
    _strip_query_string_from_span_url(span)


def configure_tracing(
    *,
    enabled: bool = OTEL_TRACING_ENABLED,
    exporter_otlp_endpoint: str = OTEL_EXPORTER_OTLP_ENDPOINT,
    service_name: str = OTEL_SERVICE_NAME,
    tracer_provider: Optional[Any] = None,
) -> TracingRuntime:
    """Sets up tracing and returns a `TracingRuntime` describing what was
    installed. A no-op (beyond returning a disabled `TracingRuntime`) when
    `enabled` is False (the default), so every existing test and local/
    standalone run (`python server.py` with no tracing env vars set) is
    completely unaffected -- matching backend/app/tracing.py's own
    disabled-by-default contract exactly.

    `tracer_provider`: when given (tests), THIS provider is used as-is --
    it is never installed as the real global `trace.set_tracer_provider()`,
    and it is threaded explicitly into `HTTPXClientInstrumentor.instrument
    (tracer_provider=...)` so httpx spans land on it regardless of whatever
    the real global provider happens to be in that process. This is what
    lets tests construct an isolated, inspectable provider (e.g. with an
    `InMemorySpanExporter`) without needing to fight OpenTelemetry's
    real, one-shot-per-process global.

    When `tracer_provider` is omitted (the real `server.py` call site),
    this builds a `TracerProvider` and installs it as the real global via
    `trace.set_tracer_provider()` -- ordinary OTel usage for a process
    that only ever configures tracing once. Calling this again with no
    `tracer_provider` (e.g. a double-call in production) returns the SAME
    `TracingRuntime` rather than constructing a second, competing provider
    or re-instrumenting httpx.

    Keyword-only, defaulted parameters (rather than a Settings object,
    unlike the backend) so tests can override any of the three plain
    values directly without needing a config class this module doesn't
    otherwise have a reason to define."""
    global _module_runtime

    if not enabled:
        return TracingRuntime(enabled=False)

    # Idempotency for the production (no injected provider) call path only:
    # a caller that doesn't inject its own provider and calls this more
    # than once gets back the SAME runtime instead of constructing a
    # second TracerProvider and fighting OpenTelemetry's real one-shot
    # `trace.set_tracer_provider()` guard (which would otherwise just log
    # a warning and silently keep the first provider anyway).
    if (
        tracer_provider is None
        and _module_runtime is not None
        and _module_runtime.enabled
        and _module_runtime.owns_global_provider
    ):
        return _module_runtime

    # Imported lazily so the opentelemetry packages are only required at
    # runtime when tracing is actually enabled -- mirrors
    # backend/app/tracing.py's own lazy-import convention (and this
    # server's existing convention of importing `uvicorn` lazily inside
    # `main()`), so a plain `python server.py` / test run with tracing
    # disabled never needs these packages importable at all.
    from opentelemetry import trace
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.aws.aws_xray_propagator import AwsXRayPropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    owns_global_provider = tracer_provider is None
    exporter = None
    if owns_global_provider:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({SERVICE_NAME: service_name})
        # No `sampler=` argument is passed here -- this is a deliberate,
        # REVERSED choice from an earlier revision of this module, which
        # pinned `sampler=DEFAULT_ON` explicitly. That pin was well-intentioned
        # (it stopped this process's sampling decision from silently depending
        # on whatever `OTEL_TRACES_SAMPLER*` happened to be set in the ambient
        # environment) but it had a real cost: it made it IMPOSSIBLE for a
        # deployment to actually configure production sampling via the
        # standard, portable OpenTelemetry mechanism (the `OTEL_TRACES_SAMPLER`
        # / `OTEL_TRACES_SAMPLER_ARG` environment variables that every OTel SDK
        # in every language honors), because the explicit `sampler=` keyword
        # argument to `TracerProvider.__init__` always takes precedence over
        # those env vars, no matter what they are set to.
        #
        # Leaving `sampler` unset restores the SDK's own standard behavior:
        # `TracerProvider.__init__` falls back to
        # `opentelemetry.sdk.trace.sampling._get_from_env_or_default()`,
        # which reads `OTEL_TRACES_SAMPLER` (default `"parentbased_always_on"`
        # when unset -- functionally identical to the previous `DEFAULT_ON`
        # pin) and, for ratio-based samplers, `OTEL_TRACES_SAMPLER_ARG`. This
        # exactly mirrors `backend/app/tracing.py`'s own `TracerProvider(...)`
        # call, which has never pinned an explicit sampler.
        #
        # `infrastructure/template.yaml` now sets `OTEL_TRACES_SAMPLER` /
        # `OTEL_TRACES_SAMPLER_ARG` as real task-definition environment
        # variables (via the `TracesSamplerArg` CloudFormation parameter), so
        # production sampling is configured there -- changeable by updating
        # the task definition's environment and forcing a new deployment,
        # with NO container image rebuild required. Local/standalone runs and
        # any environment that sets neither var keep the SDK's own
        # `parentbased_always_on` default, so this change is behavior-neutral
        # everywhere `OTEL_TRACES_SAMPLER*` is not explicitly set.
        #
        # Tests NEVER rely on this fallback: every test that constructs its
        # own `TracerProvider` (via the `tracer_provider=` injection path
        # below) pins `sampler=ALWAYS_ON` explicitly and unconditionally, so
        # test outcomes never depend on the developer machine's or CI
        # runner's ambient environment variables.
        provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())
        exporter = OTLPSpanExporter(endpoint=f"{exporter_otlp_endpoint}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    else:
        provider = tracer_provider

    # Same AWS X-Ray-compatible propagator the backend installs -- REQUIRED
    # for the two processes' traces to actually connect: both ends of a
    # single request must agree on the same wire-format header
    # (`X-Amzn-Trace-Id`, not OpenTelemetry's own default `traceparent`)
    # or a trace entering this service would simply start a new,
    # disconnected trace on the backend side instead of continuing this
    # one. `set_global_textmap` (unlike `set_tracer_provider`) is not a
    # one-shot operation -- calling it repeatedly with the same value is
    # harmless, so this is safe regardless of the provider-injection path.
    set_global_textmap(AwsXRayPropagator())

    # ASGI (inbound) instrumentation is applied per-app in
    # `instrument_app()` below (it needs the actual Starlette app
    # instance, built by `server.py:build_streamable_http_app`) -- httpx
    # (outbound, this server's own calls to the backend) is instrumented
    # globally here since it patches the underlying client class, not a
    # specific instance, exactly matching backend/app/tracing.py's own
    # split between per-instance FastAPI instrumentation and global httpx/
    # botocore instrumentation. `tracer_provider=provider` is passed
    # explicitly so instrumented httpx clients use THIS provider even when
    # it is not the real global one (the test-injection path).
    instrumentor = HTTPXClientInstrumentor()
    instrumented_now = False
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(
            tracer_provider=provider,
            request_hook=_httpx_request_hook,
            async_request_hook=_httpx_async_request_hook,
        )
        instrumented_now = True

    runtime = TracingRuntime(
        enabled=True,
        tracer_provider=provider,
        exporter=exporter,
        owns_httpx_instrumentation=instrumented_now,
        owns_global_provider=owns_global_provider,
    )
    if owns_global_provider:
        _module_runtime = runtime

    logger.info("OpenTelemetry tracing configured: service=%s endpoint=%s", service_name, exporter_otlp_endpoint)
    return runtime


def instrument_app(app, *, enabled: bool = OTEL_TRACING_ENABLED, tracer_provider: Optional[Any] = None):
    """Wraps `app` (the Starlette app `build_streamable_http_app` builds)
    with `OpenTelemetryMiddleware`, creating a SERVER span for every
    inbound request at the ASGI layer -- the MCP-endpoint equivalent of
    `backend/app/tracing.py:instrument_app`'s `FastAPIInstrumentor.
    instrument_app(app)`. Should be called with the `TracingRuntime.
    tracer_provider` returned by `configure_tracing()` so the ASGI
    middleware uses that same provider explicitly, rather than picking up
    whatever the real global tracer provider happens to be -- this is what
    lets a test inject its own provider without needing
    `configure_tracing()` to have touched the real global at all. Returns
    `app` UNCHANGED -- a no-op passthrough -- when `enabled` is False, so
    every caller can always write
    `app = instrument_app(build_streamable_http_app(...))` unconditionally,
    exactly like the backend's own call site in `main.py`.

    Returns the (possibly wrapped) app rather than mutating in place,
    because `OpenTelemetryMiddleware` is itself the outermost ASGI
    callable an uvicorn server needs to be pointed at -- unlike Starlette's
    own `add_middleware`, which mutates the app's middleware stack
    in-place, wrapping is how ASGI middleware composition normally works
    for a middleware from OUTSIDE the Starlette framework itself."""
    if not enabled:
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    # No `http_capture_headers_*` arguments passed -- see this module's own
    # docstring, "No sensitive data in spans": the default is to capture
    # no header VALUES at all (only method/route/status-code attributes),
    # which is exactly what this service needs given the bearer token on
    # every MCP request and the demo-access-token header this server
    # itself forwards to the backend.
    return OpenTelemetryMiddleware(
        app,
        tracer_provider=tracer_provider,
        server_request_hook=_asgi_server_request_hook,
    )


def current_trace_id() -> Optional[str]:
    """The active span's trace id as a 32-hex-char string (X-Ray/OTel's
    own hex representation), or None when tracing is disabled or there is
    no active span. Never raises -- safe to call unconditionally from
    logging/error-handling code. Identical contract to
    `backend/app/tracing.py:current_trace_id`, so a person correlating a
    log line from either service uses the exact same trace-id format to
    pivot into X-Ray/CloudWatch ServiceLens.

    This reads the ambient span bound to the current execution context
    (via `trace.get_current_span()`), which is set by
    `tracer.start_as_current_span(...)` regardless of which
    `TracerProvider` created that tracer -- so this works identically
    whether the active span came from the real global provider (production)
    or from an explicitly-injected one (tests)."""
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
    """Flushes and shuts down the tracing configured by the most recent
    no-explicit-provider `configure_tracing()` call -- called from
    `server.py`'s uvicorn shutdown path so buffered spans are not silently
    dropped when this task is stopped/replaced, exactly mirroring
    `backend/app/tracing.py:shutdown_tracing`. A no-op when tracing was
    never configured (or was configured with an explicitly-injected
    provider, whose owner is expected to call that runtime's own
    `shutdown()` directly instead)."""
    global _module_runtime
    if _module_runtime is None:
        return
    _module_runtime.shutdown()
