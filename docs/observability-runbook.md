# Observability runbook

AWS hackathon-final pass, P2 (original pass) / phase 5 (this update — real
OTel tracing + EMF application metrics, see below). This is the
operator-facing companion to `docs/deployment-runbook.md`: what to look at,
what each alarm means, and what to do when it fires. See
`AWS_ARCHITECTURE.md` for the system-level picture this all instruments.

## Where to look

1. **`OperationalDashboard`** (CloudWatch console — the deployed stack's
   `OperationalDashboardUrl` output gives a direct link). One page: both
   ALBs' request/error/latency, ECS CPU/memory for both services,
   DynamoDB throttling/errors/consumed capacity, both Lambdas'
   errors/duration, the application-level auth/pairing metrics, the phase
   5 EMF metrics (Bedrock latency/throttling/fallback, event-broker
   degradation, and a per-`RaceId` `SEARCH(...)` widget for simulator
   lease-ownership changes / stale-leader write rejections), and every
   alarm's current status. Start here for "is the system healthy right
   now."
2. **Structured audit log** (`BackendLogGroup` in CloudWatch Logs) — every
   `audit_log(...)` call (`backend/app/observability.py`) emits one JSON
   line per event: `authentication_failed`, `device_pairing_succeeded`,
   `device_pairing_failed`, `device_pairing_rate_limited`,
   `device_revoked`, `pairing_code_generated`, and more. Every line is
   guaranteed free of secret-shaped fields (see `AuditFieldViolation` in
   `observability.py` — a call that would log a `token`/`secret`/
   `password`/etc.-suffixed field raises BEFORE logging, it does not log
   a redacted version). Use CloudWatch Logs Insights:
   ```
   fields @timestamp, @message
   | filter audit_event = "authentication_failed"
   | sort @timestamp desc
   | limit 50
   ```
3. **CloudWatch custom metrics (EMF), namespace `PitWallCopilot`** — phase
   5 addition, `backend/app/metrics.py`. A representative subset (NOT
   every metric a full production build would want — see that module's
   own docstring for the exact list of what is and is not covered):
   `BedrockInvocationLatency`/`BedrockThrottled`/`BedrockFallbackUsed`
   (dimension `Source`), `SimulatorLeaseOwnershipChanged`/
   `StaleLeaderWriteRejected` (dimension `RaceId` — dynamic, so browse
   these via the dashboard's `SEARCH(...)` widget or a Logs Insights
   query rather than a fixed metric-explorer chart), `PairingOutcome`
   (dimension `Outcome`), `EventBrokerDegraded` (dimension `Reason`).
   These are ordinary CloudWatch Metrics once emitted (EMF is
   auto-extracted by the platform from the structured log line — no
   extra IAM permission, no separate ingestion pipeline), so any
   CloudWatch console workflow (Metrics Explorer, `GetMetricData`, a
   Logs Insights query against the same log group) works on them exactly
   like a built-in `AWS/*` metric.
