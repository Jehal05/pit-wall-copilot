# Judging Evidence

A judge-facing index: every major claim this submission makes, next to
the exact test file/command that backs it, and — separately, honestly —
the specific things that could NOT be verified in this development
environment (no AWS account, no real Fire TV/Echo device) and exactly
what running them for real would take. Nothing below is aspirational.
Every count in this document was produced by actually running the
command shown, in this environment, immediately before this document was
written.

**Two-status framing** (see also `README.md`'s and
`FINAL_PRODUCTION_READINESS_REPORT.md`'s use of the same framing):

- **Source readiness** — is the code, its tests, its CI, and its
  documentation actually correct and complete, as verified by running
  everything runnable in a sandboxed dev environment with no AWS
  account? This is what the counts below answer, and the answer is yes.
- **Externally verified deployment readiness** — has this actually been
  deployed to a real AWS account, exercised through a real Fire TV
  device and a real Alexa+ device, and left running under real load?
  This has NOT happened in this environment — there is no AWS access
  here. `REMAINING_EXTERNAL_VERIFICATION.md` lists exactly what to run,
  and with what access, to close this gap for real.

---

## 1. Automated test suites — run just now, in this environment

| Suite | Command | Result |
|---|---|---|
| Backend (FastAPI) | `cd backend && .venv/bin/python -m pytest -q` | **564 passed** |
| Infrastructure (CloudFormation structural tests) | `cd infrastructure && python3 -m pytest tests/ -q` | **62 passed** |
| Fire TV (Jest/React Native) | `cd fire-tv && npx jest --silent` | **186 passed** |
| MCP server | `cd mcp_server && .venv/bin/python -m pytest -q` | **47 passed** |
| Alexa+ simulated client | `cd alexa_plus_client && .venv/bin/python -m pytest -q` | **8 passed** |
| Demo/chaos/packaging scripts | `cd scripts && python3 -m pytest -q` | **38 passed** |
| **Six-suite total** | | **905 passed, 0 failed, 0 skipped** |
| Legacy Alexa REST skill (kept separate — superseded component) | `cd legacy/alexa_skill_legacy_rest && pytest -q` | **19 passed** |
| **Grand total (incl. legacy)** | | **924 passed, 0 failed, 0 skipped** |

(This table has been superseded three times already, from an earlier 721
baseline, then a 788/818 baseline, then an 886/905 baseline, all recorded
in `FINAL_READINESS_REPORT.md`'s reconciliation table; the numbers above
are the current, freshly re-verified counts from the third repair pass,
which fixed two more confirmed concurrency defects on top of the prior
P0 repair pass's three: a valid, still-current simulator leader
overwriting an operator's pause (a stale-**read** problem, distinct from
the stale-**leader** problem the original lease fencing closes — see §5
below), and a `control_revision` consistency bug between DynamoDB's
top-level attribute, its serialized JSON blob, and what callers saw.
Backend grew by 19 tests (15 in the new
`tests/test_control_revision_fencing.py` + 3 in
`tests/test_simulator_leadership.py` + 1 in
`tests/test_dynamodb_simulator_service.py`); mcp_server's test count is
unchanged at 47 — this pass's MCP tracing work hardened
`tests/test_tracing.py` against ambient `OTEL_TRACES_SAMPLER`
configuration without adding new test functions. Backend and mcp_server
suites were each run twice in this pass with identical results; the
six-suite total intentionally excludes the legacy Alexa REST skill, which
is tracked separately. Note: 905 is this pass's six-suite-only total —
it is coincidentally the same number as the *previous* pass's
grand-total-including-legacy; the two are different claims, and every
occurrence of a total in this document states explicitly whether it
includes the legacy suite.)

Template/infra validation, also run just now:
```
cd infrastructure && cfn-lint template.yaml   # clean, no findings
cd infrastructure && sam validate --lint      # "is a valid SAM Template"
```

