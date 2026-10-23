# Judge Q&A

AWS hackathon-final pass, phase 9. Anticipated questions about this
submission's scope, honestly answered — pulled from
`JUDGING_EVIDENCE.md`, `FINAL_PRODUCTION_READINESS_REPORT.md`,
`FINAL_READINESS_REPORT.md`, and `alexa_plus_client/README.md`, which
remain the fuller sources for anything summarized here.

**Q: Is this actually deployed on AWS right now?**
A: Not in this environment — this development sandbox has no AWS
credentials and no route to a real AWS account. The infrastructure
(`infrastructure/template.yaml`) has been validated statically
(`cfn-lint`, `sam validate --lint`, a 62-test infrastructure test suite
asserting IAM scoping, TLS defaults, and deploy-time Rules) but never
run through a real `sam deploy`. See `EXTERNAL_RELEASE_CHECKLIST.md` for
the exact commands to close that gap.

**Q: Does Alexa+ actually notify me proactively when the undercut window
opens?**
A: No. Alexa+ integrates with this backend purely *reactively* — it
calls an MCP tool when you ask it a question and gets a grounded answer
back. Nothing in this codebase pushes an unprompted notification to
Alexa+; the only real proactive push is the authenticated WebSocket
event to the Fire TV app. (An earlier draft of `JUDGE_DEMO.md`
incorrectly implied otherwise — corrected in this pass; see the file's
own history if you're comparing revisions.) `alexa_plus_client/` is
additionally a *simulated* MCP client — it has never been exercised
against a real Alexa+ account or device.

**Q: Has the Fire TV app run on a real Fire TV device?**
A: No. Its 186 tests run under Jest (`@testing-library/react-native`),
and its TypeScript compiles clean, but there is no physical Fire TV
device in this environment to install the built APK on. See
`scripts/preflight.sh`'s own NOT RUN row for this.

**Q: Is the deterministic strategy math ever overridden or invented by
the LLM?**
A: No, by design and by test. `ExplanationEngine` independently
re-checks every number the LLM narrates against the grounded facts it
was given (`backend/app/services/explanation_engine.py`'s grounding
guard) — the model can phrase a sentence, it cannot invent a lap time, a
margin, or a probability. This is exercised by `scripts/golden_demo.py`
itself (asserts "grounding guard flagged no unsupported numbers" on
every run) and by unit tests in `backend/tests/`.

**Q: What happens if Bedrock is unavailable or throttled?**
A: The backend falls back to a deterministic template narrator — see
`LlmProvider`'s own description in `infrastructure/template.yaml` and
the phase-5 `BedrockFallbackUsed`/`BedrockThrottled` CloudWatch metrics
that make this observable in a real deployment. The race-fact numbers
never change based on which narrator is active; only the prose does.

**Q: Is the observability (tracing/metrics) real, or just code that
exists?**
A: Both are real, wired-through infrastructure (an ADOT collector
sidecar per ECS service, CloudWatch EMF metrics with two new alarms, a
dashboard) — but neither has been exercised against a real AWS account.
Tracing now covers both ECS services: the backend's original
`backend/app/tracing.py` and, added in a later correction pass, the
equivalent `mcp_server/tracing.py`, each with its own collector sidecar
and IAM permissions in `infrastructure/template.yaml`. `mcp_server/tracing.py`
was redesigned in a prior repair pass around explicit
`TracerProvider` dependency injection (rather than resetting OpenTelemetry's
private global internals for tests), and a real sanitization gap was found
and fixed while doing so: OTel's built-in URL redaction only strips four
hardcoded signed-URL query parameter names, so this app now also runs its
own generic query-string sanitizer over span attributes. The third repair
pass hardened the tracing tests further: OTel's SDK reads
`OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` from the environment
whenever a `TracerProvider` is constructed without an explicit `sampler=`
(confirmed empirically — `OTEL_TRACES_SAMPLER=always_off pytest
tests/test_tracing.py` produced 6 real failures beforehand). Every
`TracerProvider` the tests construct now explicitly passes
`sampler=ALWAYS_ON`, and the production provider-construction path in
`mcp_server/tracing.py` now explicitly passes `sampler=DEFAULT_ON` — the
SDK's existing default, made explicit rather than implicit, so production
sampling behavior is unchanged. Validated here
with `cfn-lint`/`sam validate --lint` and unit tests only
(`backend/tests/test_tracing.py`, `backend/tests/test_metrics.py`,
`mcp_server/tests/test_tracing.py` — 47 tests total in mcp_server, run
clean 20 consecutive times including under a deliberately hostile
`OTEL_TRACES_SAMPLER=always_off` environment — proving real span creation,
cross-service trace-context propagation, exporter-failure resilience,
idempotent shutdown, and that no span attribute ever contains a
bearer/access token or an arbitrary sensitive query parameter). See
`docs/observability-runbook.md`'s own HONEST STATUS note.

**Q: Does raising `BackendDesiredCount` above 1 actually work safely
(no split-brain, no dropped WebSocket events)?**
A: Yes, by construction and by test: every fenced DynamoDB write is a
single `TransactWriteItems` call that also `ConditionCheck`s the live
LEASE item itself (exact owner, exact fencing token, not expired), not
just a monotonic floor compared against write history
(`backend/app/repositories/dynamodb.py`, `lease.py`). This closes the
stale-leader window a floor-only design leaves open — a leader that
pauses past its lease TTL and resumes can no longer write after being
superseded, even if the successor hasn't written yet — and production
DynamoDB mode fails closed if no lease proof is supplied at all. A
deploy-time CloudFormation Rule also refuses `BackendDesiredCount > 1`
unless the cross-instance event broker is also enabled — a multi-task
deployment with only the in-process broker would silently drop events
for clients connected to a different task. Tests prove the
stale-leader/expired-lease/wrong-owner/wrong-token rejection cases and
that two simulator instances never commit duplicate laps, but neither
path has been exercised against a real multi-task ECS deployment.

The most recent repair pass split the write boundary further: leader-owned
tick/control writes (`save_state_from_simulator()`,
`save_control_from_simulator()`) still require a valid, matching lease
proof, but operator commands (`/start`, `/pause`) now go through a new
`transition_control_from_operator()` method that deliberately requires
**no** lease proof — start/pause must work regardless of which ECS task
currently holds simulator leadership, and previously they didn't: `factory.py`
forced `require_lease_proof=True` unconditionally in DynamoDB mode, so
`/start`/`/pause` (which called the old, ambiguous `save_control()` with no
proof) raised `StaleWriteError` against any real DynamoDB deployment — this
only ever looked fine because tests and demos ran in memory mode. The
operator path uses its own atomic `control_revision` counter, stored as a
first-class DynamoDB attribute, for optimistic concurrency. Separately, a
real cross-race bug was found and fixed: a `LeaseProof` legitimately
acquired for one race could previously be reused to authorize a write to a
*different* race, because the persistence layer never checked the proof's
race ID against the write's actual target. Both `repositories/memory.py`
and `repositories/dynamodb.py` now reject a mismatched proof explicitly
(see `simulator_lease_name()` in `repositories/lease.py`), covered by 22
new parametrized tests
(`backend/tests/test_cross_race_lease_fencing.py`) plus 14 new
DynamoDB-simulator-service tests and 4 new DynamoDB REST-level tests.

The third repair pass found and fixed two more defects in this same
area. First, a valid, still-current leader (its lease genuinely was not
stale or superseded) could still overwrite an operator's pause: after
committing race state, `SimulatorService._advance()` re-read the control
record but then ignored what it read, unconditionally forcing the status
back to RUNNING/COMPLETED. This is a stale-**read** problem, distinct
from the stale-**leader** problem the lease fencing above closes — fixed
at the service level (lifecycle status is now preserved unless the
transition is genuinely RUNNING->RUNNING or RUNNING->COMPLETED) and, as
defense-in-depth, at the repository level (`save_control_from_simulator`
now also fences its write on the stored `control_revision` matching what
the write was built from, atomically). Second, `control_revision` was
inconsistent between DynamoDB's top-level attribute and the serialized
JSON blob — `transition_control_from_operator` is now a genuine
compare-and-set with a bounded retry loop instead of mixing an atomically
computed attribute with a blob built from a stale read, and
`get_control()` now always uses `ConsistentRead=True`. Covered by 15 new
tests in `backend/tests/test_control_revision_fencing.py` and 3 new
tests in `backend/tests/test_simulator_leadership.py`, each re-run 20
consecutive times clean using deterministic hooks/barriers, not
sleep-based timing.

**Q: Is the pairing-code exchange atomic — can a code be consumed
without a token actually being issued, or vice versa?**
A: No — this is exactly the failure mode phase 3 closed. Code
consumption, device-token creation, and its index entry are one
DynamoDB `TransactWriteItems` call; a code is never burned if token
creation fails. See `backend/app/repositories/pairing_dynamodb.py` and
its test coverage.

**Q: What's the single biggest remaining gap between "this repo" and "a
production launch"?**
A: External verification against real AWS — everything above that says
"validated statically, never deployed" is the honest gap. The source
itself does not claim to be 100% production-ready; see
`FINAL_PRODUCTION_READINESS_REPORT.md`'s explicit two-status verdict
(source readiness vs. externally verified deployment readiness).
