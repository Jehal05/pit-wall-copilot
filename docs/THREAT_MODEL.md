# Threat model

AWS hackathon-final pass, phase 9. A lightweight STRIDE-style pass over
the architecture described in `AWS_ARCHITECTURE.md`, covering what's
actually implemented (see `SECURITY.md` for the controls themselves).
This is scoped to the system as built, not a generic checklist — each
entry names the concrete asset, the concrete threat, and the concrete
mitigation already in the codebase, or the concrete gap.

## Assets

1. **Demo access tokens / device tokens** — the sole credential
   protecting every mutating endpoint and the WebSocket route.
2. **Pairing codes** — short-lived, single-use, exchanged for a device
   token; the bridge between "a new device wants in" and "a new device
   has a credential."
3. **Race state / watch / alert data in DynamoDB** — not sensitive on
   its own (synthetic race telemetry), but its *integrity* matters: a
   judge or user must be able to trust the numbers.
4. **Secrets Manager secrets** (the demo token itself, Bedrock
   configuration where applicable).
5. **The deterministic strategy engine's output** — the thing every
   other guarantee in this system (grounding, no-hallucination) is
   built on top of.

## Trust boundaries

```
[Fire TV app / Alexa+ platform / legacy Alexa skill]   <- untrusted network
        |
     WAF (rate limiting)
        |
     ALB (TLS termination)
        |
   [ECS backend tasks]  <-- trust boundary: only past this point is a
        |                    request considered "inside" the system
   [DynamoDB, Secrets Manager, Bedrock, KMS]  <- AWS-managed, IAM-gated
```