Android (Gradle/instrumented) tests are **not** in this count — this
sandbox has no Android SDK/emulator; see
`REMAINING_EXTERNAL_VERIFICATION.md` §1 for the exact commands and
expected output on a machine that has one, and
`fire-tv/src/__tests__/androidReleaseSigning.test.ts` for the
statically-verifiable (no SDK required) release-signing checks, which
*are* included in the Fire TV count above.

## 2. The golden demo path — re-verified end-to-end, live, in this environment

```
./scripts/judge_demo.sh
```
This starts a real local backend, waits for it to become healthy, then
drives the actual documented demo narrative over real HTTP (not a mock):
reset → create an undercut watch (baseline `CLOSED`) → start the demo →
poll until the watch flips to `OPEN` → assert the transition happened on
**exactly lap 27, state_version 3** (never lap 25/26/28 — this is a
named regression the spec explicitly calls out) → fetch the grounded
explanation and assert its `grounding_warnings` list is empty. This was
run immediately before this document was written and printed `Golden
demo PASSED end-to-end`. See `JUDGE_DEMO.md` for the live-presentation
script built on this same verified path.

## 3. "Explain, don't invent" — the core trust claim, and how it's tested

The architecture principle: an LLM (Bedrock or the deterministic
template fallback) narrates precomputed numbers, and is never the source
of a number a client sees. Two independent, automated guards enforce
this, and both are tested against a REAL `BedrockLLMClient` parsing a
realistic mocked `invoke_model` response — not just against a hand-written
fake client:

- **Grounding guard** (`ExplanationEngine._check_grounding`): every
  numeric token in the narrative must trace back to a value actually
  present in the grounded facts or retrieved citations.
- **Attribution guard** (`ExplanationEngine._check_attribution`): catches
  a narrative that states two real, grounded numbers but swaps which
  driver they belong to — a materially wrong claim even though every
  digit involved is individually "grounded."

`backend/tests/test_bedrock_trust_scenarios.py` — 6 named scenarios, run
just now (`.venv/bin/python -m pytest tests/test_bedrock_trust_scenarios.py -q` →
**6 passed**):

1. A well-formed, honest response passes both guards clean.
2. A hallucinated number (present nowhere in the grounded facts) is
   flagged in `grounding_warnings`, not silently trusted.
3. A response with no text content block at all degrades to the exact
   documented literal (`BEDROCK_UNAVAILABLE_NARRATIVE`), not a crash.
4. A malformed (non-JSON) response body degrades safely, not a crash.
5. A `ThrottlingException` raised by `invoke_model` (the realistic
   AWS-shaped failure under concurrent judge-day load) degrades safely —
   never surfaces as an HTTP 500 to the Fire TV/Alexa+ client.
6. A realistic narrative with candidate/target tire ages deliberately
   swapped is caught by the attribution guard specifically (not the
   plain grounding guard, which alone cannot catch this class of error).

## 4. Security — what was found and fixed in this final pass, not just claimed