4. **X-Ray traces** — both ECS services and both Lambdas have the X-Ray
   daemon path enabled (`Tracing: Active` for Lambdas; the `xray-daemon`
   sidecar for ECS); use it to see where time actually goes on a slow
   request (e.g. a Bedrock `invoke_model` call vs. a DynamoDB round
   trip). Phase 5 additionally wires a proper OpenTelemetry pipeline for
   BOTH ECS services — `backend/app/tracing.py` for the backend and, added
   in a later correction pass, the equivalent `mcp_server/tracing.py` for
   the MCP server (same X-Ray-compatible id generator/propagator, same
   OTLP/HTTP-to-collector-sidecar export path) — each with its own
   `otel-collector` sidecar, gated by the same `EnableOtelTracing` stack
   parameter, that re-exports OTel spans to the SAME X-Ray console via the
   collector's `awsxray` exporter, using
   `AwsXRayIdGenerator`/`AwsXRayPropagator` so trace IDs from both paths
   are X-Ray-compatible and show up together, including trace context
   propagated from an inbound MCP request through to the backend call it
   triggers. The most recent repair pass redesigned `mcp_server/tracing.py`
   around explicit `TracerProvider` dependency injection (`configure_tracing()`/
   `instrument_app()` now accept an injected provider and return a
   structured `TracingRuntime` handle with an idempotent `.shutdown()`)
   instead of resetting OpenTelemetry's private global internals for
   tests, and along the way found and fixed a real sanitization gap: OTel's
   built-in URL redaction only strips four hardcoded signed-URL query
   parameter names, so this app now also runs its own generic query-string
   sanitizer over span attributes so a sensitive query parameter can never
   leak into a trace regardless of its name. A third repair pass hardened
   the tracing tests against ambient sampler configuration: OTel's SDK
   reads `OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` from the
   environment whenever a `TracerProvider` is constructed without an
   explicit `sampler=` (confirmed empirically —
   `OTEL_TRACES_SAMPLER=always_off pytest tests/test_tracing.py` produced
   6 real failures beforehand). Every `TracerProvider`
   `mcp_server/tests/test_tracing.py` constructs now explicitly passes
   `sampler=ALWAYS_ON`, and this module's own production
   provider-construction path now explicitly passes `sampler=DEFAULT_ON`
   (`ParentBased(ALWAYS_ON)`) — the SDK's existing default, made explicit
   rather than implicit, so production sampling behavior is unchanged.
   **HONEST STATUS**:
   `EnableOtelTracing` defaults to `"false"` — the collector sidecars are
   always provisioned (negligible idle cost, matches the `xray-daemon`
   sidecar's own always-on pattern) but neither service sends anything
   until this is explicitly set to `"true"` at deploy time, and this OTel
   path has been validated with `cfn-lint`/`sam validate` and unit tests
   only (`backend/tests/test_tracing.py`,
   `mcp_server/tests/test_tracing.py` — rewritten and grown from 12 to 20
   tests, proving real span creation, cross-service trace-context
   propagation, exporter-failure resilience, idempotent shutdown, and that
   no span attribute ever contains a bearer/access token or an arbitrary
   sensitive query parameter) — NOT against a real ADOT collector or a
   real X-Ray console (this sandbox has no AWS credentials and no route to
   a live collector).
   Look for the `X-Trace-Id` response header (`backend/app/observability.py`'s
   `CorrelationIdMiddleware`) to correlate one HTTP response back to a
   specific trace once tracing is actually enabled and exercised.
5. **`GET /health`** — liveness only (used by the ALB target group health
   check and `scripts/judge_demo.sh`'s pre-flight wait). It does not
   prove DynamoDB or Bedrock are reachable; for that, run
   `scripts/golden_demo.py` against the deployed URL, which exercises the
   real end-to-end path.

## Alarms and what to do when each fires

| Alarm | Fires when | Likely cause | First response |
|---|---|---|---|
| `<stack>-backend-unhealthy-hosts` | ≥1 unhealthy target on the backend ALB target group | A task crash-looping (bad image, missing env var/secret), or a genuinely overloaded task failing its health check | Check `BackendService`'s ECS console events and the task's `BackendLogGroup` output for a startup exception; `validate_production_configuration` failing at startup is a common cause — see its error message for exactly which setting is missing |
| `<stack>-race-state-table-throttled` | DynamoDB `ThrottledRequests` ≥ threshold over 5 min | A genuine traffic spike outrunning on-demand auto-scaling, or (less likely) a hot-partition access pattern | Check `ConsumedReadCapacityUnits`/`ConsumedWriteCapacityUnits` on the dashboard for the actual volume; DynamoDB on-demand usually self-heals within minutes — if sustained, check for a runaway client (e.g. a polling loop with no backoff) |
| `<stack>-race-state-table-system-errors` | DynamoDB-side (5xx-equivalent) `SystemErrors` ≥ threshold | An AWS-side DynamoDB issue, not this application's code | Check the [AWS Health Dashboard](https://health.aws.amazon.com/health/status) for a regional DynamoDB event; this is not something application code can retry its way out of if it's a genuine service event (though the SDK's own default retry behavior already absorbs isolated transient errors) |
| `<stack>-watch-evaluator-errors` | `WatchEvaluatorStreamFunction` raises on any invocation, 5 min window | A bug in `WatchEvaluator.evaluate` reachable from real stream records, or a DynamoDB Streams record shape this handler doesn't expect | Check the Lambda's own CloudWatch Logs for the traceback; the handler already swallows per-record evaluation errors (see its own module docstring), so an alarm here means the handler's *top-level* code failed, not one bad record — a more serious signal |
| `<stack>-pairing-failures` | Invalid/expired/already-used pairing-code attempts ≥ threshold, 5 min | The volume pattern a brute-force pairing-code guesser produces (each individual guess is indistinguishable from an honest typo; the aggregate is the signal) | Check `authentication_failed`/pairing audit-log lines for the source IP pattern (via `get_client_ip`'s trusted-proxy-resolved value, not the ALB's own peer address); consider the WAF's `/v1/auth/*`-scoped rate limit is already the first line of defense here — this alarm is the second |
| `<stack>-pairing-throttling` | The application-layer `PairingRateLimiter` itself starts rejecting (429s), 5 min | A single client has already exceeded the configured pairing-attempt budget — a later-stage, stronger signal than the failures alarm above | Same investigation as pairing-failures; by the time this fires, the rate limiter is already doing its job — this is a "should I dig deeper" signal, not itself an outage |
| `<stack>-authentication-failures` | `authentication_failed` audit events ≥ threshold (REST *or* WebSocket — see ADR 0002), 5 min | Credential stuffing/brute-forcing the demo token, a client with a stale/revoked device token retrying without backoff, or (benign) a burst of unpaired devices simply trying and failing as expected | Check the audit log for `path`/`transport` fields to see whether it's concentrated on one route (mutating REST vs. the WebSocket) or one client; a stale-token client should self-correct once `useDeviceAuth.ts` (Fire TV) or the Alexa+ client observes the 401/4401 and returns to pairing |
| `<stack>-event-broker-degraded` | EMF metric `EventBrokerDegraded{Reason=publish_failed}` ≥ threshold, 5 min | The cross-instance event broker (Redis/Valkey, when `EnableCrossInstanceEventBroker=true`) is rejecting or failing publishes — the exact failure mode that Rule exists to prevent architecturally is happening anyway because the broker itself is unhealthy | Check `EventBrokerCache`'s own ElastiCache console metrics (CPU, evictions, connection count) and the audit log's `event_broker_degraded` lines for `reason`; if this fires with `BackendDesiredCount=1` something is still wrong even though the single-task blast radius is smaller |
| `<stack>-bedrock-throttled` | EMF metric `BedrockThrottled{Source=bedrock}` ≥ threshold, 5 min | Bedrock model invocations are being throttled (account/region TPS limits, or a burst of demo traffic) | Check `BedrockFallbackUsed` on the dashboard for the actual judge-facing impact — every throttled call already degrades gracefully to the deterministic template narrator (see `LlmProvider`'s own description: Bedrock only ever explains, never calculates race facts), so this is a quality-of-explanation signal, not a correctness or availability risk |

## Structured logging conventions

- Every audit-relevant event goes through `audit_log(event_name, **fields)`,
  never a bare `logger.info(...)` — this is what makes every line
  machine-parseable JSON and guarantees the secret-field guard applies.
- `correlation_id` (from `CorrelationIdMiddleware`, sanitized against a
  strict charset — see `backend/app/observability.py`) is included on
  every event that has request context, so a single request's full story
  can be traced across log lines with one Logs Insights filter.
- Never add a new audit field ending in `token`, `secret`, `password`,
  `key`, or similar — `AuditFieldViolation` will reject the call outright
  at runtime (fail loud, not fail by silently logging the secret) and the
  matching test in `backend/tests/test_observability.py` will fail in CI
  if this guard is ever weakened.

## Related documents

- `AWS_ARCHITECTURE.md` — the system this instruments.
- `docs/deployment-runbook.md` — deploy, pairing, backup, and secret
  rotation procedures.
- `JUDGE_DEMO.md` / `scripts/judge_demo.sh` — the pre-flight check that
  proves the golden path works end-to-end, not just that `/health`
  returns 200.
