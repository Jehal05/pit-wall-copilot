# ADR 0004: CloudWatch Embedded Metric Format instead of direct `PutMetricData`

Status: **Accepted**
Date: 2026-09-24
Related: AWS hackathon-final pass, phase 5 (observability),
`backend/app/metrics.py`, `backend/tests/test_metrics.py`,
`infrastructure/template.yaml`'s `EventBrokerDegradedAlarm`/
`BedrockThrottledAlarm`

## Context

Phase 5 required real, queryable CloudWatch custom metrics for
application-level signals the built-in `AWS/*` namespaces cannot see:
Bedrock invocation latency/throttling/fallback, simulator lease-ownership
changes, stale-leader write rejections, pairing outcomes, and
event-broker degradation. Two implementation paths exist:

1. Call `cloudwatch:PutMetricData` directly from the backend on each
   event.
2. Emit a structured JSON log line in CloudWatch's Embedded Metric
   Format (EMF) to stdout, and let the platform auto-extract it into a
   real CloudWatch Metric.

## Decision

Use EMF (`backend/app/metrics.py`'s `emit_metric`), not direct
`PutMetricData` calls.

## Rationale

- **No new IAM permission.** The backend already ships every log line to
  CloudWatch Logs via the `awslogs` driver (`infrastructure/template.yaml`).
  An EMF-shaped line is automatically extracted into a real CloudWatch
  Metric by the platform itself — `cloudwatch:PutMetricData` is never
  called by this application's own code, so `BackendTaskRole` needs no
  new grant at all for this feature.
- **No new failure mode on the request path.** A `PutMetricData` call is
  a real network call that can itself fail, throttle, or add latency to
  the request it's instrumenting, and would need its own retry/error
  handling to avoid becoming a new source of request-path failures.
  `emit_metric` is a local `print()` to stdout wrapped in a blanket
  `try/except` that never raises — a metrics emission failure can never
  break the request it's describing (see that function's own docstring).
- **Reuses infrastructure that already exists.** No new ingestion
  pipeline, no CloudWatch agent, no sidecar beyond what's already
  provisioned for logs.
- **Precedent.** This is the same pattern AWS's own Lambda Powertools and
  the CDK's metrics guidance recommend for exactly this reason — not a
  novel approach invented for this codebase.

## Consequences

- Alarms that read these metrics (`EventBrokerDegradedAlarm`,
  `BedrockThrottledAlarm`) must reference fixed dimension values (e.g.
  `Reason=publish_failed`, `Source=bedrock`) because EMF only creates the
  metric/dimension combination actually emitted — there is no automatic
  "roll-up across all dimension values" metric the way a `PutMetricData`
  call without dimensions would produce. Metrics with a genuinely dynamic
  dimension (`RaceId` on `SimulatorLeaseOwnershipChanged`/
  `StaleLeaderWriteRejected`) are therefore surfaced on the operational
  dashboard via a `SEARCH()` expression rather than a static alarm — see
  `template.yaml`'s own comment on `EmfMetricAlarms` for the full
  reasoning.
- Coverage is a representative subset, not the full metric list phase 5's
  instruction named (API latency, WebSocket connection counts, DynamoDB
  throttling, ECS task health) — several of those are already available
  for free from AWS's own built-in metrics without any application code,
  so this module exists only for what the application itself can
  observe. See `app/metrics.py`'s own module docstring for the exact
  list of what is and is not covered.
- Validated with `cfn-lint`/`sam validate --lint` and
  `backend/tests/test_metrics.py`'s unit tests (asserting the exact EMF
  JSON shape) only — not against a real CloudWatch console, since this
  environment has no AWS credentials.
