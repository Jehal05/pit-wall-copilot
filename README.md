# Pit Wall Copilot — Gate 0 + Gate 1 + Gate 2 + Gate 3 + Gate 4 + Gate 5

Agentic race-strategy companion for Amazon Fire TV + Alexa+.
**This repository currently implements Gate 0 through Gate 4 in full**,
plus the parts of **Gate 5** that are actually buildable without real
AWS/Alexa access (§2e has the honest breakdown) — the first vertical
slice, the deterministic strategy engine, the persistent Strategy Watch
Engine, LLM-grounded natural-language explanation of alerts, an MCP tool
server + Alexa+ voice skill wrapping all of it, multi-watch voice
disambiguation, and the infrastructure-as-code + runbook needed to
actually deploy it, from the
[Product & Technical Specification v1.1](./docs) project.

> **All race data in this repository is 100% synthetic and fictional.**
> Drivers, teams, the track, timing values and gaps are authored for this
> demo. Nothing here is sourced from, affiliated with, or endorsed by
> Formula 1, the FIA, or any real motorsport series, team or driver.

> **Local demo status vs. AWS status — read this before presenting.**
> The release you have (`pit-wall-copilot-v1.1-local-demo-fixed-aws-pending`)
> has its **local backend functionality validated**: the full local test
> suite passes, the natural-language undercut question is grounded in the
> real StrategyEngine, the pit-lane speed-limit regulation query is found
> honestly (no invented numeric value), the simulator's lap-28 completion
> is clearly reported, no exposed token remains in this repository, and
> the local test suite makes no outbound AWS/network call at all. **AWS
> deployment itself is still pending** — nothing in this repository has
> been proven to run on a real ECS task, behind a real ALB, calling a real
> Bedrock endpoint, or reporting to real CloudWatch/X-Ray. See
> [`LOCAL-DEMO-VALIDATION-REPORT.md`](./LOCAL-DEMO-VALIDATION-REPORT.md)
> and [`LOCAL-DEMO-CHECKLIST.md`](./LOCAL-DEMO-CHECKLIST.md) for the exact,
> current, honest scope of what has and has not been verified. In
> particular: `event_broker=memory` (this project's local-demo default) is
> correct **only** for a single backend process — a real multi-instance
> AWS deployment requires the configured cross-instance broker
> (`EVENT_BROKER=redis`; see `AWS_ARCHITECTURE.md`), and evidence collected
> against `127.0.0.1` must never be presented as AWS evidence.

## 0. Spec v1.1 pass 4 — production-readiness hardening (start here)

This repository has been through a fourth hardening pass focused
specifically on production-readiness: durable (DynamoDB-backed) device
pairing that survives a restart and works across multiple ECS tasks;
correct WebSocket reconnect/run-isolation behavior on Fire TV; every
backend network call routed through a release-transport security guard;
a deterministic, permission-preserving release ZIP; fail-closed production
configuration validation and structured audit logging; container/AWS
hardening (read-only filesystems, dropped capabilities, deployment circuit
breakers); fail-closed Android release signing; and a full CI workflow.

**Read [`FINAL_READINESS_REPORT.md`](./FINAL_READINESS_REPORT.md) first**
— it is the current, authoritative record of what was fixed, what was
verified (with real command output and test counts, most recently a
third repair pass fixing a stale-read pause-overwrite defect and a
`control_revision` consistency bug, on top of an earlier P0 repair pass
that fixed the operator/leader write-boundary split, cross-race lease
fencing, and the MCP tracing redesign), and what remains genuinely
unverified (signed APK, real AWS deployment, physical Fire TV, real
Alexa+ journeys — see
[`EXTERNAL_RELEASE_CHECKLIST.md`](./EXTERNAL_RELEASE_CHECKLIST.md) for the
exact commands to run those). `FINAL_PRODUCTION_READINESS_REPORT.md` is
the historical record of pass 4 specifically and is superseded for
current status by `FINAL_READINESS_REPORT.md`. **"Production-ready
source code" and "production-verified product" are different claims** —
this repository supports the former; only a person with a signed APK, a
real AWS account, a physical Fire TV, and a real Alexa+ device can
establish the latter.

The rest of this README (below) predates pass 4 and describes Gates 0–5 as
they were delivered through pass 3; it has not been rewritten wholesale,
but nothing in it is superseded or contradicted by pass 4's changes —
pass 4 is additive hardening on top of the same architecture.

## 1. Project purpose

Pit Wall Copilot is an AI race-strategy companion that continuously
observes a race, monitors user-defined conditions, and explains strategic
opportunities — grounded in deterministic calculations and retrieved
evidence rather than model invention. See the full product specification
for the end-to-end vision.

## 2. Gate 0 scope

Gate 0 delivers the first complete vertical slice:

```
Synthetic race simulator → canonical race state → FastAPI endpoints → Fire TV dashboard
```

Concretely, Gate 0 includes:

- A deterministic race simulator that plays back `scenarios/scenario-demo-01.json`,
  advancing from **lap 24 to lap 28** every 2 seconds, for **four fictional
  cars** (CAR_03, CAR_04, CAR_07, CAR_11).
- A FastAPI backend exposing race state and demo control endpoints.
- A repository abstraction with an in-memory implementation (default, no
  AWS required) and a DynamoDB implementation (`boto3`, version-aware
  conditional writes).
- Run identity (`run_id`) and monotonic `state_version` so a reset can
  never be corrupted by a stale event from a previous run.
- A React Native / TypeScript Fire TV dashboard that polls race state every
  2 seconds and exposes Start / Pause / Reset as D-pad-navigable controls.

**Explicitly out of scope for Gate 0** (per the specification): the
strategy engine, undercut calculation, watch evaluator, Bedrock, the
Knowledge Base, MCP, and Alexa+. These belong to later gates.

## 2a. Gate 1 scope

Gate 1 adds the deterministic **Strategy Engine** on top of the Gate 0
canonical race state, implementing spec v1.1 sections 15–17 and the
precise contract from sections 46–47:

- **Lap-time model** (`backend/app/services/strategy_engine.py`):
  `PredictedLapTime_ms = BaseTrackTime_ms + DriverDelta_ms + CompoundDelta_ms
  + TireDegradation_ms(age) + TrafficPenalty_ms + EnvironmentalAdjustment_ms`,
  exactly as specified in section 46.1. All internal values are integer
  milliseconds. Tire age is evaluated at the start of the projected lap; a
  pit stop resets it to zero for the first completed lap on the new
  compound; warm-up loss applies to that first lap; pit loss (entry + lane
  transit + stationary + exit) is added once, on the pit lap.
- **Undercut calculation** (`calculate_undercut`): simulates Scenario A
  (candidate pits lap N, target responds N+1) from a single immutable race
  state snapshot, through `comparison_horizon_lap = max(candidate_pit_lap,
  target_pit_lap) + 2` (section 46.2). `expected_margin_ms` follows the
  spec's exact sign convention: positive means the candidate is projected
  ahead. Every result carries `calculation_id`, `model_version`,
  `source_state_version`, both pit schedules, the comparison horizon,
  traffic classification and assumptions.
- **Mandatory seeded Monte Carlo confidence model** (section 47): every
  undercut and simulation runs exactly 200 deterministic pseudo-random
  trials seeded from the scenario's `model_seed`, varying lap pace, pit
  duration, degradation and traffic loss. Returns `median_margin_ms`,
  `p10_margin_ms`, `p90_margin_ms` and `probability_ahead`. The undercut
  window is `OPEN` when `probability_ahead >= 0.65` and otherwise
  `CLOSED` (the same 0.65/0.50 hysteresis thresholds the future Watch
  Engine will use).
- **`simulate_strategy`**: a single-driver PIT NOW vs WAIT-k-laps
  counterfactual (the "what happens if the driver stays out three more
  laps" journey), returning `delta_ms` — positive means pitting now is
  faster by that margin — plus its own Monte Carlo confidence result.
- All strategy-model parameters (tire curves, pit-loss components,
  warm-up loss, traffic penalty, environmental adjustments, Monte Carlo
  noise distributions, watch thresholds) live in
  `scenarios/scenario-demo-01.json` under `strategy_model`, not hardcoded
  in Python — consistent with the Gate 0 principle that the scenario file
  is the single source of the changing demonstration values.
- Two new REST endpoints (see §11): `POST /v1/races/{race_id}/strategy/undercut`
  and `POST /v1/races/{race_id}/strategy/simulate`. These are plain HTTP,
  not MCP — MCP/Alexa+ remain out of scope until a later gate; the tool
  names and input shapes match spec section 22 so wiring an MCP server to
  them later is a thin adapter, not a rewrite.
- **A documented modeling simplification**: the canonical race-state
  contract's `base_pace_ms` field bundles `BaseTrackTime_ms` and
  `DriverDelta_ms` together (that split isn't separately observable in the
  Gate 0 state), so the engine treats `base_pace_ms` as their sum. This is
  noted directly in `predicted_lap_time_ms`'s docstring.

**Explicitly out of scope for Gate 1**: the persistent Strategy Watch
Engine (section 18 — DynamoDB Streams-triggered watch evaluation with
hysteresis and idempotent alerts), Bedrock, the Knowledge Base, MCP, and
Alexa+. Gate 1 computes undercut/simulation results on demand via REST;
it does not yet persist watches or push proactive alerts.

## 2b. Gate 2 scope

Gate 2 adds the persistent **Strategy Watch Engine** on top of the Gate 1
strategy engine, implementing spec v1.1 sections 18 and 48–49:

- **Watch domain model** (`backend/app/domain/watch_models.py`): a `Watch`
  (candidate, target, open/close thresholds, current `status`, the
  `state_version` it was last evaluated against) and an `Alert` (an
  immutable record of one `OPEN`/`CLOSED` transition, carrying its own
  `triggering_snapshot: RaceState` and the full deterministic
  `UndercutResult` that produced it).
- **Watch + Alert repositories** (`backend/app/repositories/watch_base.py`,
  `watch_memory.py`, and — fixing this gate's original in-memory-only
  limitation — `watch_dynamodb.py`): the same abstraction pattern as
  Gate 0's race-state repository, now with the same `memory`/`dynamodb`
  choice too (`STATE_REPOSITORY`, §10/§14). `AlertRepository.record_transition`
  performs a **conditional write keyed by `run_id:watch_id:state_version:new_status`**
  — a real DynamoDB conditional put in `dynamodb` mode, an `asyncio.Lock`
  in `memory` mode — so replaying the exact same state transition never
  creates a second alert (spec 49's idempotency requirement) either way.
- **Watch Evaluator** (`backend/app/services/watch_evaluator.py`):
  - `create_watch(race_state, candidate, target)` computes a baseline
    `calculate_undercut` result immediately and stores the watch with that
    starting `OPEN`/`CLOSED` status — **creation never publishes an
    alert**; only a subsequent transition does.
  - `evaluate(race_state)` is invoked on **every committed race-state
    change** (see the streams emulation below): it loads only the watches
    active for that race's *current* `run_id`, recomputes
    `calculate_undercut` for each, applies hysteresis
    (`open_probability >= 0.65` opens; `probability_ahead <=
    close_probability (0.50)` closes; anything in between holds — this is
    what stops the watch oscillating on evaluation noise), and for every
    watch whose status actually changed, writes one idempotent `Alert`
    with an immutable copy of the triggering `RaceState`.
- **DynamoDB-Streams emulation**: real DynamoDB Streams trigger a Lambda
  on every table write; this repository has no live AWS Streams
  infrastructure, so `SimulatorService` accepts an `on_state_committed`
  async callback and invokes it once, synchronously, immediately after
  every accepted state write (`backend/app/services/simulator.py`).
  `main.py`'s lifespan wires `watch_evaluator.evaluate` as that callback.
  This preserves the same trigger semantics (evaluate once, right after
  state is durably committed) without standing up real AWS infrastructure
  for local development for a hackathon-scale demo.
- **Run-scoped watches**: `list_active_watches(race_id, run_id)` only
  returns watches created under the race's *current* run. A `/v1/demo/reset`
  generates a new `run_id` (unchanged Gate 0 behavior), which automatically
  excludes older watches from evaluation without deleting their history —
  matching spec 48's requirement that a reset invalidates prior-run watches.
- **"Why now?" contract** (spec 49): `GET /v1/races/{race_id}/alerts/{alert_id}`
  always returns the alert's own stored `triggering_snapshot`, never the
  live race state — verified live in this delivery by fetching an alert
  again after the race had advanced several more laps past it (§11).
- Six new REST endpoints (see §11): create/list/get/delete a watch, and
  list/get alerts. These are plain HTTP, matching the same "REST now, MCP
  later" posture as Gate 1's strategy endpoints — the tool names and input
  shapes match spec section 22 (`create_strategy_watch`).

**Explicitly out of scope for Gate 2**: Bedrock, the Knowledge Base, MCP,
Alexa+, and any real-time push transport. Alerts are exposed via a
polling REST endpoint (`GET /v1/races/{race_id}/alerts`); a genuine
push/WebSocket channel to Fire TV is deferred, consistent with Gate 0's
explicit "no WebSockets yet" scope decision.

## 2c. Gate 3 scope

Gate 3 adds **LLM-grounded natural-language explanation** of the alerts
Gate 2 already persists, implementing spec sections 19 and 50 while
strictly preserving the architecture's core rule stated in §1: *"The LLM
explains and orchestrates. It does not invent race state, sporting rules
or strategy mathematics."*

- **One new endpoint**: `GET /v1/races/{race_id}/alerts/{alert_id}/explain`
  — takes an existing alert's `alert_id` and returns an `ExplanationResult`
  with a short natural-language narrative, the sporting-rules citations
  used, and the exact deterministic facts the narrative was grounded in.
  It is a read, like `GET .../alerts/{id}` itself, so — consistent with
  every other GET endpoint in this API — it does not require the demo
  access token. Results are cached per `alert_id` and the route is
  rate-limited per client IP (§14 has the detail).
- **Grounded facts, not free generation** (`backend/app/services/explanation_engine.py`):
  `ExplanationEngine.explain_alert` builds a `grounded_facts` dict
  *entirely* from the target alert's own immutable
  `triggering_snapshot` and `deterministic_result` (both already
  persisted, unmodified, by Gate 2) — margin, probability, lap, tire
  ages, traffic classification, assumptions. The LLM is handed exactly
  this dict as text (e.g. `{"expected_margin_ms": "910", ...}`) and
  instructed to narrate only these values, verbatim; it is never asked
  to compute, estimate or round a number itself.
- **A machine-checked grounding guard, not just a prompt instruction**:
  after generation, every numeric token in the returned narrative is
  extracted and checked against the numbers actually present in
  `grounded_facts` (and the retrieved citations). Anything that doesn't
  trace back to a given value is recorded in
  `ExplanationResult.grounding_warnings` rather than silently trusted.
  This is what makes it safe to point the engine at a real,
  non-deterministic model later without weakening the "never invent a
  number" guarantee — the check does not rely on the LLM being well
  behaved. Small integers (0-9) are excluded from the check to avoid
  flagging a real model's ordinary phrasing (list counts, etc.) as
  ungrounded. A second, proximity-based check
  (`ExplanationEngine._check_attribution`) additionally catches a
  narrative that states only real, grounded numbers but pins one to the
  wrong driver (e.g. swaps whose tires are older) — see §14 for exactly
  what it does and does not cover.
- **`LLMClient` abstraction** (`backend/app/services/llm_client.py`), same
  memory/DynamoDB pattern as the state repository, selected by
  `LLM_PROVIDER`:
  - `TemplateLLMClient` (**default** — no AWS account required): not
    actually a generative model. It deterministically assembles the
    narrative by interpolating the grounded facts into fixed sentence
    templates, so identical inputs always produce byte-identical output
    and it can never mention an ungrounded number by construction. This
    is what local development, CI, and this delivery's own tests use.
  - `BedrockLLMClient` (opt-in, `LLM_PROVIDER=bedrock`): calls a real
    Anthropic-on-Bedrock model via boto3's `bedrock-runtime.invoke_model`,
    using the documented Messages-API request/response shape.
    **Implemented against that documented API but — like the DynamoDB
    repository before real AWS credentials are available — not
    exercised against a live AWS account in this environment** (no
    network egress to AWS / no Bedrock access here). It is covered by a
    unit test that constructs the client and confirms it fails fast with
    a wrapped `LLMClientError` rather than a raw, unhandled exception —
    that is genuinely exercised; a successful real Bedrock call is not.
- **Local Knowledge-Base stand-in** (`backend/app/services/knowledge_base.py`,
  `docs/sporting-rules/*.md`): four short, **entirely fictional** sporting-
  rules documents (the undercut, pit-lane procedure, tire compounds,
  track status/safety car) are loaded at startup and ranked for a query
  by simple keyword overlap — a documented simplification standing in
  for a real Bedrock Knowledge Base's managed embeddings + vector search
  over a much larger corpus (see that file's module docstring for
  exactly what a production version would need instead). It sits behind
  the same `retrieve(...) -> list[KBCitation]` interface a real Bedrock
  Knowledge Base client would implement, so swapping it out later doesn't
  change anything above it.
- All of Gate 3's new code paths are exercised by **37 tests** across five
  files (`test_knowledge_base.py`, `test_llm_client.py`,
  `test_explanation_engine.py`, `test_explanation_api.py`,
  `test_rate_limiter.py`), including a deliberately hallucinating fake LLM
  client used specifically to prove the grounding guard actually catches
  an invented number rather than only ever seeing clean template output.
- **`/explain` results are cached per `alert_id`, and the route is
  rate-limited per client IP** (fixes the limitation described below —
  see `app/services/explanation_engine.py`'s and
  `app/services/rate_limiter.py`'s module docstrings, and §14).

**Explicitly out of scope for Gate 3**: MCP and Alexa+ voice integration
(the explanation is available via REST only, for now — Gate 4 later adds
both); a real Bedrock Knowledge Base backed by managed vector search.

## 2d. Gate 4 scope

> **⚠ SUPERSEDED — this section describes Gate 4 as originally built, not
> the current implementation.** Spec v1.1 repair 7 rewrote `mcp_server/`
> around a **Streamable HTTP** transport exposing **exactly 5** P0 tools
> (not the 8 stdio tools described below), and spec v1.1 repair 10 made
> `alexa_plus_client/` (not the Alexa Skill described below) the
> **primary** Alexa+ integration, moving the original Lambda skill to
> `legacy/alexa_skill_legacy_rest/` as an explicitly optional fallback.
> See §7a/§7b for current setup instructions and §"3. Architecture"'s
> diagram for the current shape. This section is kept for historical
> context on what Gate 4 originally delivered before those repairs.

Gate 4 wraps the entire REST surface built in Gates 1–3 as **MCP tools**
and adds an **Alexa+ voice skill** on top, implementing spec section 22's
MCP tool contract and the voice-narration half of sections 19/50 —
without adding any new business logic anywhere in this gate.

- **`mcp_server/`** — a real MCP server (the official `mcp` Python SDK,
  stdio transport, not a hand-rolled protocol stand-in) exposing 8 tools:
  `get_race_state`, `calculate_undercut`, `simulate_strategy`,
  `create_strategy_watch`, `list_strategy_watches`, `list_alerts`,
  `get_alert`, `explain_alert`. Every tool is a direct HTTP call into the
  Gate 0–3 backend — `mcp_server/server.py` contains **no strategy math,
  no watch/alert logic, and no LLM prompting of its own**, so every
  guarantee from earlier gates (deterministic calculations, idempotent
  alerts, the machine-checked explanation grounding guard) carries over
  completely unchanged.
  - Kept in its **own virtual environment**, deliberately: the `mcp` SDK
    pulls in a newer `starlette` than `backend/requirements.txt` pins for
    FastAPI, and isolating the two avoids destabilizing the
    already-verified backend rather than forcing a fragile shared
    dependency resolution (see `mcp_server/server.py`'s module docstring).
  - Tool names and input shapes match `backend/app/api/schemas.py`'s
    request bodies exactly, which is what makes this "a thin adapter, not
    a rewrite" — a promise the Gate 1–3 README notes made from the start.
  - Tested two ways: `mcp_server/tests/test_mcp_tools.py` (9 tests) spins
    up a real backend subprocess and drives the MCP server's tools
    through `FastMCP.call_tool` — the real tool-dispatch path, not the
    bare Python functions underneath — and a manual live check (§11) goes
    one step further and drives it over the **real stdio JSON-RPC
    transport** with the official `mcp` client SDK, exactly as an MCP
    host (Claude Desktop, an Alexa+ skill's backend, etc.) would connect
    to it.
- **`legacy/alexa_skill_legacy_rest/`** — an AWS Lambda handler (`ask-sdk-core`) plus an ASK
  interaction model (`legacy/alexa_skill_legacy_rest/models/interaction-model.json`) with
  three custom intents — `CheckUndercutIntent`, `CreateWatchIntent`,
  `ExplainLatestAlertIntent` — each backed by a `DRIVER_ID` custom slot
  type mapping spoken phrases ("car four", "number four") to canonical
  driver ids. The handler calls the same REST endpoints the MCP server
  calls, and for `ExplainLatestAlertIntent` simply speaks
  `/explain`'s already-grounded, already-checked narrative verbatim — it
  never composes its own strategy explanation.
  - **19 unit tests** (`legacy/alexa_skill_legacy_rest/tests/test_lambda_function.py`,
    14 → 19 as of Gate 5's multi-watch disambiguation, §2e.1) construct
    real `ask_sdk_model` request envelopes — including a real `Session`
    object and simulated slot-type resolutions, the same shape Alexa's
    NLU sends — and dispatch them through the real, fully-wired
    `ask_sdk_core` skill, with only the outbound `requests` call mocked.
    One test confirms a raw backend error's structured detail is never
    read aloud verbatim (a small but real safety property for a voice
    interface). Several of the 19 chain real multi-turn dispatches
    into a real `ExplainLatestAlertIntent` dispatch, carrying the first
    turn's response session attributes into the second turn's request
    `Session` exactly as Alexa would, proving the multi-turn
    watch-tracking fix in §14 end-to-end.
  - A slot-resolution fallback (a small word/digit → driver-id map) is
    included and tested for when Alexa's NLU doesn't return
    `ER_SUCCESS_MATCH` — documented as a safety net, not the primary
    mechanism.

**\*\*\* IMPORTANT — HONEST STATUS ON REAL DEPLOYMENT \*\*\*** Both
pieces are real, working, tested code — genuinely exercised end-to-end
against a live backend (§11 has the transcripts) — but **neither has been
deployed to real infrastructure**: the MCP server has not been registered
with a real MCP host, and the Alexa skill has not been created in the
Alexa Developer Console, deployed to a real AWS Lambda, or exercised
against a real Echo device or voice input. There is no AWS/Alexa account
or MCP host available in this environment to do that — the same
constraint already documented for `BedrockLLMClient` (Gate 3) and for
Fire TV/Android device testing (Gate 0). §11 and the sections above are
explicit about exactly which parts were verified and how.

**Explicitly out of scope for Gate 4**: real deployment/registration of
either piece (see above); any Alexa Account Linking or per-user
authentication (the skill uses the same shared demo access token as the
REST API); multi-race or multi-user session handling in the Alexa skill
(it always targets `PIT_WALL_RACE_ID`, defaulting to `demo-race-001`);
and any MCP resources/prompts beyond the 8 tools (no MCP "resources" or
"prompts" primitives are exposed, only "tools").

## 2e. Gate 5 scope

Gate 5's own stated scope (README §15, as of Gate 4) is "deploy to real
infrastructure and close the loop on what Gate 4 built but could not
exercise for real" — by definition, most of it needs an AWS account,
Alexa Developer Console access, a Docker daemon, and a real Echo device,
**none of which are available in the environment that built this
repository**. Rather than fabricate deployment output, this gate splits
into what code alone can do (done, tested, below) and what genuinely
needs real infrastructure (not done — a runbook instead, §2e.2).

### 2e.1 — Done: multi-watch voice disambiguation

Gate 5's own secondary, code-level target (README §15): the Gate 4 fix
only ever remembered a session's single most-recently-created watch.
Gate 5 extends this to **multiple concurrently-watched cars per session**:

- `CreateWatchIntentHandler` now tracks every watch created in a session
  (`legacy/alexa_skill_legacy_rest/lambda/lambda_function.py`'s `_remember_watch`), keyed by
  `watch_id`, capped at 10 (FIFO eviction) rather than growing
  unboundedly.
- `ExplainLatestAlertIntentHandler` gained an optional `WatchDriver` slot
  (new in `legacy/alexa_skill_legacy_rest/models/interaction-model.json`) — "explain the car
  four watch" / "explain the alert for the car four watch" now resolves
  to whichever session watch actually involves CAR_04
  (`_find_watch_by_driver`: prefers a candidate match over a target
  match, and the most recently created watch among ties), instead of
  always meaning "whatever I created most recently." A driver with no
  matching session watch gets an honest "I don't have a watch open on
  that car this session" rather than silently falling back to the wrong
  watch. Omitting the slot still defaults to the most recent watch,
  unchanged from Gate 4.
- **5 new tests** in `legacy/alexa_skill_legacy_rest/tests/test_lambda_function.py`
  (14 → 19): disambiguation by slot proven against a genuinely ambiguous
  two-watch session (not just a single-watch happy path), the
  most-recent-watch fallback, the "no watch open on that car" message,
  `_find_watch_by_driver`'s candidate-over-target preference directly,
  and the FIFO eviction cap.

### 2e.2 — Not done here: the actual real-infrastructure deployment

Backend/mcp_server deployment to a public endpoint, MCP host registration,
Alexa Developer Console skill creation + real Lambda deployment, a
golden-demo run on a real Echo device, and a real `BedrockLLMClient` call
all remain **unexecuted** — this environment has no AWS account, Alexa
Developer Console access, Docker daemon, or Echo device to do any of it
with. What this gate (extended by spec v1.1 repair 12 — see §2f below)
adds instead, so that work is no longer starting from nothing:

- **`backend/Dockerfile`** and **`mcp_server/Dockerfile`** +
  **`docker-compose.yml`** (repo root): container images for the backend
  and `mcp_server`, reviewed against their respective `requirements.txt`
  files and env-var contracts, but **not build-tested** — `docker info`
  fails in this environment (no daemon socket). See each Dockerfile's own
  module docstring.
- **`infrastructure/template.yaml`**: a real, `cfn-lint`/`sam validate`-
  clean SAM template provisioning the shared DynamoDB table + Streams
  (race state + watches/alerts — `infrastructure/README.md` documents the
  full item-type/sort-key scheme), a real DynamoDB-Streams-triggered
  Watch Evaluator Lambda, the legacy Alexa skill Lambda, ECS Fargate
  services for the backend and `mcp_server` behind ALBs with WAF rate
  limiting, a Secrets Manager-generated demo access token, CloudWatch
  alarms and X-Ray tracing. **`sam build` was actually run** against both
  Lambda functions (real `pip install`, real file packaging — see
  `infrastructure/lambda/watch_evaluator_stream/Makefile`), and the
  packaged Watch Evaluator handler was confirmed importable AND invokable
  against a synthetic DynamoDB Streams event + a `moto`-mocked table
  (`backend/tests/lambda_handlers/`) — but never deployed against a real
  AWS account, since there is none here.
- **[`docs/deployment-runbook.md`](./docs/deployment-runbook.md)**: the
  concrete, step-by-step checklist for everything above — provision the
  stack, deploy the backend and `mcp_server`, create and deploy the Alexa
  skill, exercise Bedrock for the first time — written against documented
  AWS CLI / SAM CLI / Alexa Developer Console / ask-sdk / ECS behavior,
  with an explicit "what Gate 5 done looks like" checklist at the end.
- **`scripts/golden_demo.py`**, **`scripts/chaos_test.py`**,
  **`scripts/endurance_test.py`** (spec v1.1 repair 15): end-to-end,
  concurrency and repeated-cycle checks against a real, already-running
  backend — **all three were actually run against a real local backend
  subprocess in this environment** and passed (see
  `VALIDATION_RESULTS.md`), giving whoever deploys for real a way to
  immediately re-verify the golden path, not just re-run the existing
  pytest suites.

None of this requires new application code beyond the multi-watch fix
above and the Watch Evaluator Lambda adapter (a thin wrapper around the
already-tested `WatchEvaluator` — see
`backend/app/lambda_handlers/watch_evaluator_stream.py`) — Gates 0
through 5 already implement every domain-logic piece the deployed system
needs; what's left is exercising it against real infrastructure and
fixing whatever that first real contact surfaces, exactly as Gate 4
described for itself.

## 2f. Spec v1.1 "fixall" pass — 16 critical repairs

A later pass repaired this repository against "Pit Wall Copilot — Product
& Technical Specification v1.1" end to end. **`IMPLEMENTATION_STATUS.md`**
and **`VALIDATION_RESULTS.md`** (repo root) are the authoritative,
per-repair record of what changed and what was actually verified versus
what remains `NOT RUN` and why — this section is a pointer to those, not
a duplicate of their detail. In brief, the 16 repairs covered: correctness
fixes to the deterministic strategy/watch/alert pipeline (repairs 3-9,
11, 13, 14 — already folded into §2b/§2c's own scope descriptions above
since they corrected Gate 2/3 behavior directly); the Fire TV P0 insight
screens and D-pad actions (repair 2, §7 below); a real native Android
project (repair 1, §7's "Native Android project" subsection); the primary
MCP-based Alexa+ integration plus relocating the legacy REST skill
(repair 10, §7b below); this real AWS infrastructure pass (repair 12,
§2e.2 above); and the chaos/endurance/golden-demo scripts (repair 15,
directly above).

Two further passes followed this one. A second, deeper pass fixed 13
more specific correctness issues (stale DynamoDB runs, ECS Secrets
Manager scoping, an unauthenticated MCP endpoint, plaintext-HTTP-by-
default deployment, and more). A third, final pass then fixed 11
additional corrections: Android release signing (no more debug-signed
releases), every WebSocket event now handled (`STRATEGY_UPDATE`/
`DEMO_STATUS`/`DATA_STALE` were previously silently ignored),
environment-independent MCP/Alexa+ test interpreter resolution (no more
hardcoded `.venv` path), a stabilized Fire TV focus test (verified stable
across 3 consecutive full-suite runs), a REAL device-pairing exchange
(§7's "Device pairing" subsection below — replacing the earlier
shared-secret-entry flow), Docker/Compose health checks, a deterministic
packaging script (`scripts/package_release.py`) that produces and
self-inspects the final archive, release network security (no cleartext
in a release build, enforced both in the Android manifest and in
`api/client.ts`), a service-level concurrent-reset test, pytest test
categories (`unit`/`integration`/`infrastructure`/`android`/`aws`/
`device`) across every Python suite, and this documentation update
itself. **`FINAL_FIX_SUMMARY.md`**'s "Third pass" section is the
authoritative repair-by-repair record; **`VALIDATION_RESULTS.md`** §10-13
has the exact commands and output.

## 3. Architecture

```
scenarios/scenario-demo-01.json   (source of all changing demo values)
            │
            ▼
   backend/app/domain/scenario.py     (load + validate)
            │
            ▼
   backend/app/services/simulator.py  (deterministic lap-by-lap player,
                                        run_id / state_version control)
            │
            ├──────────────────────────────────────────┐
            ▼                                           ▼ on_state_committed
   backend/app/repositories/*                backend/app/services/watch_evaluator.py
   (memory | dynamodb — one interface)       (Gate 2: evaluates every active watch via
            │                                 strategy_engine.calculate_undercut,
            │                                 applies hysteresis, writes idempotent
            │                                 Alerts — emulates a DynamoDB-Streams-
            │                                 triggered Lambda, in-process)
            │                                           │
            ▼                                           ▼
   backend/app/api/routes.py          (FastAPI: /health, /v1/races/*, /v1/demo/*,
                                        /v1/races/{id}/strategy/*, /v1/races/{id}/watches,
                                        /v1/races/{id}/alerts, /v1/races/{id}/alerts/{id}/explain,
                                        /v1/agent/query, /v1/races/{id}/changes)
            │
            │  WebSocket push (primary, spec v1.1 repair 3) — RACE_STATE,
            │  STRATEGY_UPDATE, WATCH_ALERT, AGENT_RESPONSE, DATA_STALE,
            │  RACE_RESET — with 2s HTTP polling as the documented fallback
            ▼
   fire-tv/App.tsx                    (React Native Fire TV dashboard)

Gate 3 adds one more path, off an Alert already persisted above:

   Alert.triggering_snapshot + Alert.deterministic_result  (immutable, from Gate 2)
            │
            ▼
   backend/app/services/explanation_engine.py   (builds grounded_facts, retrieves
                                                   KB citations, calls the LLM client,
                                                   then re-checks the narrative's own
                                                   numbers against grounded_facts)
            │                                    │
            ▼                                    ▼
   backend/app/services/knowledge_base.py   backend/app/services/llm_client.py
   (docs/sporting-rules/*.md, keyword-       (TemplateLLMClient — default, deterministic —
    ranked retrieval; stands in for a real    or BedrockLLMClient — opt-in, calls a real
    Bedrock Knowledge Base)                   Anthropic-on-Bedrock model via boto3)

Gate 4 / spec v1.1 repair 7 & 10 add the voice-integration surface in
front of the same REST API — none of it contains any business logic,
all of it is a thin adapter over the one backend:

   backend/app/api/routes.py  (the same REST API, still the only implementation)
            │
            │ real HTTP
            ▼
   mcp_server/server.py         (FastMCP, STREAMABLE HTTP transport — spec v1.1
   (own venv — see §2d)          repair 7 — exactly 5 P0 domain tools:
                                  get_race_state, calculate_undercut,
                                  simulate_strategy, create_strategy_watch,
                                  get_regulation)
            │
            │ real MCP: initialize -> tools/list -> tools/call
            ▼
   alexa_plus_client/simulated_client.py    (PRIMARY Alexa+ integration —
   (own venv — see §2d)                      a simulated Alexa+ client, since no
                                              real Alexa+ account is available;
                                              "Why now?" additionally uses the
                                              REST-only /alerts + /explain
                                              endpoints, per repair 7's own
                                              carve-out for those two)

   legacy/alexa_skill_legacy_rest/lambda/lambda_function.py   (OPTIONAL LEGACY
   (own venv — see §2d)                                        FALLBACK, not the
                                                                 primary integration —
                                                                 ask-sdk-core; intents
                                                                 -> REST calls directly,
                                                                 bypassing the MCP tools)
```

The backend is one modular FastAPI application (not multiple
microservices), matching the Gate 0 instruction to keep this phase simple.
`mcp_server/`, `alexa_plus_client/`, and `legacy/alexa_skill_legacy_rest/`
are each separate Python packages with their own virtual environments
(§2d explains why), each ultimately calling the one backend over real
HTTP — this repository does not run multiple copies of the strategy
logic, only multiple ways of reaching the one implementation.

## 4. Folder structure

```
pit-wall-copilot/
├── backend/
│   ├── app/
│   │   ├── api/            # FastAPI routes, DTOs, dependency wiring
│   │   ├── domain/         # Pydantic models, scenario loader/validator,
│   │   │                   # strategy-model config + result types,
│   │   │                   # watch/alert domain models (Gate 2),
│   │   │                   # explanation domain models (Gate 3)
│   │   ├── repositories/   # Storage abstraction: race state + watch/alert
│   │   │                   # (memory + DynamoDB)
│   │   ├── services/       # Deterministic simulator, strategy engine,
│   │   │                   # watch evaluator (Gate 2), knowledge base +
│   │   │                   # LLM client + explanation engine (Gate 3)
│   │   ├── lambda_handlers/ # spec v1.1 repair 12: Watch Evaluator Lambda
│   │   │                   # adapter (DynamoDB Streams -> WatchEvaluator),
│   │   │                   # reusing services/repositories directly
│   │   ├── config.py
│   │   └── main.py
│   ├── tests/               # pytest suite (227 tests, incl. tests/lambda_handlers/)
│   ├── requirements.txt
│   └── requirements-dev.txt # adds moto for DynamoDB-mocked tests
├── fire-tv/
│   ├── App.tsx
│   ├── android/              # spec v1.1 repair 1: real native Android project
│   │   ├── app/               # LEANBACK_LAUNCHER manifest, TV banner/icons,
│   │   │                      # debug+release build types
│   │   └── gradlew, gradlew.bat, gradle/wrapper/*
│   ├── src/
│   │   ├── api/             # typed client + shared types (mirrors backend Pydantic models)
│   │   ├── components/      # FocusableButton, StandingsTable, badges, the
│   │   │                    # P0 screens (StrategyBattlePanel, SimulationComparisonScreen,
│   │   │                    # AgentAnswerScreen, EvidenceSourcesScreen,
│   │   │                    # StrategyWatchAlertOverlay, InsightModal, ...)
│   │   ├── hooks/           # useRaceState, useRaceSocket (WebSocket, repair 3),
│   │   │                    # useDemoControls, useStrategyBattle, useInsightActions
│   │   └── __tests__/       # jest + @testing-library/react-native (77 tests)
│   └── package.json
├── scenarios/
│   └── scenario-demo-01.json  # single source of the changing demo values
├── infrastructure/
│   ├── README.md            # DynamoDB table schema (manual provisioning)
│   ├── template.yaml        # spec v1.1 repair 12: SAM template — DynamoDB +
│   │                        # Streams, Watch Evaluator Lambda, legacy Alexa
│   │                        # Lambda, ECS Fargate (backend + mcp_server) +
│   │                        # ALBs, WAF, Secrets Manager, CloudWatch, X-Ray
│   └── lambda/watch_evaluator_stream/  # Makefile-based SAM build packaging
│                              # backend/app/ + scenarios/ into the Lambda
├── docs/
│   ├── sporting-rules/      # fictional rule excerpts backing Gate 3's
│   │                        # Knowledge-Base retrieval stand-in
│   └── deployment-runbook.md  # Gate 5: real-infrastructure deployment checklist
├── scripts/                 # spec v1.1 repair 15: golden demo, chaos and
│   │                        # endurance scripts against a real running backend
│   ├── golden_demo.py
│   ├── chaos_test.py
│   └── endurance_test.py
├── mcp_server/              # spec v1.1 repair 7: MCP tool server (own venv)
│   ├── server.py            # exactly 5 P0 tools, Streamable HTTP transport,
│   │                        # each a thin HTTP call into backend/
│   ├── Dockerfile           # spec v1.1 repair 12: container image (see §2e.2)
│   ├── tests/                # pytest suite (19 tests: in-process call_tool +
│   │                          # a real Streamable HTTP transport round trip)
│   ├── requirements.txt
│   └── requirements-dev.txt
├── alexa_plus_client/       # spec v1.1 repair 10: PRIMARY Alexa+ integration (own venv)
│   ├── simulated_client.py   # simulated Alexa+ client — real MCP session over
│   │                         # real Streamable HTTP; the 6 required voice journeys
│   ├── tests/                 # pytest suite (8 tests, real backend + real mcp_server)
│   └── requirements-dev.txt
├── legacy/                  # spec v1.1 repair 10: optional/non-primary components
│   ├── README.md
│   └── alexa_skill_legacy_rest/  # OPTIONAL FALLBACK, not primary — classic REST-based
│       │                          # ask-sdk-core Alexa Skill (own venv)
│       ├── lambda/
│       │   ├── lambda_function.py  # AWS Lambda handler (ask-sdk-core)
│       │   └── requirements.txt    # deployment deps (bundled into the Lambda zip)
│       ├── models/
│       │   └── interaction-model.json  # ASK interaction model (intents + slots)
│       ├── tests/                 # pytest suite (19 tests)
│       └── requirements-dev.txt
├── backend/Dockerfile        # Gate 5: backend container image (see §2e.2)
├── docker-compose.yml        # Gate 5: local-dev convenience (see §2e.2)
├── IMPLEMENTATION_STATUS.md  # spec v1.1 fixall pass: per-repair status
├── VALIDATION_RESULTS.md     # spec v1.1 fixall pass: what was actually run
├── .dockerignore
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## 5. Prerequisites

- Windows 11 with PowerShell
- Python 3.12
- Node.js 18+ and npm
- Git
- For Android/Fire TV emulator or device testing: Android Studio + an
  Android/Fire TV emulator, or a physical Fire TV device in Developer Mode
  on the same network as your backend.

## 6. Backend setup (Windows PowerShell)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
copy ..\.env.example .env
```

To run the DynamoDB-repository test as well (mocked, no AWS account
needed):

```powershell
pip install -r requirements-dev.txt
```

### Run the backend

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Run the backend tests

```powershell
.venv\Scripts\Activate.ps1
python -m pytest -v
```

Expected: **116 passed** (22 from Gate 0 + 24 from Gate 1's strategy engine
and its API + 18 from Gate 2's watch evaluator and its API + 37 from
Gate 3's knowledge base, LLM client, explanation engine and its API, and
the per-alert cache + rate limiter that fix Gate 3's original caching/
rate-limiting limitation + 10 from `test_watch_dynamodb_repository.py`,
which fix Gate 2's original in-memory-only watch/alert storage
limitation + 5 from the proximity-based attribution guard that fixes
Gate 3's original "cannot catch misattributed numbers" limitation). See
§11 for the exact output captured during development.

## 7. Fire TV / React Native setup (Windows PowerShell)

```powershell
cd fire-tv
npm install
```

### Run tests and typecheck

```powershell
npm test
npm run typecheck
```

### Configure the backend API/WebSocket URL (`.env`)

Spec v1.1 repair 13 makes the backend base URL configurable without editing
`src/api/client.ts` by hand — it's read from a git-ignored `.env` file (via
the `react-native-dotenv` Babel plugin) with `.env.example` as the
documented template. The WebSocket URL used by `useRaceSocket` is derived
automatically from this same value (`http`→`ws`, `https`→`wss`), so there
is nothing separate to set for sockets.

```powershell
cd fire-tv
Copy-Item .env.example .env
notepad .env   # or any editor — set API_BASE_URL for your target below
```

| Target                                   | `API_BASE_URL`                  |
| ----------------------------------------- | -------------------------------- |
| Android / Fire TV **emulator**            | `http://10.0.2.2:8000`           |
| Physical Android/Fire TV **device**, same network as the backend | `http://<backend-host-LAN-IP>:8000` (find it with `ipconfig`) |
| iOS simulator                             | `http://localhost:8000`          |

After changing `.env`, restart Metro with a cache reset so the new value is
re-inlined into the bundle:

```powershell
npm start -- --reset-cache
```

### Native Android project (spec v1.1 repair 1)

`fire-tv/android/` is a complete, real React Native 0.74.5 Android project
— it is **not** a stub. It was generated from React Native 0.74.5's own
official template (`node_modules/react-native/template/android`, the exact
same scaffold `npx react-native init` produces for this RN version) and
then customized for Fire TV:

* Real Gradle wrapper (`gradlew`, `gradlew.bat`, `gradle/wrapper/*`) —
  `npm run android` / `npx react-native run-android` now resolves a real
  native project instead of failing with "Android project not found".
* `applicationId` / package `com.pitwallcopilot`, app name "Pit Wall
  Copilot" (`res/values/strings.xml`).
* `AndroidManifest.xml`: `LEANBACK_LAUNCHER` category (so the app appears
  on the Fire TV home-screen launcher row), `android.software.leanback`
  required, `android.hardware.touchscreen` **not** required, landscape
  orientation locked (`android:screenOrientation="landscape"`), a TV
  banner (`android:banner`), and no `RECORD_AUDIO` permission or
  microphone-button intent handling anywhere (the app never captures or
  overrides the Fire TV remote's mic button).
* An original, non-trademarked TV banner (320×180,
  `res/drawable/tv_banner.png`) and launcher icons at all five mipmap
  densities (`mdpi` through `xxxhdpi`) — a simple abstract gauge/dial mark
  in the app's own teal accent color, generated locally; no real racing
  series, team, or brand asset is referenced.
* Debug and release `buildTypes` (`app/build.gradle`), with the template's
  standard debug-only `debug.keystore` for local development builds.
* D-pad focus/navigation, focus rings, and TV-safe-area layout are all
  handled on the JS side (`FocusableButton.tsx`, `App.tsx`'s `padding: 32`
  content inset) — the native project itself only needs to *allow*
  keyboard/D-pad-driven focus, which is Android's default `View` focus
  behavior and requires no extra native code.

**What was verified locally, in this container (no Android SDK, no
device):**

```text
npx react-native config     # confirms sourceDir/packageName/applicationId/
                             # mainActivity resolve correctly to android/
```

Every `AndroidManifest.xml`, `strings.xml`, `styles.xml`, and
`colors.xml` file under `android/` was also parsed with a standalone XML
parser to confirm it is well-formed.

**What is `NOT RUN`, and why:** `cd android && ./gradlew assembleDebug` /
`gradle assembleDebug`. Two independent blockers, either one sufficient on
its own:

1. No Android SDK is installed in this container (no `ANDROID_HOME` /
   `sdkmanager` / `adb`), so there is nowhere for Gradle to resolve
   `compileSdk 34` / build tools against even if the build reached that
   step.
2. This container's outbound network is restricted to an explicit
   allowlist (npm/PyPI/GitHub and a few others); `services.gradle.org`
   (the Gradle wrapper's own distribution download), `repo.maven.apache.org`,
   `dl.google.com`, and `plugins.gradle.org` are all outside it (each
   returns HTTP 403 from the proxy on a direct `curl` probe). A real
   attempt with the system's pre-installed Gradle 8.14.3 confirmed this:
   it got as far as resolving `android/settings.gradle`, `android/build.gradle`,
   and `android/app/build.gradle` (no syntax errors), then failed at
   `org.gradle.toolchains.foojay-resolver-convention` plugin resolution —
   the first step needing the Gradle Plugin Portal — with
   `Plugin ... was not found in any of the following sources`.

**Exact manual verification command** (Windows PowerShell, on a machine
with Android Studio / the Android SDK and normal internet access
installed per <https://reactnative.dev/docs/environment-setup>):

```powershell
cd fire-tv\android
.\gradlew.bat assembleDebug
# Expect: BUILD SUCCESSFUL, output at
# fire-tv\android\app\build\outputs\apk\debug\app-debug.apk
```

### Run on an Android / Fire TV emulator

1. Start an Android TV (or Fire TV) emulator from Android Studio's Device
   Manager, or launch one from the command line:
   ```powershell
   emulator -avd <your-avd-name>
   ```
2. Confirm it's visible to ADB:
   ```powershell
   adb devices
   ```
3. From `fire-tv/`, run:
   ```powershell
   npx react-native run-android
   ```
4. Metro will start automatically in a new window; if not, run `npm start`
   in a second PowerShell terminal.

### Run on a physical Fire TV device (ADB over network)

Fire TV devices don't expose a USB port for ADB, so connect over the
device's network address instead:

1. On the Fire TV: **Settings → My Fire TV → About → \[device name\]**
   (press Select 7× if Developer Options isn't already visible), then
   **Settings → My Fire TV → Developer Options** → enable **ADB Debugging**
   and note the device's IP address (**Settings → My Fire TV → About →
   Network**).
2. From PowerShell, connect ADB to that IP (default Fire TV ADB port is
   `5555`):
   ```powershell
   adb connect <fire-tv-ip-address>:5555
   adb devices   # should list the device as "device", not "unauthorized"
   ```
   If it shows `unauthorized`, accept the "Allow USB/ADB debugging?" prompt
   that appears on the Fire TV screen, then re-run `adb devices`.
3. Because the backend runs on your development machine, not the Fire TV
   itself, set `API_BASE_URL` in `.env` (see above) to your machine's LAN
   IP — `10.0.2.2` (the emulator-only loopback alias) will **not** resolve
   from a physical device.
4. Deploy and run:
   ```powershell
   npx react-native run-android --deviceId <fire-tv-ip-address>:5555
   ```

### Build and install an APK directly (no Metro dev server)

Useful for a release-style build, or installing onto a device without
keeping a Metro terminal open:

```powershell
cd fire-tv\android
.\gradlew.bat assembleRelease
# Output: fire-tv\android\app\build\outputs\apk\release\app-release.apk

adb -s <fire-tv-ip-address>:5555 install -r `
  ..\app\build\outputs\apk\release\app-release.apk
```

Remember: a release build has `__DEV__ === false`, so `LOCAL_DEV_AUTH_TOKEN`
is never read (spec v1.1 repair 11) — Start/Pause/Reset will show the
pairing screen until the device is paired (see "Device pairing" below).

### Device pairing (spec v1.1 pass 3, correction 5)

A release Fire TV never holds the backend's master `DEMO_ACCESS_TOKEN` —
instead, it pairs once using a short-lived, one-time 6-digit code:

1. An administrator (anyone holding the master token) generates a code:

   ```powershell
   curl -X POST https://<BackendUrl>/v1/auth/pairing-codes `
     -H "X-Demo-Access-Token: <master token>"
   # {"code": "482913", "expires_in_seconds": 300}
   ```

2. Read (or send) that 6-digit code to whoever is at the Fire TV. It is
   valid for 5 minutes and can only be used once.
3. On the Fire TV's pairing screen, enter the code and press PAIR. The
   app exchanges it (`POST /v1/auth/pair`) for a device-specific token,
   which it stores via Android Keystore-backed secure storage
   (`react-native-keychain`) — never the master token, and never in
   plaintext.
4. "UNPAIR" on the dashboard revokes that device's token server-side
   (`POST /v1/auth/revoke`, self-revoke — no master token needed) and
   returns to the pairing screen. An administrator can also revoke a
   specific device remotely with the master token plus its `device_id`.

Local development without pairing remains available via
`LOCAL_DEV_AUTH_TOKEN` in `fire-tv/src/config/authToken.ts` (dev builds
only, `__DEV__`-gated — never available in a release build).

### Debugging ADB connection issues

```powershell
adb kill-server
adb start-server
adb devices                      # list all connected/authorized devices
adb -s <device-id> logcat *:S ReactNative:V ReactNativeJS:V   # app logs only
```

> **This repository was built and validated in a Linux container without
> an Android SDK, an Android/Fire TV emulator, or unrestricted internet
> access to Maven Central / Google's Maven repo / the Gradle Plugin
> Portal.** The React Native/TypeScript source, the real native
> `android/` project (§"Native Android project" above), and the Jest/RTL
> test suite were all written, installed, and verified (`npm test`,
> `npx tsc --noEmit`, `npx react-native config`) in that environment. The
> actual Gradle build (`gradlew assembleDebug`/`assembleRelease`), APK
> install, ADB deployment, and on-device/emulator Fire TV rendering have
> **not** been executed as part of this delivery — see §14 (Limitations)
> for exactly what to run yourself (the PowerShell commands above) to
> complete that verification.

## 7a. MCP server setup (Windows PowerShell)

`mcp_server/` is a separate Python package with its own virtual
environment (§2d explains why) — it does not share `backend/`'s venv.

```powershell
cd mcp_server
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### Run the MCP server tests

The test suite spins up a real backend subprocess (using
`backend\.venv`, which must already be set up per §6) for each test
session, so no separately-running backend is needed first.

```powershell
.venv\Scripts\Activate.ps1
python -m pytest -v
```

The interpreter used to launch that backend subprocess is resolved by
`scripts/testing_support/resolve_backend_python.py` — it does **not**
require a venv literally named `.venv` at `backend\.venv`. In order:
`PIT_WALL_BACKEND_PYTHON` (if set) → `backend\.venv\Scripts\python.exe`
(Windows) / `backend/.venv/bin/python` (Unix) if present → the current
interpreter, if it already has the backend's dependencies installed →
otherwise a clear failure (never a silent skip). To point at a
differently-located or differently-named backend environment:

```powershell
$env:PIT_WALL_BACKEND_PYTHON = "C:\path\to\some\python.exe"
python -m pytest -v
```

```bash
PIT_WALL_BACKEND_PYTHON=/path/to/some/python python -m pytest -v
```

Expected: **19 passed** — `tests/test_mcp_tools.py` (in-process
`call_tool`, per spec v1.1 repair 7's exactly-five-tools contract: each
tool call, invalid parameters, unknown race, unavailable knowledge base,
timeout) plus `tests/test_streamable_http_transport.py` (a real
`initialize` → `tools/list` → `tools/call` round trip over a real TCP
port, plus `/health`/`/ready`).

### Run the MCP server standalone

With a backend already running (§6's "Run the backend"):

```powershell
$env:PIT_WALL_API_BASE_URL = "http://localhost:8000"
.venv\Scripts\Activate.ps1
python server.py
```

Spec v1.1 repair 7 rewrote this server to serve **Streamable HTTP**, not
stdio (a stdio server can only be launched as a local subprocess by
whatever process started it, which a hosted Alexa+ skill or an AgentCore
Gateway cannot do) — this starts a real HTTP server on
`PIT_WALL_MCP_HOST`:`PIT_WALL_MCP_PORT` (default `127.0.0.1:9000`), with
`/health`, `/ready`, and the MCP endpoint itself at `/mcp`. It will
appear to hang with no output in an interactive terminal once started,
which is expected; connect a real MCP client to it (see
`alexa_plus_client/`, §7b below, for exactly that) rather than running it
bare to watch it print anything.

## 7b. Alexa+ setup (Windows PowerShell)

Spec v1.1 repair 10's primary architecture is:

```text
Alexa+  --Streamable HTTP MCP-->  five domain tools  -->  backend
```

**`alexa_plus_client/` (repository root) is the primary Alexa+
integration** — a simulated Alexa+ client that holds a real MCP session
against `mcp_server/`'s real Streamable HTTP transport (§7a) and
exercises all six required voice journeys over it. There is no Alexa+
developer/onboarding account available in this environment, so "simulated"
means exactly what it says — see `alexa_plus_client/README.md` for the
full, honest explanation of what is and isn't proven by it, and for what
a real Alexa+ console deployment would additionally require.

```powershell
cd alexa_plus_client
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### Run the simulated Alexa+ client's tests

Requires `backend\.venv` to already be set up (§6) — these tests launch a
real backend subprocess AND a real `mcp_server` Streamable HTTP server
for the duration of each test.

```powershell
.venv\Scripts\Activate.ps1
python -m pytest -v
```

Expected: **8 passed** — one per required primary voice journey (check
undercut, create watch, why now, PIT NOW vs WAIT 3 LAPS, a supported
regulation question, the canonical unsupported/hallucination-trap
regulation question, plus two driver-word-resolution edge cases), each a
genuine MCP `initialize` → `tools/list` → `tools/call` round trip against
a genuine backend.

### Legacy / optional fallback: the classic REST-based Alexa Skill

`legacy/alexa_skill_legacy_rest/` (moved from its original location at
the repository root, `legacy/alexa_skill_legacy_rest/`) is the pre-repair-10, traditional
`ask-sdk-core` Lambda skill that calls the backend REST API directly,
duplicating the orchestration the MCP tools now provide. It is kept only
as an explicitly optional fallback (spec v1.1 repair 10) — **do not**
treat it as, or deploy it as, the primary Alexa+ integration.

```powershell
cd legacy\alexa_skill_legacy_rest
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements-dev.txt
python -m pytest -v
```

Expected: **19 passed**. These are unit tests against the Lambda handler
directly — running them does not require a live backend, since the
outbound HTTP call is mocked in every test.

#### What deploying the legacy skill for real would additionally require

**Not done in this repository.** To actually run this on a real Echo
device:

1. Create a skill in the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask),
   upload `legacy/alexa_skill_legacy_rest/models/interaction-model.json`
   as its interaction model (or `ask-cli`'s `ask deploy`).
2. Package `legacy/alexa_skill_legacy_rest/lambda/lambda_function.py`
   with `legacy/alexa_skill_legacy_rest/lambda/requirements.txt`'s
   dependencies into an AWS Lambda deployment zip, and deploy it (a plain
   `pip install -r requirements.txt -t build/` + zip, or AWS SAM/CDK).
3. Set the Lambda's `PIT_WALL_API_BASE_URL` to a **publicly reachable**
   backend URL — `localhost` will not work from AWS; the backend would
   need to be deployed somewhere Lambda can reach it.
4. Set the Lambda's function ARN as the skill's endpoint in the
   Developer Console, and enable testing on a real device or the
   Console's simulator.

See `alexa_plus_client/README.md`'s own "What a real deployment would
additionally require" section for the equivalent steps for the
**primary** (MCP-based) integration instead.

## 8. Environment variables

See [`.env.example`](./.env.example) for the full list. Key ones:

| Variable | Default | Purpose |
|---|---|---|
| `STATE_REPOSITORY` | `memory` | `memory` or `dynamodb` |
| `DYNAMODB_TABLE_NAME` | `pit-wall-race-state` | Table name when using DynamoDB |
| `AWS_REGION` | `us-east-1` | Region for the DynamoDB client |
| `CORS_ALLOWED_ORIGINS` | local dev origins | Comma-separated allow-list, never `*` |
| `DEMO_ACCESS_TOKEN` | *(none — unset)* | Required in `X-Demo-Access-Token` header for start/pause/reset. No default is shipped; if unset (and `LOCAL_DEMO_AUTH_MODE` is not `true`), every mutating endpoint fails closed with `503 auth_not_configured`. |
| `LOCAL_DEMO_AUTH_MODE` | `false` | Local dev only: `true` makes the backend generate and print a fresh random token to stderr at startup instead of requiring `DEMO_ACCESS_TOKEN` to be set. Never enable in a shared/deployed environment. |
| `LLM_PROVIDER` | `template` | `template` (default, deterministic, no AWS) or `bedrock` |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-haiku-20240307-v1:0` | Bedrock model id, only used when `LLM_PROVIDER=bedrock` |
| `BEDROCK_REGION` | `us-east-1` | Region for the `bedrock-runtime` client |
| `KNOWLEDGE_BASE_DIR` | `docs/sporting-rules` | Folder of `.md` documents the Gate 3 KB stand-in loads |
| `EXPLAIN_RATE_LIMIT_PER_MINUTE` | `30` | Per-client-IP cap on `GET .../explain` calls per minute; `<= 0` disables it |

`mcp_server/` and `legacy/alexa_skill_legacy_rest/` read their own, separate environment
variables (not from this table's `.env` — they're separate processes).
`alexa_plus_client/` (the primary Alexa+ integration) is a library/example
client, not a standalone server process — its `race_id` and backend/MCP
URLs are ordinary constructor arguments (see `alexa_plus_client/README.md`),
not environment variables, so it has no row here.

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `PIT_WALL_API_BASE_URL` | `http://localhost:8000` | `mcp_server`, `legacy/alexa_skill_legacy_rest` | Base URL of the running backend |
| `PIT_WALL_DEMO_ACCESS_TOKEN` | *(none — unset)* | `mcp_server`, `legacy/alexa_skill_legacy_rest` | Forwarded as `X-Demo-Access-Token` for mutating calls. No default is bundled (spec v1.1 repair 11) — `create_strategy_watch`/`CreateWatchIntent` fail closed if this isn't set to match the backend's own `DEMO_ACCESS_TOKEN`. |
| `PIT_WALL_RACE_ID` | `demo-race-001` | `legacy/alexa_skill_legacy_rest` only | Which race the legacy voice skill targets |
| `PIT_WALL_MCP_HOST` / `PIT_WALL_MCP_PORT` | `127.0.0.1` / `9000` | `mcp_server` only | Where the Streamable HTTP MCP server listens |
| `PIT_WALL_MCP_ACCESS_TOKEN` | *(none — unset)* | `mcp_server` only | If set, required as `Authorization: Bearer <token>` on the MCP endpoint (spec v1.1 repair 10's real-deployment steps — see `alexa_plus_client/README.md`) |

## 9. Memory-repository mode (default)

No AWS credentials are required. State lives in process memory and is lost
on restart, which is expected and acceptable for local development and the
automated test suite.

## 10. DynamoDB mode

1. Provision a table matching [`infrastructure/README.md`](./infrastructure/README.md).
2. Set `STATE_REPOSITORY=dynamodb`, `DYNAMODB_TABLE_NAME`, `AWS_REGION` in
   `.env` (and configure standard AWS credentials via your usual mechanism
   — never commit them).
3. Run the backend as normal.

`STATE_REPOSITORY=dynamodb` now switches race state **and** watches/alerts
together (§14) — they share the one table, distinguished by sort-key
prefix (`STATE`/`CONTROL` for race state, `WATCH#...`/`ALERT#...`/
`ALERTIDX#...` for watches/alerts), so there is still only one table to
provision.

The automated test suite does **not** require this —
`test_dynamodb_repository.py` (race state) and
`test_watch_dynamodb_repository.py` (watches/alerts) both use `moto` to
simulate DynamoDB in-process.

## 11. API examples

```powershell
# Health check
curl http://localhost:8000/health

# Current race state (auto-initializes at lap 24 on first call)
curl http://localhost:8000/v1/races/demo-race-001/state

# Start the demo (mutating endpoints require the access token header)
curl -X POST http://localhost:8000/v1/demo/start `
  -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"

# Pause
curl -X POST http://localhost:8000/v1/demo/pause `
  -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"

# Reset to lap 24 with a new run_id
curl -X POST http://localhost:8000/v1/demo/reset `
  -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"

# Demo lifecycle status
curl http://localhost:8000/v1/demo/status

# Gate 1 — deterministic undercut calculation (candidate vs target)
curl -X POST http://localhost:8000/v1/races/demo-race-001/strategy/undercut `
  -H "Content-Type: application/json" `
  -d '{\"candidate_driver\": \"CAR_04\", \"target_driver\": \"CAR_03\"}'

# Gate 1 — PIT NOW vs WAIT-k-laps counterfactual for one driver
curl -X POST http://localhost:8000/v1/races/demo-race-001/strategy/simulate `
  -H "Content-Type: application/json" `
  -d '{\"driver_id\": \"CAR_04\", \"wait_laps\": 3}'

# Gate 2 — create a persistent undercut watch (requires the access token)
curl -X POST http://localhost:8000/v1/races/demo-race-001/watches `
  -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>" `
  -H "Content-Type: application/json" `
  -d '{\"candidate_driver\": \"CAR_04\", \"target_driver\": \"CAR_03\"}'

# Gate 2 — list active watches for the current run
curl http://localhost:8000/v1/races/demo-race-001/watches

# Gate 2 — list alerts (newest first), optionally scoped to one watch
curl "http://localhost:8000/v1/races/demo-race-001/alerts?watch_id=<watch_id>"

# Gate 2 — "Why now?": fetch one alert's immutable triggering snapshot
curl http://localhost:8000/v1/races/demo-race-001/alerts/<alert_id>

# Gate 2 — delete a watch (requires the access token)
curl -X DELETE http://localhost:8000/v1/races/demo-race-001/watches/<watch_id> `
  -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"

# Gate 3 — explain an alert in natural language (no auth required, it's a read)
curl http://localhost:8000/v1/races/demo-race-001/alerts/<alert_id>/explain
```

### Actual captured output (development machine, this container)

```
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","app_version":"0.1.0-gate0","timestamp":"2026-09-16T12:55:18.174675Z"}

$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/state
lap= 24 version= 0 run_id= cd637fd3-86c8-47fa-b36f-6b6137d385b0 drivers=['CAR_03','CAR_04','CAR_07','CAR_11']

$ curl -s -X POST http://127.0.0.1:8000/v1/demo/start -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"
{"status":"RUNNING","race_id":"demo-race-001","run_id":"b54a88e8-...","current_lap":24,"state_version":0}

# ...after ~5 seconds of real time (2s tick interval)...
$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/state
lap= 28 version= 4

$ curl -s -X POST http://127.0.0.1:8000/v1/demo/pause -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"
{"status":"COMPLETED", ...}   # already completed by lap 28; pause is a documented no-op once COMPLETED

$ curl -s -X POST http://127.0.0.1:8000/v1/demo/reset -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"
lap= 24 version= 0 run_id= 73501cef-b840-437f-9fad-7c7643b29feb   # new run_id, confirms reset

$ curl -s -i http://127.0.0.1:8000/v1/races/nope/state | head -1
HTTP/1.1 404 Not Found

$ curl -s -i -X POST http://127.0.0.1:8000/v1/demo/start | head -1
HTTP/1.1 401 Unauthorized   # missing access token

# --- Gate 1: strategy endpoints, captured against the live server ---

$ curl -s -X POST http://127.0.0.1:8000/v1/races/demo-race-001/strategy/undercut \
    -H "Content-Type: application/json" \
    -d '{"candidate_driver": "CAR_04", "target_driver": "CAR_03"}'
# At lap 24 (initial state):
window=CLOSED expected_margin_ms=-320 confidence.probability_ahead=0.455 confidence.trials=200

# ...after the demo runs forward to lap 27...
window=OPEN expected_margin_ms=1385 confidence.probability_ahead=0.775
# This is the exact canonical demo narrative from spec section 2: the
# undercut window opens as Car 4 closes the gap on Car 3.

$ curl -s -X POST http://127.0.0.1:8000/v1/races/demo-race-001/strategy/simulate \
    -H "Content-Type: application/json" \
    -d '{"driver_id": "CAR_04", "wait_laps": 3}'
driver_id=CAR_04 pit_now_lap=25 wait_lap=28 delta_ms=5745
confidence.median_margin_ms=6195 confidence.probability_ahead=0.975

$ curl -s -i -X POST http://127.0.0.1:8000/v1/races/demo-race-001/strategy/undercut \
    -H "Content-Type: application/json" \
    -d '{"candidate_driver": "CAR_99", "target_driver": "CAR_03"}' | head -1
HTTP/1.1 422 Unprocessable Entity   # unknown driver, explicit rejection — never a guess

$ curl -s -i -X POST http://127.0.0.1:8000/v1/races/nope/strategy/undercut \
    -H "Content-Type: application/json" \
    -d '{"candidate_driver": "CAR_04", "target_driver": "CAR_03"}' | head -1
HTTP/1.1 404 Not Found

# --- Gate 2: watch + alert endpoints, captured against the live server ---

$ curl -s -X POST http://127.0.0.1:8000/v1/races/demo-race-001/watches \
    -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>" -H "Content-Type: application/json" \
    -d '{"candidate_driver": "CAR_04", "target_driver": "CAR_03"}'
watch_id=watch-a9e0b121-... status=CLOSED   # baseline at lap 24, no alert published

$ curl -s -X POST http://127.0.0.1:8000/v1/demo/start -H "X-Demo-Access-Token: <YOUR_LOCAL_DEMO_ACCESS_TOKEN>"
{"status":"RUNNING", ...}

# ...demo advances autonomously from lap 24 toward lap 28...
$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/watches/watch-a9e0b121-...
status=OPEN   # the watch flipped on its own, with no client re-request

$ curl -s "http://127.0.0.1:8000/v1/races/demo-race-001/alerts?watch_id=watch-a9e0b121-..."
alert_count=1 first_alert_id=alert-c233c5bf-...
triggering_snapshot: lap=27 state_version=3
deterministic_result.confidence.probability_ahead=0.775 expected_margin_ms=1385

$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/alerts/alert-c233c5bf-...
# same snapshot: lap=27 state_version=3 (fetched immediately after the alert fired)

# ...race continues to COMPLETED at lap 28, state_version 4...
$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/state
lap=28 state_version=4

$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/alerts/alert-c233c5bf-...
# re-fetched AFTER the live race moved on one more lap — snapshot is
# UNCHANGED: lap=27 state_version=3. This is the "Why now?" contract
# (spec 49): the alert's evidence never mutates once written.

$ curl -s -i -X POST http://127.0.0.1:8000/v1/races/demo-race-001/watches \
    -H "Content-Type: application/json" \
    -d '{"candidate_driver": "CAR_04", "target_driver": "CAR_03"}' | head -1
HTTP/1.1 401 Unauthorized   # missing access token
```

This exact sequence — reset, create watch (CLOSED), start, watch
autonomously flips to OPEN, exactly one alert appears, the alert's
snapshot survives the race moving on — was re-captured against a live
server after spec v1.1 correction 6 retuned
`scenarios/scenario-demo-01.json`'s `watch_thresholds.open_probability`
(0.65 -> 0.72) to move the transition from lap 26 to the spec-required
lap 27: watch opens at lap 27 / state_version 3, `probability_ahead=0.775`,
`expected_margin_ms=1385`, exactly one alert. Because the calculation is
fully deterministic (fixed `model_seed`, no wall-clock-dependent inputs),
this outcome does not vary run to run by construction — the repeatability
claim rests on that determinism (also exercised directly by
`backend/tests/test_strategy_engine.py`'s per-lap parametrized tests and
`scripts/golden_demo.py`, both of which assert this exact lap/version/
probability), not on having replayed the live sequence an arbitrary
number of times in this session.

```
# --- Gate 3: explanation endpoint, captured against the live server ---

$ curl -s http://127.0.0.1:8000/v1/races/demo-race-001/alerts/alert-c233c5bf-.../explain
{
  "explanation_id": "expl-a3ff0f37-...",
  "alert_id": "alert-c233c5bf-...",
  "narrative": "On lap 27, the undercut watch on CAR_04 against CAR_03 moved
    from CLOSED to OPEN. Pit Wall Copilot's deterministic simulation projects
    CAR_04 finishing the comparison window 1385ms ahead of CAR_03 if CAR_04
    pits now and CAR_03 responds the following lap, keeping the undercut
    window OPEN. Across 200 seeded Monte Carlo trials, CAR_04 comes out
    ahead in 77.5% of outcomes (P10=-1281ms, P90=4038ms). Relevant
    sporting-rules context: The Undercut, Pit Lane Procedure and Pit Loss,
    Tire Compounds and Degradation.",
  "citations": [
    {"doc_id": "sr-undercut-001", "title": "The Undercut", "score": 3, ...},
    {"doc_id": "sr-pitlane-001", "title": "Pit Lane Procedure and Pit Loss", "score": 1, ...},
    {"doc_id": "sr-tires-001", "title": "Tire Compounds and Degradation", "score": 1, ...}
  ],
  "grounded_facts": {
    "candidate": "CAR_04", "target": "CAR_03", "expected_margin_ms": "1385",
    "probability_ahead": "77.5%", "triggering_lap": "27", ...
  },
  "llm_provider": "template",
  "model_id": "template-v1",
  "grounding_warnings": []
}

$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/races/demo-race-001/alerts/nope/explain
404
```

Every number in that narrative — `1385`, `77.5%`, `200`, `27`, `-1281`,
`4038` — traces back to `grounded_facts`, and `grounding_warnings` is
empty, confirming the explanation does not contradict (or add to) the
deterministic result it describes. `backend/tests/test_explanation_engine.py`
has the adversarial version of this same check: a fake LLM client that
deliberately states an ungrounded number, used to prove
`grounding_warnings` actually catches that case rather than always
passing clean.

```
# --- Gate 4: MCP server, captured live over the REAL stdio transport ---
# (backend running separately on 127.0.0.1:8010; this uses the official
# `mcp` Python client SDK, not this repo's own test suite's call_tool)

$ python3 -c "
    ... connects via mcp.client.stdio.stdio_client + mcp.ClientSession ...
    tools = await session.list_tools()
    print([t.name for t in tools.tools])
    result = await session.call_tool('get_race_state', {'race_id': 'demo-race-001'})
    ...
"
tools over real stdio transport: ['get_race_state', 'calculate_undercut',
  'simulate_strategy', 'create_strategy_watch', 'list_strategy_watches',
  'list_alerts', 'get_alert', 'explain_alert']
lap over real stdio: 24
undercut window over real stdio: CLOSED -320
```

```
# --- Gate 4: Alexa+ Lambda handler, captured live against a real backend ---
# (invoked with a raw Alexa IntentRequest event dict, exactly as AWS
# Lambda would call lambda_function.handler — NOT the mocked unit tests)

$ PIT_WALL_API_BASE_URL=http://127.0.0.1:8020 python3 -c "
    import lambda_function
    response = lambda_function.handler(event, None)   # CheckUndercutIntent, car four vs car three
    print(response['response']['outputSpeech']['ssml'])
"
<speak>The undercut window for car 4 against car 3 is closed. The
simulation projects car 4 finishing 320 milliseconds behind, with a 45.5
percent chance across two hundred simulated trials.</speak>

# ...after creating a watch and letting the live demo run it open...
$ PIT_WALL_API_BASE_URL=http://127.0.0.1:8020 python3 -c "
    response = lambda_function.handler(explain_latest_alert_event, None)
    print(response['response']['outputSpeech']['ssml'])
"
<speak>On lap 27, the undercut watch on CAR_04 against CAR_03 moved from
CLOSED to OPEN. Pit Wall Copilot's deterministic simulation projects
CAR_04 finishing the comparison window 1385ms ahead of CAR_03 if CAR_04
pits now and CAR_03 responds the following lap, keeping the undercut
window OPEN. Across 200 seeded Monte Carlo trials, CAR_04 comes out ahead
in 77.5% of outcomes (P10=-1281ms, P90=4038ms). Relevant sporting-rules
context: The Undercut, Pit Lane Procedure and Pit Loss, Tire Compounds
and Degradation.</speak>
```

Both transcripts are the same live backend results already validated in
§11's earlier sections (`-320`/45.5%/CLOSED at the baseline lap;
`1385`/77.5%/OPEN at lap 27) — reached this time through the MCP tool
layer and the Alexa Lambda handler respectively, rather than curl,
confirming both adapters faithfully carry the deterministic numbers
through without altering them.

## 12. Reset procedure

Call `POST /v1/demo/reset` with the `X-Demo-Access-Token` header (or press
**RESET** on the Fire TV dashboard). This:

1. Cancels any in-flight simulator loop for the current run.
2. Clears persisted state and control data for the race.
3. Generates a brand-new `run_id`.
4. Restores lap 24 at `state_version` 0.
5. Publishes a fresh control state (`STOPPED`), ready for a new `start`.

No manual database editing is ever required — this matches the hackathon
requirement that judges/developers can return the scenario to a known
state immediately.

## 13. Troubleshooting: `localhost` vs `10.0.2.2`

- The **Android emulator** runs in its own virtual network. From inside
  it, `localhost` refers to the emulator itself, not your host machine.
  Use `http://10.0.2.2:8000` (the emulator's alias for the host loopback
  interface) instead — set this in `fire-tv/.env`'s `API_BASE_URL` (§7);
  it's also the built-in fallback in `fire-tv/src/api/client.ts` if `.env`
  is missing entirely.
- A **physical Fire TV device** is a separate machine on your network —
  use your backend machine's LAN IP (e.g. `http://192.168.1.20:8000`), not
  `localhost` or `10.0.2.2`. Again, set this via `fire-tv/.env`, not by
  editing `client.ts` (spec v1.1 repair 13).
- If you see `DISCONNECTED` on the dashboard, first confirm the backend is
  reachable from the device/emulator's network context, then check
  `CORS_ALLOWED_ORIGINS` includes the app's origin.
- After changing `fire-tv/.env`, restart Metro with `npm start --
  --reset-cache` — the value is inlined into the JS bundle at build time,
  not read at runtime, so a plain reload won't pick up the change.

## 13a. Degraded modes (spec v1.1 Section 29 / repair 14)

The system is required to fail explicitly, with specific literal text,
rather than inventing an answer when a dependency is down. Implemented as
follows:

| Condition | Where enforced | Exact response |
| --------- | -------------- | --------------- |
| Bedrock (LLM) unavailable | `backend/app/services/explanation_engine.py`: `explain_alert` catches `LLMClientError` from the configured `LLMClient` | `ExplanationResult.narrative = "Strategy explanation temporarily unavailable."`, `ExplanationResult.degraded = True`. Not cached, so a later request retries once Bedrock recovers. Everything else on `/explain` (grounded_facts, citations) is still returned — only the narration degrades. |
| Regulation source / Knowledge Base unavailable | `backend/app/services/regulation_service.py`: `RegulationService.is_available` is `False` when the corpus directory can't be found; `backend/app/services/agent_service.py`: `AgentQueryService.query` checks it before falling back to the ordinary "unsupported" response | `AgentQueryResult.narrative = "Regulation source unavailable. I won't infer the rule."` `GET /v1/regulations` and the MCP `get_regulation` tool also carry `available: false` on every answer, distinct from an ordinary `found: false`. |
| Strategy engine failure | `backend/app/api/routes.py`: `calculate_undercut`, `simulate_strategy` and `create_strategy_watch` each catch any exception other than `StrategyValidationError` | HTTP 503 with `{"error": "strategy_engine_unavailable", "message": "STRATEGY MODEL UNAVAILABLE"}`. No LLM is invoked to paper over the failure. |
| WebSocket disconnected | `fire-tv/src/hooks/useRaceSocket.ts` (connection-state + exponential reconnect: 250/500/1000/2000ms) + `fire-tv/src/hooks/useRaceState.ts` (unconditional 2s REST poll that keeps running underneath regardless of socket state) | `App.tsx` shows a `DISCONNECTED` connection badge and a `DISCONNECTED FROM PIT WALL BACKEND — retrying…` banner; the dashboard keeps refreshing every 2s via REST while the socket reconnects. |
| Race data stale (`current_time - generated_at > 5s`) | Backend: `app/domain/models.py`'s `is_state_stale`, enforced in `app/services/watch_evaluator.py`'s `evaluate()` (returns no alerts for a stale snapshot). Fire TV: `useRaceSocket.ts`'s `isStale` (same 5000ms threshold, based on time since the last message of any kind) | Backend suppresses new proactive alerts for a stale snapshot. Fire TV renders a `DATA STALE` badge (`StaleDataBadge`, `fire-tv/src/components/StatusBadges.tsx`) next to the connection badge whenever `isStale` is true. |

Tests: `backend/tests/test_explanation_engine.py` (Bedrock), `test_regulation_service.py`/`test_agent_service.py`/`test_regulation_api.py` (Knowledge Base), `test_strategy_api.py` (strategy engine), `test_watch_evaluator.py` (stale suppression); `fire-tv/src/__tests__/useRaceSocket.test.ts`, `StatusBadges.test.tsx` and `AppFocus.test.tsx` (WebSocket disconnect + DATA STALE badge).

## 14. Current limitations

- **Fire TV / Android device verification was not performed** in the
  environment used to build this repository — it has no Android SDK, no
  Android/Fire TV emulator, and no network access to Maven Central/Google's
  Maven repo/the Gradle Plugin Portal (spec v1.1 repair 1's §"Native
  Android project" above has the full detail, including the exact `curl`
  probes and Gradle error confirming the network restriction). The real
  native `android/` project exists (generated from React Native 0.74.5's
  own official template and customized for Fire TV — `LEANBACK_LAUNCHER`,
  no-touchscreen-required, landscape lock, TV banner/icons, debug+release
  build types) and `npx react-native config` confirms it resolves
  correctly, but `gradlew assembleDebug`/`assembleRelease`, the APK
  install, and on-device/emulator rendering all still need to be run by
  whoever has that tooling. See §7's "Native Android project" and
  "Build and install an APK directly" subsections for the exact commands.
- DynamoDB mode was verified with `moto` (a mocked AWS environment), not
  against a real AWS account. The conditional-write logic is identical
  either way; only the live network call is untested against a real table.
- ~~Some Jest tests print benign React "not wrapped in act(...)" warnings
  from async state updates that resolve after the assertion has already
  run.~~ — **fixed.** The real cause was **not** state updates landing on
  unmounted components (that was the first hypothesis, and it was wrong —
  adding `isMountedRef` guards alone did not stop the warnings). The
  actual cause was the *tests* returning before the full async chain
  (press → fetch → `response.text()` → `JSON.parse` → the hook's
  `onChanged`/`setIsBusy(false)`) had settled, so a state update that
  happened perfectly normally — while the component was still mounted —
  landed after the test function's promise had already resolved, outside
  any `act(...)` scope. `AppFocus.test.tsx` and `ResetControl.test.tsx`
  now use `waitFor` to poll a real observable condition (the connection
  badge's text, the reset button's `accessibilityState.disabled` going
  `true` then back to `false`) until the whole chain has actually
  finished, instead of guessing a number of `await Promise.resolve()`
  ticks. Separately, `useRaceState.ts` and `useDemoControls.ts` were
  hardened with `isMountedRef` guards on every `setState` call that
  happens after an `await` — a genuine robustness fix in its own right
  (an in-flight request settling just after unmount should not touch
  state), even though it turned out not to be what was causing the test
  warnings. Verified with `npx tsc --noEmit` (clean) and four consecutive
  clean `npx jest` runs — `Test Suites: 3 passed, 3 total`,
  `Tests: 4 passed, 4 total`, zero act() warnings in any run. (`npm run
  lint` could not be verified — this repository has no `.eslintrc` /
  flat-config file checked in, so ESLint has nothing to load; that gap
  predates this fix and is unrelated to it.)
- No WebSockets yet — the dashboard polls every 2 seconds, per the Gate 0
  instruction to defer WebSockets until this phase is complete and tested.
- Vega OS compatibility has not been investigated (explicitly deferred by
  the specification).
- ~~**Gate 1's strategy endpoints are plain REST, not MCP.**~~ — **fixed
  in Gate 4.** `mcp_server/server.py` now exposes `calculate_undercut` and
  `simulate_strategy` as real MCP tools (alongside 6 others — §2d), with
  the exact tool names and input shapes spec section 22 defines, tested
  over the actual MCP stdio JSON-RPC transport
  (`mcp_server/tests/test_mcp_tools.py`). The REST endpoints
  (`/v1/races/{race_id}/strategy/...`) still exist unchanged — MCP is an
  additional way to reach the same underlying `StrategyEngine`, not a
  replacement for the REST API the Fire TV dashboard uses.
- **The "next compound after a pit stop" choice is a documented demo
  default** (`StrategyEngine._next_compound`: switch to SOFT unless
  already on SOFT, then MEDIUM), not a strategy recommendation — Gate 1
  has no concept of tire allocation/availability constraints. This is
  still true in Gate 2, since the watch evaluator delegates to the same
  `calculate_undercut`.
- `simulate_strategy` compares a single driver's own PIT NOW vs WAIT
  timelines; it does not model a second car's reaction, unlike
  `calculate_undercut` which explicitly does (this matches the spec's
  distinct definitions of the two tools in section 22).
- ~~**Watch/alert storage is in-memory only** ... A DynamoDB-backed
  implementation is not included for watches/alerts~~ — **fixed.**
  `STATE_REPOSITORY` (memory | dynamodb) now selects watches/alerts too,
  not just race state — `app/repositories/factory.py`'s
  `build_watch_repositories` mirrors `build_repository`'s existing
  selection, and `app/repositories/watch_dynamodb.py` implements
  `DynamoDBWatchRepository`/`DynamoDBAlertRepository` against the SAME
  table race state already uses (`DYNAMODB_TABLE_NAME`), distinguished by
  sort-key prefix (`WATCH#...`, `ALERT#...`, `ALERTIDX#...`) — no second
  table to provision. Spec 49's idempotency guarantee (an alert transition
  recorded exactly once per `idempotency_key`) is enforced with a
  DynamoDB conditional write on the `ALERTIDX#` item
  (`attribute_not_exists(pk)`), the same conditional-write pattern
  `DynamoDBRaceStateRepository` already used for stale-write rejection —
  and, unlike the in-memory repository's `asyncio.Lock`, this is actually
  safe across multiple backend processes, which matters once the
  "DynamoDB Streams → Lambda" trigger below is a real, possibly-concurrent
  Lambda rather than one in-process callback. Verified with **10 new
  tests** in `test_watch_dynamodb_repository.py` (via `moto`, no real AWS
  account needed) that drive the actual `WatchEvaluator` + `StrategyEngine`
  pipeline against the DynamoDB-backed repositories — including a replay
  test and a direct `record_transition` duplicate-call test — not just
  hand-built fixtures round-tripping through get/put. Like the existing
  race-state DynamoDB path, this has been verified with `moto`, not
  against a real AWS account (no AWS access in this environment); the
  conditional-write logic is identical either way.
- **The "DynamoDB Streams → Lambda" trigger is emulated in-process**, not
  backed by real AWS Streams infrastructure: `SimulatorService` invokes an
  `on_state_committed` async callback synchronously, in the same process,
  immediately after each accepted write. This preserves the same
  "evaluate once, right after commit" semantics the real architecture
  requires, but is not a deployed Streams+Lambda pipeline — that
  infrastructure wiring (an actual DynamoDB Streams-subscribed Lambda) is
  still deferred to a later gate; it needs a real AWS deployment to
  exercise, unlike the storage layer above, which needed only code.
- **No real-time push to Fire TV.** Alerts are exposed via
  `GET /v1/races/{race_id}/alerts`, which the client is expected to poll
  (mirroring the existing race-state polling pattern) — there is no
  WebSocket or push-notification channel yet, and that part remains true.
  ~~The Fire TV app itself was not modified in Gate 2; wiring the
  dashboard to poll/display alerts is still open work.~~ — **fixed.**
  `fire-tv/src/hooks/useAlerts.ts` polls
  `GET /v1/races/{race_id}/alerts` every 4 seconds (deliberately slower
  than the 2-second race-state poll — alerts are lower-frequency events,
  not a per-lap live value), following the exact same
  poll-with-`isMountedRef`-guard pattern as `useRaceState.ts`. A new
  `fire-tv/src/components/AlertsPanel.tsx` renders the results — newest
  first (the backend already returns them that way), one card per alert
  showing the watch-state transition (window opened/closed), the
  candidate/target driver pair, the expected margin, and the confidence
  model's `probability_ahead` — and is wired into `App.tsx` below the
  existing standings table and controls. A failed poll surfaces a quiet
  inline error without blanking out the last-known alerts, since this is
  a best-effort secondary surface, not the primary race-state display.
  New types (`Alert`, `UndercutResult`, `ConfidenceResult`,
  `WatchStatus`) were added to `fire-tv/src/api/types.ts` mirroring
  `backend/app/domain/watch_models.py`/`strategy_models.py` exactly, and
  `api.listAlerts` was added to `fire-tv/src/api/client.ts`. Tested with
  9 new Jest tests (`useAlerts.test.ts`: initial poll population, replace
  rather than merge on a later poll, error surfaced without clearing
  prior alerts; `AlertsPanel.test.tsx`: empty state, one card per alert
  with correct content, error shown alongside existing alerts) plus
  updates to the existing `AppFocus.test.tsx`/`ResetControl.test.tsx`
  fetch mocks so the new alerts poll doesn't collide with their existing
  race-state mock (it now branches on URL and returns `[]` for the
  alerts endpoint). Full suite: 10/10 passing, confirmed clean across
  multiple consecutive runs with zero act() warnings; `tsc --noEmit`
  clean. What this does **not** cover: an actual on-device/emulator
  render of the new panel (see the Fire TV/Android device-verification
  limitation above, unchanged by this fix) and any push mechanism — the
  panel is still polling-based, same as the rest of the dashboard.
- **`WatchType` only supports `UNDERCUT`** — the enum is structured to
  admit future watch types (e.g. tire-degradation or pit-window watches)
  but only the undercut evaluation path is implemented.
- Only one `WatchEvaluator.evaluate()` call runs per committed state
  change, and it evaluates every active watch for that race sequentially
  in-process; this has not been load-tested against a large number of
  concurrent watches, which is a reasonable question for a later gate but
  out of scope for this hackathon-scale demo.
- **The Knowledge Base is a small, keyword-ranked local stand-in, not a
  real Bedrock Knowledge Base.** Four short fictional documents, ranked by
  exact/substring keyword overlap against a `Keywords:` header line — not
  semantic/embedding-based retrieval over a large corpus. It is
  behind the same `retrieve(...)` interface a real Bedrock Knowledge Base
  client would implement (`knowledge_base.py`'s module docstring spells
  out exactly what would need to change), but it has not been swapped
  for one.
- **`BedrockLLMClient` is implemented but not exercised against a real
  AWS account in this environment** — there is no AWS access here, the
  same constraint already documented for the DynamoDB repository. It is
  covered by a test that confirms it fails fast with a wrapped
  `LLMClientError` when it cannot reach AWS, not by a test that confirms
  a real Bedrock model produces a good narrative. Anyone running this
  with real `AWS credentials` + `LLM_PROVIDER=bedrock` + a valid,
  access-granted `BEDROCK_MODEL_ID` would be the first real exercise of
  that path.
- **The grounding guard is a numeric heuristic, not a full semantic
  fact-check.** It verifies every number in the narrative traces back to
  a given fact or citation, which catches invented margins, percentages,
  or lap numbers — the failure mode a hallucinating LLM is most likely to
  produce for this kind of numeric content. ~~It cannot catch a narrative
  that uses only grounded numbers but misattributes them (e.g. swaps
  which driver a real number belongs to); that would need a stronger
  structured-output/schema-constrained approach, which is a reasonable
  next hardening step before pointing this at a real model in
  production.~~ — **partially fixed.** `ExplanationEngine._check_attribution`
  (`app/services/explanation_engine.py`) adds a proximity-based check on
  top of the existing numeric-membership check: for the two grounded
  facts that are inherently "about" one specific driver rather than the
  pair or the watch as a whole (`candidate_tire_age_laps` /
  `target_tire_age_laps`), it finds every mention of the candidate's and
  target's driver IDs in the narrative and, for each occurrence of one of
  those two values, checks that the *nearest* driver-ID mention (within
  60 characters) is the correct owner — flagging e.g. "CAR_03's tires are
  18 laps old" when 18 is actually CAR_04's figure, even though 18 is a
  perfectly real, grounded number and the old numeric-only check would
  have passed it. This is deliberately narrow, not a general
  entity-linking system, and the "partially" is honest, not a hedge: it
  only covers the two driver-specific tire-age facts (the only ones this
  engine currently grounds that are provably owned by one driver — margin,
  probability, and the rest describe the pair or the watch, not one
  driver alone, so there is nothing for proximity to disambiguate them
  against), it skips a value that's identical for both drivers (no way to
  tell a correct attribution from a swapped one when the numbers
  coincide), and it skips a numeric mention with no driver-ID mention
  within the proximity window (silence over a guess). A true general
  fact-check would still need the stronger structured-output/
  schema-constrained approach noted above; this closes the specific,
  highest-value gap (wrong driver, real number) cheaply and
  deterministically instead. Verified with 5 new tests in
  `test_explanation_engine.py`: a swapped-attribution narrative is
  flagged (`test_attribution_guard_flags_a_grounded_number_pinned_to_the_wrong_driver`),
  a correctly-attributed one is not
  (`test_attribution_guard_passes_a_correctly_attributed_narrative`), and
  three tests confirming the guard's own deliberate blind spots behave as
  documented (identical-value skip, no-driver-ID-mentioned skip,
  outside-proximity-window skip) rather than silently mis-flagging those
  cases. Full backend suite: 111→116 tests, all passing.
- ~~**No caching or rate-limiting on `/explain`.**~~ — **fixed.**
  `ExplanationEngine` now caches its result per `alert_id`
  (`app/services/explanation_engine.py`): the first call generates and
  grounds a narrative and every later call for that same alert returns
  the identical cached `ExplanationResult` (same `explanation_id`, same
  narrative, same `created_at`) rather than regenerating one — correct,
  not just cheaper, since an alert's grounded facts are immutable once
  written, and it also means a real (non-deterministic) Bedrock model
  can't give two differently-worded answers for the same unchanged facts.
  Separately, the route itself is now rate-limited per client IP via a
  fixed-window limiter (`app/services/rate_limiter.py`,
  `EXPLAIN_RATE_LIMIT_PER_MINUTE`, default 30/minute, `<= 0` disables it),
  returning `429` with a `Retry-After` header once exceeded — this caps
  how many *distinct* alerts one client can force-generate explanations
  for in a burst, which the cache alone doesn't address. Both are covered
  by real tests: `test_explain_alert_is_cached_by_alert_id` and
  `test_explain_alert_cache_hit_does_not_call_the_llm_again` (proving the
  LLM is actually skipped, not just that the output looks the same) in
  `test_explanation_engine.py`; `test_explain_alert_rate_limits_a_client_that_exceeds_the_per_minute_cap`
  in `test_explanation_api.py`; and `test_rate_limiter.py`'s standalone
  unit tests for the limiter itself. Both are deliberately in-process,
  single-instance implementations (an in-memory dict, no eviction/TTL on
  the cache; a fixed, not sliding, window on the limiter) — an honestly
  scoped choice for a single-process hackathon demo, documented in each
  module's own docstring, not a stand-in for a distributed cache or a
  production-grade limiter.
- Explanation was REST-only as of Gate 3; Gate 4 now exposes it through
  both the MCP `explain_alert` tool and the Alexa `ExplainLatestAlertIntent`
  voice intent (see §2d) — both call the same, unmodified `/explain`
  endpoint.

**Gate 4's own limitations** (in addition to carrying forward everything
above, since Gate 4 changes nothing underneath it):
- **Neither `mcp_server/` nor `alexa_plus_client/` (the current, primary
  pieces — spec v1.1 repairs 7/10) nor `legacy/alexa_skill_legacy_rest/`
  has been deployed to real infrastructure or exercised against a real
  device/host** — no real Alexa+ onboarding/account, no Alexa Developer
  Console skill, no real AWS Lambda, no real Echo/Alexa+ device or voice
  input. All were genuinely exercised end-to-end against a live backend
  (§11's transcripts are real, captured output, not fabricated;
  `alexa_plus_client/`'s 8 tests are a real Streamable HTTP MCP round
  trip) — but that is not the same as a real deployment. §7a/§7b and
  `alexa_plus_client/README.md` spell out exactly what remains and why.
- **The Alexa skill only targets one hardcoded race** (`PIT_WALL_RACE_ID`,
  default `demo-race-001`) and has no per-user session or account
  linking — every caller shares the same demo access token as the REST
  API, which is a hackathon-appropriate simplification (spec 52), not a
  production auth model.
- ~~**`ExplainLatestAlertIntent` always explains the single newest alert**
  across the whole race, not "the alert for the watch we were just
  discussing"~~ — **fixed.** `CreateWatchIntentHandler` now stores
  `last_watch_id`/`last_watch_candidate`/`last_watch_target` in Alexa
  session attributes and keeps the session open
  (`should_end_session=False`) specifically so that state survives into a
  follow-up turn; `ExplainLatestAlertIntentHandler` reads those attributes
  and, when present, scopes the alerts query to that specific watch
  (`GET /v1/races/{id}/alerts?watch_id=...`) instead of the race-wide
  newest alert, and says so explicitly ("the watch on car 4 against car 3
  hasn't changed status yet") rather than a generic "no alerts yet" when
  that specific watch hasn't fired. Covered by
  `test_explain_latest_alert_after_create_watch_scopes_to_that_watch` and
  `test_explain_latest_alert_after_create_watch_with_no_change_yet` in
  `legacy/alexa_skill_legacy_rest/tests/test_lambda_function.py`, which chain a real
  `CreateWatchIntent` dispatch into a real `ExplainLatestAlertIntent`
  dispatch, carrying the first response's session attributes into the
  second request's `Session` exactly as Alexa would. At the time, this
  narrowed but did not remove the underlying simplification — the session
  only ever tracked the one most recently created watch. **Gate 5 removed
  that remaining simplification** (§2e.1): the session now tracks every
  watch created in it, and an optional `WatchDriver` slot lets "explain
  the car four watch" disambiguate between them by name, with the
  most-recent watch remaining the default when the slot is omitted.
- **The MCP server's `list_strategy_watches`/`list_alerts` tools wrap
  their array results in a keyed object** (`{"watches": [...]}`,
  `{"alerts": [...]}`) rather than returning a bare JSON array — this was
  a deliberate fix (documented in `mcp_server/server.py`) for a real,
  reproduced ambiguity in how the installed `mcp` SDK (1.9.4) serializes
  list-typed tool results (each item becomes its own content block, so a
  bare 1-item list is indistinguishable from a scalar); it is not how the
  REST API itself shapes those same responses.
- **`mcp_server/` and `legacy/alexa_skill_legacy_rest/` are each pinned to specific SDK
  versions in their own isolated virtual environments**, not integrated
  into `backend/`'s dependency set, because the `mcp` SDK's Starlette
  requirement conflicts with `backend/requirements.txt`'s pinned
  FastAPI/Starlette (documented in `mcp_server/server.py`'s module
  docstring). This is a real, verified conflict, not a hypothetical one.

## 15. Next recommended task

**Gate 5's code-level target is done (§2e.1: multi-watch voice
disambiguation). What's left is exactly what Gate 5 was always going to
need real infrastructure for** — see
[`docs/deployment-runbook.md`](./docs/deployment-runbook.md) for the full
checklist, summarized here: (a) deploy the backend somewhere publicly
reachable (the already-implemented DynamoDB path, §10, plus the new
`backend/Dockerfile`), (b) register `mcp_server/` with a real MCP host
and confirm a real client can drive the full demo through it, (c) create
the Alexa skill for real in the Developer Console from
`legacy/alexa_skill_legacy_rest/models/interaction-model.json`, deploy
`legacy/alexa_skill_legacy_rest/lambda/` to the real AWS Lambda `infrastructure/template.yaml`
provisions, and run the golden demo end-to-end on a real Echo device or
the Console's voice simulator, and (d) run `BedrockLLMClient` against a
real AWS account with a valid, access-granted model for the first time,
comparing its output against `TemplateLLMClient`'s deterministic baseline
under the same grounding guard. None of this requires new application
code — Gates 0 through 5 already implement every piece; what's left is
purely exercising what exists (and the IaC/runbook Gate 5 added to make
that exercising turnkey) against real infrastructure this environment
does not have access to, and fixing whatever that first real contact
surfaces (a category of bug no amount of mocked/local testing can catch).
