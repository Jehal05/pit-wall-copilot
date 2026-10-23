# Infrastructure Notes (Gate 0, extended in Gate 2 and spec v1.1 repair 12)

Gate 0 introduced no Infrastructure-as-Code automation. `../infrastructure/template.yaml`
(spec v1.1 repair 12) now provisions a real, best-effort AWS deployment —
DynamoDB + Streams, a Watch Evaluator Lambda, the legacy Alexa skill
Lambda, ECS Fargate services for the backend and `mcp_server`, WAF rate
limiting, Secrets Manager, CloudWatch alarms and X-Ray tracing — see that
file's own module-docstring "HONEST STATUS" note and
`../docs/deployment-runbook.md` for exact commands and what has/hasn't
been verified in this environment (no AWS account is available here).
None of this is required for local development: the backend defaults to
the in-memory repositories (`STATE_REPOSITORY=memory`) and requires no AWS
resources at all for local development or the automated test suite.

If you want to exercise `STATE_REPOSITORY=dynamodb` against a real table,
create it yourself (console, AWS CLI, or your own IaC) using the schema
below. This one table now backs race state (`backend/app/repositories/dynamodb.py`)
**and** watches/alerts (`backend/app/repositories/watch_dynamodb.py`,
added in the Gate 2 fix that gave watches/alerts a real DynamoDB path) —
item type is distinguished entirely by the sort key, so no second table
needs provisioning.

## Table: `pit-wall-race-state` (name configurable via `DYNAMODB_TABLE_NAME`)

| Attribute | Type | Role |
|---|---|---|
| `pk` | String | Partition key, always `RACE#<race_id>` |
| `sk` | String | Sort key — see item types below |
| `race_id` | String | Convenience attribute, mirrors the race ID |
| `run_id` | String | Current run identifier (STATE/CONTROL/WATCH items only) |
| `state_version` | Number | Monotonic version (STATE and ALERT items) |
| `watch_id` / `alert_id` | String | Convenience attributes on WATCH/ALERT items |
| `data` | String | The full domain object, JSON-encoded |

### Item types (all share the one table, distinguished by `sk`)

| `sk` | Item | Written by |
|---|---|---|
| `STATE` | Current `RaceState` | `DynamoDBRaceStateRepository` |
| `CONTROL` | Current `DemoControlState` | `DynamoDBRaceStateRepository` |
| `WATCH#<watch_id>` | A `Watch` | `DynamoDBWatchRepository` |
| `ALERT#<alert_id>` | An `Alert` | `DynamoDBAlertRepository` |
| `ALERTIDX#<idempotency_key>` | `{alert_id}` — enforces spec 49's exactly-once alert write via a conditional put | `DynamoDBAlertRepository` |

`WATCH#`/`ALERT#`/`ALERTIDX#` are chosen so `sk.begins_with("ALERT#")`
(used to list all alerts for a race) never accidentally matches an
`ALERTIDX#` item — they diverge at the 6th character.

### Device-pairing item types (spec v1.1 pass 4, mandatory correction 1)

Pairing codes and device tokens (`backend/app/repositories/pairing_dynamodb.py`)
share the SAME table, but under a dedicated, non-race-scoped partition
key (`pk="AUTH"`) since they are not associated with any single race:

| `pk` | `sk` | Item | Written by |
|---|---|---|---|
| `AUTH` | `PAIRCODE#<sha256 of code>` | `{code_hash, created_at, expires_at, used, ttl}` | `DynamoDBPairingCodeRepository` |
| `AUTH` | `DEVICETOKEN#<sha256 of token>` | `{device_id, token_hash, device_name, created_at, expires_at, revoked, ttl}` | `DynamoDBDeviceTokenRepository` |
| `AUTH` | `DEVICEIDX#<device_id>` | `{device_id, token_hash}` — lets an admin revoke by `device_id` without a table scan | `DynamoDBDeviceTokenRepository` |
| `AUTH` | `RATELIMIT#<client key>#<window start>` | `{attempt_count, ttl}` — distributed pairing-attempt rate limit | `DynamoDBPairingRateLimiter` |

**Only a SHA-256 hash of a pairing code or device token is ever stored —
never the plaintext value.** `ttl` is set to the same epoch-seconds value
as `expires_at` on every pairing/device-token/rate-limit item and is
DynamoDB's TTL attribute (see `TimeToLiveSpecification` in
`template.yaml`) — a storage-cost cleanup only; every read re-validates
`expires_at`/`used`/`revoked` in application code first, so correctness
never depends on how quickly DynamoDB's background TTL sweep physically
deletes an expired item.

Consuming a pairing code, revoking a device token, and incrementing the
rate-limit counter are all implemented as a single conditional
`UpdateItem` call each (`ConditionExpression` on `used`/`revoked`/
`attempt_count`), which is what makes them safe under concurrent requests
across multiple backend tasks with no shared process memory — see
`pairing_dynamodb.py`'s module docstring for the full reasoning, and
`backend/tests/test_pairing_dynamodb.py` for the tests exercising restart
persistence, multi-instance agreement, concurrent code exchange, token
expiry, and independent revocation against a real (moto-simulated)
DynamoDB table.

Billing mode: on-demand (`PAY_PER_REQUEST`) is sufficient for demo traffic.

### AWS CLI example

```powershell
aws dynamodb create-table `
  --table-name pit-wall-race-state `
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S `
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE `
  --billing-mode PAY_PER_REQUEST
```

### Version-aware writes

`save_state` uses a DynamoDB `ConditionExpression` so a write is only
accepted when no item exists yet, the write belongs to a new `run_id`, or
its `state_version` is strictly greater than what is already stored. A
stale or duplicate write raises `StaleWriteError` in the application rather
than silently overwriting newer state. This is exercised in
`backend/tests/test_dynamodb_repository.py` using `moto` (a mocked AWS
environment) — no real AWS account or credentials are needed to run that
test.

### Idempotent alert writes

`DynamoDBAlertRepository.record_transition` uses a `ConditionExpression`
of `attribute_not_exists(pk)` on the `ALERTIDX#<idempotency_key>` item, so
a duplicate/replayed transition (spec 49) is rejected at the storage layer
itself rather than relying on the caller to check first — the same
conditional-write-for-correctness approach `save_state` above already
uses, applied to a different guarantee. Exercised in
`backend/tests/test_watch_dynamodb_repository.py` using `moto`, including
a test that drives the real `WatchEvaluator` end-to-end against the
DynamoDB-backed repositories and a test that calls
`AlertRepository.record_transition` directly with a duplicated
`idempotency_key`.