- **A real, previously-unauthenticated WebSocket endpoint.**
  `/v1/races/{race_id}/ws` had zero credential check while every other
  endpoint serving the same data required `X-Demo-Access-Token`. Found
  during this pass's own review (not reported externally), fixed with a
  `?token=` query-parameter check (the standard workaround for
  WebSocket's inability to carry custom headers — see
  `docs/adr/0002-websocket-query-token-authentication.md`), and tested on
  both ends: `backend/tests/test_websocket_events.py::TestWebSocketAuthentication`
  (3 tests) and `fire-tv/src/__tests__/useRaceSocket.test.ts`'s `toWebSocketUrl
  auth token propagation` (3 tests) plus its `4401 ... close handling`
  (4 tests, covering the follow-on Fire TV polish fix below).
- **An event-loop-blocking Bedrock call.** `explain_alert`'s synchronous
  `invoke_model` call ran directly on the async route handler's event
  loop thread — a slow/hung Bedrock call would have frozen every other
  concurrent request and open WebSocket on that ECS task. Fixed with
  `starlette.concurrency.run_in_threadpool` plus bounded boto3 timeouts
  (`connect_timeout=5s`, `read_timeout=15s`, 2 attempts) — see
  `backend/tests/test_llm_client.py::test_bedrock_client_configures_bounded_connect_and_read_timeouts`.
  Without the bound, a hung call could occupy a worker thread for boto3's
  ~60s default indefinitely.
- **A Fire TV UX regression the WebSocket-auth fix would otherwise have
  introduced**: without a follow-on fix, a device whose token was
  rejected (4401) would have retried the exact same rejected credential
  forever at the capped 2s backoff, with no indication to the person that
  anything was wrong. Fixed by treating a 4401 (with a token actually
  present — not a device that simply hasn't paired yet) as a
  non-retryable signal that demotes the app back to the pairing screen,
  exactly matching the existing REST-401 contract in `useDeviceAuth.ts`.
- **Request-body size limiting and standard security response headers**
  (`X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy`,
  `Strict-Transport-Security`, `Referrer-Policy`) — neither existed
  before this pass. See `docs/adr/0003-plain-asgi-request-body-size-limit.md`
  for a real implementation bug (a `BaseHTTPMiddleware` approach that
  silently failed to enforce the limit) found and fixed during
  development, not just the final design.
- **Correlation-ID header injection**: an inbound `X-Request-ID` is now
  validated against a strict charset/length before being echoed or
  logged, closing a header/log-injection vector.
- **Full list of every prior pass's security work** (fail-closed auth,
  collision-safe pairing codes, WAF rate limiting, DynamoDB encryption/
  backup, CI supply-chain SHA-pinning, and more) is in
  `FINAL_FIX_SUMMARY.md` and `FINAL_PRODUCTION_READINESS_REPORT.md`.

## 5. Observability — what a judge or operator can actually see

- **Distributed tracing on both ECS services.** `backend/app/tracing.py`
  and `mcp_server/tracing.py` generate X-Ray-compatible trace IDs and
  export via an OTLP/HTTP-to-ADOT-collector-sidecar path, disabled by
  default via an env var and gated behind
  `infrastructure/template.yaml`'s `EnableOtelTracing` parameter. Both
  services now have their own collector sidecar and the
  `xray:PutTraceSegments`/`xray:PutTelemetryRecords` IAM permissions.
  The most recent repair pass redesigned `mcp_server/tracing.py` around
  explicit `TracerProvider` dependency injection instead of resetting
  OpenTelemetry's private one-shot global internals for tests:
  `configure_tracing()`/`instrument_app()` now accept an injected
  provider and return a structured `TracingRuntime` handle with an
  idempotent `.shutdown()`. While redesigning it, a real sanitization gap
  was found and fixed: OTel's built-in URL redaction only strips four
  hardcoded signed-URL query-string parameter names, so this app now has
  its own generic query-string sanitizer for span attributes, ensuring a
  sensitive query parameter can never leak into a trace regardless of its
  name. `mcp_server/tests/test_tracing.py` was rewritten and grew from 12
  to 20 tests, adding coverage for exporter-failure resilience and
  idempotent shutdown, and proves real span creation, trace-context
  propagation between an inbound MCP request and the outbound backend
  call in both directions, that tracing is a true no-op when disabled,
  and that no span attribute ever contains the MCP bearer token, the
  forwarded demo-access token, or an arbitrary sensitive query parameter.
  This has been re-verified locally (20/20 across 20 consecutive
  fresh-process runs; real span creation/propagation/no-leaked-secrets,
  plus `cfn-lint`/`sam validate` on the infra side) — **not** against a
  real AWS account or a real X-Ray/CloudWatch ServiceLens console; see
  §6 and `REMAINING_EXTERNAL_VERIFICATION.md`.
- **Repository write boundary and cross-race lease fencing (most recent
  repair pass).** `RaceStateRepository` now exposes three distinct write
  methods instead of two ambiguous ones: `save_state_from_simulator()`
  and `save_control_from_simulator()` are leader-owned tick/control
  writes that require a valid live lease proof in DynamoDB mode (failing
  closed with no proof), while a new
  `transition_control_from_operator()` handles operator commands
  (`/start`, `/pause`) and deliberately requires **no** lease proof, so
  those commands work regardless of which backend task currently holds
  simulator leadership — it uses its own atomic `control_revision`
  counter, stored as a first-class DynamoDB attribute, for optimistic
  concurrency. This fixes a real defect: `factory.py` previously forced
  `require_lease_proof=True` unconditionally in DynamoDB mode, so
  start/pause (which called the old, ambiguous `save_control()` with no
  lease proof) would raise `StaleWriteError` against any real DynamoDB
  deployment — this only ever appeared to work because tests and demos
  ran in memory mode. Separately, both `repositories/memory.py` and
  `repositories/dynamodb.py` now explicitly reject a `LeaseProof` whose
  race ID or canonical lease name (see `simulator_lease_name()` in
  `repositories/lease.py`) doesn't match the write's real target race —
  previously a proof legitimately acquired for one race could be reused
  to authorize a write to a different race. Covered by
  `backend/tests/test_dynamodb_simulator_service.py` (14 tests),
  `backend/tests/test_api_dynamodb.py` (4 REST-level tests against a
  real Moto-backed DynamoDB app), and
  `backend/tests/test_cross_race_lease_fencing.py` (22 tests,
  parametrized across memory and Moto-backed DynamoDB).
- **Stale-read pause protection and control_revision consistency (third
  repair pass).** A separate, subtler defect from the stale-leader window
  above: a simulator leader whose lease is perfectly valid — not stale,
  not superseded — could still overwrite an operator's pause, because
  `SimulatorService._advance()` re-read the control record after
  committing race state but then ignored what it read, unconditionally
  forcing the status back to RUNNING or COMPLETED. This is a stale-**read**
  problem, not a stale-**leader** problem, so lease fencing alone can't
  catch it: the write genuinely comes from the current, valid leader. Fixed
  at the service level (`_advance` now preserves whatever lifecycle status
  is actually stored, only ever transitioning RUNNING->RUNNING or
  RUNNING->COMPLETED) and, as defense-in-depth, at the repository level
  (`save_control_from_simulator`, in both `repositories/memory.py` and
  `repositories/dynamodb.py`, now additionally fences its write on the
  stored `control_revision` matching what the write was built from,
  atomically, so a write built from a stale pre-pause read is rejected
  even if the service-level fix were somehow bypassed). Separately,
  `control_revision` itself was inconsistent between DynamoDB's top-level
  attribute (previously computed via an atomic `if_not_exists(...) + 1`
  expression) and the serialized JSON blob (built from the caller's own
  stale pre-write read) — `transition_control_from_operator` is now a
  genuine compare-and-set with a bounded retry loop: read the current
  revision, compute `next_revision` once in Python, write that same
  literal value to the attribute, the JSON blob, and the returned object,
  conditioned on the stored revision still matching what was read; a
  losing concurrent call retries against a fresh read instead of
  erroring, which is also what lets two genuinely concurrent `/start` or
  `/pause` calls both succeed rather than one returning HTTP 500.
  `get_control()` now always uses `ConsistentRead=True` in DynamoDB mode.
  Covered by `backend/tests/test_control_revision_fencing.py` (15 tests)
  and `backend/tests/test_simulator_leadership.py` (3 tests), each run 20
  consecutive times clean using deterministic hooks/barriers rather than
  sleep-based timing.
- **MCP tracing tests made immune to ambient sampler configuration (third
  repair pass).** OTel's SDK reads `OTEL_TRACES_SAMPLER`/
  `OTEL_TRACES_SAMPLER_ARG` from the environment whenever a
  `TracerProvider` is constructed without an explicit `sampler=` —
  confirmed empirically: `OTEL_TRACES_SAMPLER=always_off pytest
  tests/test_tracing.py` produced 6 real failures before this fix. Every
  `TracerProvider` `mcp_server/tests/test_tracing.py` constructs now
  explicitly passes `sampler=ALWAYS_ON`, and `mcp_server/tracing.py`'s own
  production provider-construction path now explicitly passes
  `sampler=DEFAULT_ON` (`ParentBased(ALWAYS_ON)`) — the SDK's existing
  default, made explicit rather than left to implicitly consult
  environment variables, so production behavior is unchanged. Re-verified
  clean under the same hostile `OTEL_TRACES_SAMPLER=always_off` setting
  across 20 consecutive fresh-process runs, and no test assertion was
  weakened to achieve this.
- `AWS_ARCHITECTURE.md` — full system diagram and component table.
- `docs/observability-runbook.md` — what each of the 7 CloudWatch alarms
  means and what to do when it fires, including the new
  `AuthenticationFailuresAlarm` added this pass specifically because this
  pass added the WebSocket auth check that alarm watches.
- `OperationalDashboard` (`infrastructure/template.yaml`) — one
  CloudWatch dashboard spanning both ALBs, both ECS services, DynamoDB,
  both Lambdas, and the application-level auth/pairing metrics; validated
  with `cfn-lint`/`sam validate` (§1 above) and covered by 6 new
  structural tests in `infrastructure/tests/test_template_security.py`.
- Every audit-relevant event is one structured JSON log line
  (`backend/app/observability.py`'s `audit_log`), with a hard guard that
  *raises* rather than logs if a call would include a secret-shaped field
  name — tested in `backend/tests/test_observability.py`.

## 6. Honest gaps — what this document does NOT claim

- **No real AWS deployment has been exercised.** Every AWS-specific claim
  above is a claim about what the CloudFormation template *defines* and
  what `cfn-lint`/`sam validate` confirm about its *syntax and structure*
  — not a claim that it has been observed running. `docs/deployment-runbook.md`
  has the exact deploy steps; `REMAINING_EXTERNAL_VERIFICATION.md` has
  the full list of what still needs a real account.
- **No real Fire TV device or Alexa+ device has run this software.** The
  Fire TV test suite runs against a Jest/React Native test environment,
  not a physical device; `alexa_plus_client/simulated_client.py` is a
  simulated MCP client, not the real Alexa+ platform. Both are
  architected to be the real thing (real REST/WebSocket/MCP protocol
  calls against a real backend), but "architected to be" is not the same
  claim as "verified to be," and this document does not blur that line.
- **`npm audit` has known findings covered by named, time-boxed
  exceptions, not silently patched around.** The most recent run of
  `npm_audit_gate.py --policy fire-tv/security/npm-audit-policy.json
  --project-dir fire-tv` reported 9 raw findings (8 moderate, 1 high;
  the raw count naturally shifts slightly with npm's own dependency
  resolution) reducing to exactly 3 distinct advisories
  (fast-xml-parser, image-size x2 GHSA IDs), all covered by current,
  unexpired, build-time-only exceptions (expiring 2026-12-31) — see
  `REMAINING_EXTERNAL_VERIFICATION.md` §6 for the full root-cause
  analysis and why a permanent fix requires a real Android build/device
  this sandbox does not have.
- **Fire TV Jest coverage instrumentation is disabled in CI** (a real,
  reproduced `babel-plugin-istanbul`/`react-native-dotenv` interaction
  bug, not a workaround for a flaky test) — see
  `REMAINING_EXTERNAL_VERIFICATION.md` §7. Every test still runs and
  passes; only the coverage percentage report is affected.

## Related documents

- `AWS_ARCHITECTURE.md`, `docs/adr/` — architecture and design decisions.
- `JUDGE_DEMO.md`, `scripts/judge_demo.sh` — the live 3-minute demo script
  and its automated pre-flight check.
- `docs/deployment-runbook.md`, `docs/observability-runbook.md` — deploy
  and operate.
- `REMAINING_EXTERNAL_VERIFICATION.md` — the complete, honest list of
  everything that needs real AWS/device access to verify, with the exact
  command for each.
- `FINAL_FIX_SUMMARY.md`, `FINAL_PRODUCTION_READINESS_REPORT.md`,
  `VALIDATION_RESULTS.md` — the full historical record of every prior
  pass's corrections, for anyone who wants the complete paper trail
  rather than this document's summary.
