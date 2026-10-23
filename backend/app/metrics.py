"""CloudWatch custom metrics via Embedded Metric Format (EMF) — AWS
hackathon-final pass, phase 5.

*** HONEST STATUS ***
This covers a representative subset of the metrics phase 5's own
requirement list names, wired into the call sites judged highest-value
for actually diagnosing a production incident in the time available for
this pass: Bedrock latency/throttling/fallback, simulator lease-ownership
changes, stale-leader write rejections, pairing success/failure, and
event-broker degradation (the last of which already had audit-log
coverage from an earlier pass — this adds a matching metric). It does
NOT yet cover every metric phase 5's instruction names (API latency/
errors, strategy-calculation latency, WebSocket connection counts,
DynamoDB throttling, ECS task health) — those are listed as remaining
work in EXTERNAL_RELEASE_CHECKLIST.md / OPERATIONS_RUNBOOK.md rather than
silently left unmentioned. Several of the un-covered ones (ECS task
health, DynamoDB throttling, ALB/target-group latency) are also already
available for free from AWS's own built-in CloudWatch metrics
(AWS/ECS, AWS/DynamoDB, AWS/ApplicationELB namespaces) without any
application code — this module exists for the metrics ONLY the
application itself can observe.

Why EMF instead of calling PutMetricData directly: this backend already
ships every log line to CloudWatch Logs via the `awslogs` driver
(infrastructure/template.yaml) with NO additional IAM permission beyond
what logging already requires. A log line in the EMF JSON shape is
AUTOMATICALLY extracted into a real CloudWatch custom metric by the
platform — no `cloudwatch:PutMetricData` IAM grant, no extra network
call, no risk of a metrics call itself failing/throttling and needing its
own error handling. This is the same pattern AWS's own Lambda Powertools
and the CDK's own metrics guidance recommend for exactly this reason.

Emitting a metric NEVER raises — a metrics call is diagnostic, not
load-bearing; a malformed metric line would just fail to be extracted by
CloudWatch, not crash the request.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

_NAMESPACE = "PitWallCopilot"


def emit_metric(
    metric_name: str,
    value: float,
    *,
    unit: str = "None",
    dimensions: Optional[dict[str, str]] = None,
) -> None:
    """Writes one EMF-shaped JSON log line to stdout (captured by the
    `awslogs` log driver exactly like every other structured log line
    this application already emits — see app/observability.py's
    `audit_log`). `unit` must be a CloudWatch-recognized unit string
    ("Milliseconds", "Count", "None", ...); an invalid unit is silently
    dropped by CloudWatch's EMF processor rather than raising here, so
    callers get a metric with no explicit unit rather than a crashed
    request.
    """
    try:
        dims = dimensions or {}
        payload = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": _NAMESPACE,
                        "Dimensions": [list(dims.keys())] if dims else [[]],
                        "Metrics": [{"Name": metric_name, "Unit": unit}],
                    }
                ],
            },
            metric_name: value,
            **dims,
        }
        print(json.dumps(payload), file=sys.stdout, flush=True)  # noqa: T201 - EMF must go to stdout, not the logging module
    except Exception:  # noqa: BLE001 - a metrics emission failure must never break the caller
        logger.warning("emit_metric failed for %s", metric_name, exc_info=True)


def emit_bedrock_invocation(*, latency_ms: float, throttled: bool, fallback_used: bool, source: str) -> None:
    """Phase 5: "Bedrock latency/throttling/fallback." `source` is the
    same "template"/"bedrock" value already surfaced to judges (see
    app/services/llm_client.py) — included as a dimension so the fallback
    rate is queryable directly (fraction of invocations where
    source="template" despite LLM_PROVIDER=bedrock being configured)."""
    emit_metric("BedrockInvocationLatency", latency_ms, unit="Milliseconds", dimensions={"Source": source})
    emit_metric("BedrockThrottled", 1.0 if throttled else 0.0, unit="Count", dimensions={"Source": source})
    emit_metric("BedrockFallbackUsed", 1.0 if fallback_used else 0.0, unit="Count", dimensions={"Source": source})


def emit_simulator_lease_ownership_changed(*, race_id: str) -> None:
    """Phase 5: "simulator lease ownership changes" — emitted whenever
    `try_acquire_or_renew` (app/repositories/lease.py) grants the lease to
    an owner DIFFERENT from who held it last tick, i.e. an actual
    takeover (crash recovery, or an initial acquire), not routine
    same-owner renewal."""
    emit_metric("SimulatorLeaseOwnershipChanged", 1.0, unit="Count", dimensions={"RaceId": race_id})


def emit_stale_leader_write_rejected(*, race_id: str) -> None:
    """Phase 5: "stale-leader write rejection" — emitted whenever
    SimulatorService's persistence-level fencing (phase 2) rejects a
    write because its fencing token has been superseded."""
    emit_metric("StaleLeaderWriteRejected", 1.0, unit="Count", dimensions={"RaceId": race_id})


def emit_pairing_outcome(*, outcome: str) -> None:
    """Phase 5: "pairing success/failure/throttling." `outcome` is one of
    "success", "code_rejected", "collision_retry", "rate_limited",
    "exchange_exhausted" — never the code/token itself (see
    app/observability.py's `audit_log` forbidden-field-name guard for the
    same no-secrets-in-telemetry rule applied there)."""
    emit_metric("PairingOutcome", 1.0, unit="Count", dimensions={"Outcome": outcome})


def emit_event_broker_degraded(*, reason: str) -> None:
    """Phase 5: matches the existing `audit_log("event_broker_degraded", ...)`
    call sites in app/services/event_broker.py — this is the metric
    counterpart of that same signal, queryable/alarmable in CloudWatch
    Metrics rather than only greppable in CloudWatch Logs."""
    emit_metric("EventBrokerDegraded", 1.0, unit="Count", dimensions={"Reason": reason})
