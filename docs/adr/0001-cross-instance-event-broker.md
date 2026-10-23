# ADR 0001: Cross-Instance WebSocket Event Broker

Status: **Accepted**
Date: 2026-09-23
Related: P0-3 (AWS hackathon-final pass), `app/services/event_broker.py`,
`app/services/event_broadcaster.py`, `infrastructure/template.yaml`,
`backend/tests/test_event_broker.py`

## Context

`RaceEventBroadcaster` fans out `RaceEvent`s (race state, resets, strategy
updates, watch alerts, agent responses, staleness signals) to WebSocket
clients connected to `/v1/races/{race_id}/ws`. Before this decision, it
only ever tracked connections held by its own process, in an in-memory
`dict[str, set[WebSocket]]`.

`infrastructure/template.yaml`'s `BackendService` runs on ECS/Fargate and
is explicitly designed to support `DesiredCount > 1` — the app layer
already shares all durable state (race state, watches, alerts, pairing)
through DynamoDB so any task can serve any request. WebSocket delivery was
the one remaining piece that was NOT safe to scale: with two tasks behind
the ALB, a client connected to task A would never see an event produced on
task B. This is silent data loss, not a crash — exactly the kind of defect
that looks fine in a single-task demo and breaks the moment the stack is
scaled, which made it a P0 production blocker.

Sticky ALB sessions were considered and explicitly rejected as "a complete
fix": they only hold as long as a task is never replaced, scaled up,
scaled down, or rebalanced across AZs — normal, expected ECS behavior, not
an edge case. A client that stays connected across a task replacement
would silently start missing every event from that point on.

## Decision

Introduce an `EventBroker` abstraction (`app/services/event_broker.py`)
with two implementations, selected by the `EVENT_BROKER` setting
(`memory` default, `redis` for cross-instance delivery):

- **`InMemoryEventBroker`** — the exact pre-existing behavior, wrapped
  behind the new interface. Used for local development and the existing
  single-process test suite; zero new external dependency.
- **`RedisEventBroker`** — publishes every event to a Redis/Valkey pub/sub
  channel (`pitwall:events:{race_id}`) backed by **Amazon ElastiCache
  Serverless** (engine: Valkey) in the deployed stack, and subscribes to
  the same channel so every task, regardless of which task produced the
  event, can deliver it to its own locally-connected clients.

`RaceEventBroadcaster` is unchanged in its public surface — every existing
call site (`app/main.py`'s `_on_state_committed`, the strategy/agent REST
handlers in `app/api/routes.py`) still just calls `broadcast(race_id,
event)`. Internally, `broadcast` now delegates to the attached broker via
`attach_broker`, and a new `deliver_local` method (the old `broadcast`
body, renamed) is what the broker calls back into.

### Why Redis/Valkey pub/sub, not SNS/SQS or a DynamoDB Streams fan-out

- **Latency**: pub/sub delivery needs to complete inside a race tick
  (roughly ~1s cadence); SQS/SNS round-trips and DynamoDB Streams'
  batching latency are both a poor fit for "deliver this to open
  WebSocket connections right now."
- **Simplicity of the broker interface**: a channel-per-race_id topic
  model maps directly onto the existing `race_id`-keyed connection
  tracking, with no additional infrastructure to reason about (no queue
  depth, no visibility timeout, no DLQ semantics needed for this
  fire-and-forget delivery problem — a missed live event is superseded by
  the next tick's `RACE_STATE` broadcast, and the REST polling fallback
  documented in `event_broadcaster.py`'s module docstring already exists
  for exactly this class of gap).
- **Serverless**: ElastiCache Serverless has no cluster/node sizing to
  manage, scales automatically, and is billed only for the cache usage
  actually consumed — appropriate for a bursty, demo-scale workload.

### Self-echo deduplication

Redis pub/sub delivers a published message back to every subscriber on
that channel, including the publisher itself (every `RedisEventBroker`
instance is always both a publisher and a subscriber). Without
correction, this would double-deliver every event to the producing
instance's own local clients: once via the direct local-handler call in
`publish()`, once again via the subscriber loop receiving its own
message. Each `RedisEventBroker` tags outgoing messages with a random
per-process `origin_instance_id` and drops any inbound message whose
origin matches its own id. This exact failure mode was caught by
`tests/test_event_broker.py::TestTwoInstanceFanOut::test_event_ids_are_not_duplicated_when_the_producing_instance_also_has_a_local_client`
during development of this ADR's implementation, before the fix — not a
hypothetical.

## Failure behavior

- **Publish with no connected broker client** (startup race, or a
  publish issued before the subscriber loop has finished connecting):
  the event is still delivered to the producing instance's own local
  clients directly; cross-instance fan-out is skipped for that one event,
  `metrics.degraded_events` is incremented, and a structured
  `event_broker_degraded` audit-log line is emitted. Delivery is never
  silently claimed to have succeeded when it did not fan out.
- **Connection lost mid-session**: the subscriber loop reconnects with
  capped exponential backoff (0.5s → 10s), logging a warning and an
  `event_broker_degraded` audit event on every drop. Local delivery for
  the producing instance is unaffected throughout.
- **Malformed message on the wire**: discarded with a warning; never
  crashes the listener loop.
- **Redis/ElastiCache entirely unavailable at startup**: `build_event_broker`
  still constructs a `RedisEventBroker` (it does not probe connectivity
  synchronously at startup — matching this codebase's existing "never let
  an external dependency block ASGI startup" pattern for the LLM client);
  the first connection attempt happens in the background listener task,
  with the same reconnect/backoff behavior as a mid-session drop.

## Testing

- `tests/test_event_broker.py::TestTwoInstanceFanOut` — the two-instance
  proof this ADR exists to satisfy: two `RedisEventBroker`s share one
  `fakeredis` in-memory server (standing in for two ECS tasks pointed at
  the same ElastiCache endpoint); a client connected only to "instance A"
  receives an event produced only through "instance B"; a second test
  proves no duplication when the producing instance also has its own
  local client; ordering across two sequential events is asserted
  explicitly.
- `TestRedisEventBrokerDegradation` — proves degraded delivery is
  reported, never silently swallowed.
- The full existing single-process test suite (`InMemoryEventBroker`,
  wired transparently) passes unchanged — this decision introduces no
  regression for `EVENT_BROKER=memory` (the default).

## Consequences

- A real multi-task deployment MUST set
  `EnableCrossInstanceEventBroker=true` (provisions `EventBrokerCache` in
  `infrastructure/template.yaml`) before raising `BackendService`'s
  `DesiredCount` above 1. This is deliberately an explicit opt-in, not
  inferred from `DesiredCount`, so a template diff always shows the
  decision being made.
- Adds one new AWS resource class (ElastiCache Serverless) and one new
  runtime dependency (`redis` — `requirements.txt`) to the production
  dependency surface, only when actually enabled.
- Estimated hackathon/demo cost: **$0** when `EnableCrossInstanceEventBroker=false`
  (this template's own default, matching `DesiredCount: 1`). When enabled,
  ElastiCache Serverless is billed per-GB-hour of data stored and per
  ECPU consumed; at this application's demo-scale event volume (a handful
  of small JSON messages per second, across four cars, for the duration of
  a live demo), the cost is a small fraction of a dollar per demo session.
- Estimated small-production cost: still governed by the same
  `CacheUsageLimits` ceiling set in `infrastructure/template.yaml`
  (1 GB storage / 5000 ECPU/s), which bounds worst-case spend; a real
  higher-traffic deployment would raise this ceiling based on observed
  CloudWatch `ElastiCache` metrics rather than guessing up front.