Everything left of the ALB is untrusted input. Everything right of the
ECS tasks is only reachable with IAM credentials this stack itself
issues, scoped per-role (see `SECURITY.md`'s "Least-privilege IAM").

## Threats and mitigations, by category

**Spoofing (impersonating a device or the master operator)**
- Threat: an attacker guesses or brute-forces a pairing code or a device
  token to impersonate a paired device.
- Mitigation: pairing codes are short-lived and single-use (atomic
  consume-or-fail, phase 3); token comparison is constant-time; a
  distributed rate limiter plus WAF rules bound guess velocity; three
  CloudWatch alarms watch for the resulting traffic signature.
- Gap: rate limiting slows, but does not architecturally prevent, a
  low-and-slow distributed brute-force below alarm thresholds — this is
  a defense-in-depth posture, not a claim of unconditional prevention.

**Tampering (with race data, or with a device's stored credential)**
- Threat: an attacker with network access between the ALB and an ECS
  task tampers with a request or response in transit.
- Mitigation: TLS end-to-end by default (deploy-time Rule refuses
  plaintext unless explicitly opted into for local demo use); the Fire
  TV client independently refuses plaintext in release builds.
- Threat: a stale simulator process (e.g. one that wins a lease, pauses
  past its lease TTL, then resumes) writes outdated race state over a
  newer leader's writes.
- Mitigation: persistence-level, lease-proof fencing (phase 2, hardened
  in a later correction pass) — every fenced write is a single DynamoDB
  `TransactWriteItems` call that includes a `ConditionCheck` against the
  live LEASE item itself (exact owner, exact fencing token, not expired),
  not just a monotonic floor compared against write history. This closes
  the stale-leader window a floor-only design leaves open: a superseded
  leader is rejected even if the new leader hasn't written yet. Production
  DynamoDB mode fails closed if no lease proof is supplied at all. This
  lease-fenced path is used only for leader-owned writes
  (`save_state_from_simulator()`, `save_control_from_simulator()`);
  operator commands (`/start`, `/pause`) go through a separate
  `transition_control_from_operator()` method with no lease requirement
  by design, so they keep working no matter which ECS task currently
  holds leadership, fenced instead by their own atomic
  `control_revision` counter.
- Threat: a valid, still-current leader (its lease is neither stale nor
  superseded) overwrites an operator's pause anyway, because after
  committing race state it re-reads the control record but then ignores
  what it read. This is a stale-**read** problem, not a stale-**leader**
  problem, so the lease-fencing mitigation above does not catch it.
- Mitigation (third repair pass): `SimulatorService._advance()` now
  preserves whatever lifecycle status is actually stored, only ever
  transitioning RUNNING->RUNNING or RUNNING->COMPLETED; as
  defense-in-depth, `save_control_from_simulator` (both
  `repositories/memory.py` and `repositories/dynamodb.py`) additionally
  fences its write on the stored `control_revision` matching what the
  write was built from, atomically, so a write built from a stale
  pre-pause read is rejected even if the service-level fix is bypassed.
- Threat: `control_revision` itself drifts out of sync between
  DynamoDB's top-level attribute and the serialized JSON blob, since the
  attribute was computed via an atomic expression while the blob was
  built from the caller's own stale pre-write read.
- Mitigation (third repair pass): `transition_control_from_operator` is
  now a genuine compare-and-set with a bounded retry loop — read the
  current revision, compute `next_revision` once in Python, write that
  same literal value to the attribute, the blob, and the returned
  object, conditioned on the stored revision still matching; a losing
  concurrent call retries against a fresh read rather than erroring.
  `get_control()` now always uses `ConsistentRead=True` in DynamoDB mode.
- Threat: a `LeaseProof` legitimately acquired for one race is reused to
  authorize a write to a *different* race (the persistence layer
  previously only checked a proof against itself, never against the
  write's actual target race).
- Mitigation (repair pass): every lease-fenced write in both
  `repositories/memory.py` and `repositories/dynamodb.py` now explicitly
  rejects a proof whose race ID or canonical lease name (see
  `simulator_lease_name()` in `repositories/lease.py`) doesn't match the
  write's real target race; `try_acquire_or_renew` also refuses to let an
  existing lease name silently rebind to a different race. Covered by 22
  parametrized tests in `backend/tests/test_cross_race_lease_fencing.py`.
- Threat: a device's locally-stored token is tampered with or extracted.
  Mitigation: `secureTokenStore` (Fire TV) uses platform-appropriate
  secure storage rather than plain `AsyncStorage`; out of scope beyond
  that (device-level compromise is not this application's threat
  surface).

**Repudiation (denying an action happened)**
- Threat: no record of who paired a device, revoked one, or triggered an
  authentication failure.
- Mitigation: every such event is a structured, timestamped audit-log
  line (`audit_log`) with a `correlation_id`, queryable via CloudWatch
  Logs Insights.
- Gap: audit logs are not shipped to a separate, tamper-evident sink
  (e.g. a write-once log destination) — an attacker with sufficient IAM
  access to the log group itself could alter history. Scoped out as
  disproportionate for a hackathon submission's threat model; flagged
  here rather than silently assumed away.

**Information disclosure (secrets, or unauthorized read of race data)**
- Threat: a secret leaks via the CloudFormation template, a log line, or
  an error message.
- Mitigation: secrets are never template/parameter values; the audit-log
  guard (`AuditFieldViolation`) raises before logging any
  secret-shaped field name rather than best-effort redacting; DynamoDB
  encryption at rest via a customer-managed KMS key.
- Threat: an unauthenticated client reads race/watch/alert data.
  Mitigation: every read endpoint (not just mutating ones) requires a
  valid token — see `require_demo_access_token`'s use on GET routes, not
  only POST/PUT.
- Threat: the correlation ID or another response header leaks internal
  state.
  Mitigation: `correlation_id` is sanitized to a strict charset before
  being echoed, closing a header-injection/information-leak vector fixed
  in an earlier pass (see `JUDGING_EVIDENCE.md` (removed in the repo cleanup; see git history) §4).

**Denial of service**
- Threat: a flood of pairing attempts or authentication failures exhausts
  backend capacity or DynamoDB throughput.
  Mitigation: WAF rate limiting (edge), the distributed
  `PairingRateLimiter` (application layer), DynamoDB on-demand capacity
  with its own throttling alarm.
- Threat: a synchronous, blocking Bedrock call on the request thread
  starves the event loop under concurrent load.
  Mitigation: `run_in_threadpool` (fixed in an earlier pass — see
  `JUDGING_EVIDENCE.md` (removed in the repo cleanup; see git history) §4) keeps the LLM call off the async request
  thread.
- Threat: an unbounded pairing-code collision retry loop becomes its own
  DoS vector against DynamoDB.
  Mitigation: `DEVICE_EXCHANGE_MAX_ATTEMPTS` bounds the retry budget
  (phase 3).
- Threat: an oversized request body exhausts memory.
  Mitigation: a plain-ASGI request-body size limit (ADR 0003).

**Elevation of privilege**
- Threat: a paired-device token is used to mint new pairing codes or
  revoke other devices (an ordinary device escalating to
  operator-level control).
  Mitigation: those specific actions require
  `require_master_demo_access_token` — a device token is explicitly
  insufficient, checked separately from the general
  `require_demo_access_token` gate.
- Threat: the MCP server (which Alexa+ calls into) is granted more AWS
  access than it needs.
  Mitigation: `McpServerService` has no DynamoDB IAM permissions at all
  — it only calls the backend's own authenticated REST API, so a
  compromise of the MCP server does not directly expose the data layer.

## What this threat model deliberately does not cover

- Compromise of the underlying AWS account itself (out of this
  application's control).
- Physical device security for a paired Fire TV or a user's Alexa+
  device.
- Supply-chain compromise of a third-party dependency before it reaches
  `npm audit`/`pip-audit` — those tools catch *known, disclosed*
  vulnerabilities, not a malicious package that hasn't been reported
  yet.
- Anything requiring a real AWS deployment to observe (e.g. actual WAF
  rule effectiveness under real attack traffic) — this threat model
  describes what the code and template are built to do, which has been
  validated statically and via the local test suites, not what has been
  observed under a real attack. See `SECURITY.md`'s closing section.
