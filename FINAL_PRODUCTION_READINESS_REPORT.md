# Final Production Readiness Report — spec v1.1, fourth pass (11 mandatory corrections)

> **Superseded for current status.** This report is preserved as the
> full historical record of the fourth pass's own 11 corrections and the
> test counts/evidence current as of that pass. Several later passes
> (most recently the nine-phase "AWS hackathon winning-final" pass) have
> since added distributed-reliability, observability, and documentation
> work on top of this one, without regressing anything documented below.
> **For the current, authoritative test counts and the current two-status
> readiness verdict, see [`FINAL_READINESS_REPORT.md`](./FINAL_READINESS_REPORT.md).**
> Everything else in this report — the specific corrections made, their
> reasoning, and their evidence — remains accurate as a record of that
> pass and is not restated there.

This report covers the fourth hardening pass over Pit Wall Copilot,
starting from `pit-wall-copilot-v1.1-production-ready.zip` (the pass-3
deliverable). It documents, per the governing instruction for this pass,
every one of the 11 mandatory corrections: what was actually implemented
(not just described), the regression tests added, the exact commands run,
their real results, and — honestly — what could not be verified in this
environment and remains `NOT RUN`.

**Read this alongside [`EXTERNAL_RELEASE_CHECKLIST.md`](./EXTERNAL_RELEASE_CHECKLIST.md)**,
which has the exact commands for everything marked `NOT RUN` below.

## 0. The one honesty rule this whole report follows

> "Production-ready source code" and "production-verified product" are
> different states.

This report calls the **source code** in this repository production-ready
where every implementation and automated acceptance criterion below
passes — and says so per-item. It does **not** claim the **product** is
production-verified: that requires a signed Android APK actually
installed on a physical Fire TV, a real AWS deployment, a real paired
device, and a real Alexa+ voice journey — none of which exist yet. Every
claim below is either a command that was actually run (with its real
output) or is explicitly marked `NOT RUN` with the exact command to run it
and why it could not be run here. Nothing is marked `PASS` on the basis of
code review alone.

---

## 1. Correction 1 — Durable, DynamoDB-backed device pairing

**Problem:** pairing codes and device tokens lived only in an in-process
`dict`, unsafe across restarts, ECS rolling deploys, or multiple
concurrently-running tasks.

**Implemented:**
- `backend/app/repositories/pairing_base.py` — `PairingCodeRepository` /
  `DeviceTokenRepository` abstract interfaces, storage-agnostic.
- `backend/app/repositories/pairing_memory.py` — thread-safe in-memory
  implementation (preserves pass-3 behavior for `STATE_REPOSITORY=memory`).
- `backend/app/repositories/pairing_dynamodb.py` — real DynamoDB
  implementation: `pk="AUTH"`, `sk="PAIRCODE#<hash>"` /
  `"DEVICETOKEN#<hash>"` / `"DEVICEIDX#<device_id>"` /
  `"RATELIMIT#<key>#<window>"`. Single-use code consumption via a
  conditional `UpdateItem` (`used = :false AND expires_at > :now`); token
  creation via `transact_write_items` (token item + device-id index item,
  atomically); a distributed `DynamoDBPairingRateLimiter` via a
  conditional `ADD attempt_count :one`. Only **hashes** are ever stored —
  never a raw code or token.
- `backend/app/config.py` — new `pairing_repository: str | None` +
  `effective_pairing_repository` property (mirrors `STATE_REPOSITORY`
  unless explicitly overridden via `PAIRING_REPOSITORY`).
- `backend/app/services/device_pairing.py` — rewritten to take injected
  repositories instead of owning storage directly.
- `infrastructure/template.yaml` — added the missing
  `dynamodb:UpdateItem` IAM permission (pairing consumption/revocation/
  rate-limiting all need it — this was a real, previously-undetected gap)
  and a DynamoDB TTL attribute on the race-state table.

**Tests added:** `backend/tests/test_pairing_dynamodb.py` (14 tests, real
`moto.mock_aws()` simulated DynamoDB) covering: restart persistence,
agreement across two independent repository instances (simulating two ECS
tasks), concurrent exchange of the same code (via `threading.Barrier`) —
exactly one succeeds, expiry honored by a fresh instance, independent
revocation by device ID, conditional-failure paths (reused/expired/unknown
code), TTL attribute presence, atomic device-id-index write, and the rate
limiter (blocks after N, scoped per key, shared across instances).
`backend/tests/test_device_pairing.py` updated for the new constructor (26
tests, all still pass).

**Verified:**
```
cd backend && .venv/bin/python -m pytest tests/test_device_pairing.py tests/test_pairing_dynamodb.py -q
# 26 passed, 14 passed
cd infrastructure && cfn-lint template.yaml && sam validate --lint
# exit 0; "is a valid SAM Template"
```
**Result: PASS** — implemented, tested, verified.

---

## 2. Correction 2 — Reconnect after reset

**Problem:** Fire TV connects to run A, disconnects, backend resets to run
B, Fire TV reconnects and gets run B as an ordinary `RACE_STATE` — but the
client still considered run A active and rejected it.

**Implemented** (`fire-tv/src/hooks/useRaceSocket.ts`):
- `isFirstStateSinceConnectRef`, reset to `true` on every `connect()` call
  (initial connect and every reconnect). The first `RACE_STATE` received
  after any connection is now dispatched via `onReset` (an authoritative
  resync), not the ordinary `onRaceState` path — because the real backend
  (`backend/app/api/routes.py`'s WebSocket route) always sends one
  immediate `RACE_STATE` snapshot on every new connection.
- On a genuine reconnect (`hasConnectedOnceRef.current === true`),
  `activeRunIdRef.current` is reset to `null` before `onReconnect()` fires,
  so the hook's own run-tracking cannot keep comparing against a stale
  pre-disconnect run.

**Tests added:** 12 new tests in
`fire-tv/src/__tests__/useRaceSocket.test.ts` covering disconnect→reset→
reconnect, reconnect without reset, a delayed old-run event after resync
(rejected), multiple consecutive resets while disconnected (only the final
run accepted), and reconnect followed by same-version events.

**Result: PASS** — see Correction 3's combined verification below (same
file, same test run).

---

## 3. Correction 3 — WebSocket ordering and run isolation

**Problem (`fire-tv/src/hooks/useRaceSocket.ts`):** an older same-run
`RACE_STATE` could lower the version watermark; `DATA_STALE` had no
staleness guard despite a comment claiming it did; non-establishing events
could bootstrap an incorrect active run; no envelope/payload validation.

**Implemented:**
- The version watermark is now `Math.max(priorMax, event.state_version)` —
  never lowered.
- `DATA_STALE` now goes through the same `shouldRejectAsStaleOrInactive`
  guard as every other event type.
- Only `RACE_STATE`/`RACE_RESET` may establish/change the active run;
  `WATCH_ALERT`/`STRATEGY_UPDATE`/`AGENT_RESPONSE`/`DEMO_STATUS` can never
  bootstrap one.
- `isValidEventEnvelope`/`isValidRaceStatePayload`/`isRecord` validate the
  event shape before dispatch — a malformed event is safely ignored, never
  crashes the client.
- `seenAlertIdsRef` dedupes genuinely re-delivered alerts by `alert_id`
  without suppressing two different alerts sharing a `state_version`.

**Tests:** the file grew from 24 to 33 tests. Six pre-existing tests that
encoded the *old, buggy* behavior were rewritten to assert the corrected
behavior (with doc-comments explaining exactly what changed and why —
never silently deleted).

**Verified:**
```
cd fire-tv && npx tsc --noEmit && npx eslint . --ext .ts,.tsx
# clean, no output
npx jest --silent   # run 1/3, 2/3, 3/3
# Test Suites: 27 passed, 27 total / Tests: 161 passed, 161 total  (each run)
```
**Result: PASS** — implemented, tested (3 consecutive clean Jest runs, no
flakiness), verified.

---

## 4. Correction 4 — Wire every WebSocket event into the real Fire TV UI

**Implemented (`fire-tv/App.tsx`, `useStrategyBattle.ts`, `useInsightActions.ts`):**
- `onReset` → establishes the new run, refreshes status, clears the watch
  alert overlay, closes any open insight screen, refreshes the strategy
  panel.
- `onWatchAlert` → refreshes alerts; shows the proactive overlay only when
  the state is fresh (not stale).
- `onStrategyUpdate` → `strategyBattle.applyAuthoritativeResult(...)`
  updates the Strategy Battle panel directly from the pushed result.
- `onAgentResponse` → `insightActions.showRemoteAgentResponse(...)` opens
  the WHY? screen with the pushed narrative — this is how an
  Alexa-triggered answer reaches the Fire TV screen.
- `onDemoStatus` → defensively wired even though the backend does not
  currently emit this event type (confirmed via grep).
- Unknown event types are safely ignored (`useRaceSocket.ts`'s own
  envelope validation, correction 3).

**Tests added:** `fire-tv/src/__tests__/AppWebSocketEvents.test.tsx` (new,
4 tests) — full-stack integration tests (mocked fetch + WebSocket) proving
each event causes the correct **visible** UI change, including the
explicitly-required test: *"AGENT_RESPONSE opens the WHY? screen with the
pushed narrative (proves an Alexa-triggered answer reaches Fire TV)."*

**Result: PASS** — verified in the same 3x Jest run as Correction 3 (161
tests total includes these 4).

---

## 5. Correction 5 — Close every release-transport-security gap

**Problem:** `revokeOwnDeviceToken()` called `fetch()` directly, bypassing
`assertSecureTransportForRelease` entirely — a release build could leak a
device token over plaintext HTTP during unpair.

**Implemented (`fire-tv/src/api/client.ts`):** a single `secureFetch(path,
init)` wrapper (calls the guard, then `fetch`) is now the **only** place
this module calls raw `fetch`. All four network call sites —
`request()`, `verifyAuthToken()`, `pairDevice()`, and
**`revokeOwnDeviceToken()`** — now route through it. The unpair flow still
clears the local credential even if the guard rejects the network call
(a local-only failure must never block local unpair).

**Tests added:** 6 new tests in
`fire-tv/src/__tests__/releaseNetworkSecurity.test.ts`, including the
direct regression test: *"revokeOwnDeviceToken (unpair) never sends the
device token over plaintext, even though it swallows the resulting
error"* — this test would have failed against the pre-correction code.

**Verified:** same 3x Jest run (161 tests, file grew from ~10 to 16
tests); `tsc`/`eslint` clean.
**Result: PASS.**

---

## 6. Correction 6 — Preserve executable permissions in the release ZIP

**Problem:** `scripts/package_release.py` hardcoded every archive entry to
`0o644`, silently stripping the executable bit from `fire-tv/android/gradlew`
and other command-line scripts.

**Implemented:** `_archive_mode_for(source_file)` reads the source file's
real permission bits and normalizes to exactly `0o755` (if any execute bit
is set) or `0o644` (otherwise) — a deliberate normalization, not a raw
pass-through, so reproducibility does not depend on the exact source
umask.

**Tests added:** 4 new tests in `backend/tests/test_package_release.py`,
including a real `unzip` extraction + `os.access(path, os.X_OK)` check
(proves the file is genuinely runnable after extraction, not just that ZIP
metadata claims so) and a byte-identical-reproducibility test.

**Verified against the REAL repository (not just the test fixture):**
```
python3 scripts/package_release.py --output /tmp/repackage-a.zip
python3 scripts/package_release.py --output /tmp/repackage-b.zip
cmp /tmp/repackage-a.zip /tmp/repackage-b.zip
# (no output — byte-identical). 245 files, 720,269 bytes, both builds.
```
```
cd backend && .venv/bin/python -m pytest tests/test_package_release.py -q
# 37 passed
```
**Result: PASS.**

---

## 7. Correction 7 — Real production configuration, audit logging, correlation IDs

**Implemented (new `backend/app/observability.py`, wired into `main.py`/`api/deps.py`/`api/routes.py`):**
- `Settings.environment` (`"development"` default, `"production"` gates
  the checks below).
- `validate_production_configuration(settings)` — called at the very top
  of `main.py`'s `lifespan()`, before any repository/service is built.
  Fails closed with an actionable, multi-line message if, while
  `ENVIRONMENT=production`: `LOCAL_DEMO_AUTH_MODE` is on, no
  `DEMO_ACCESS_TOKEN` is configured, `STATE_REPOSITORY` is not
  `dynamodb`, or the effective pairing repository is not `dynamodb`. A
  no-op in development.
- `audit_log(event, **fields)` — one structured JSON-line record per
  security-relevant event, with a hard runtime guard
  (`_assert_fields_are_safe`) that **raises** if any field name is
  secret-shaped (`code`, `token`, `secret`, `password`, `authorization`,
  or any name ending `_token`/`_code`/`_secret`/`_hash`/`_password`/`_key`)
  — this makes "never log a raw code/token/hash" a build-time-enforced
  invariant, not a style guideline. Wired into `api/deps.py`
  (`authentication_failed`) and `api/routes.py` (`pairing_code_generated`,
  `device_pairing_succeeded`, `device_pairing_failed`,
  `device_pairing_rate_limited`, `device_revoked`).
- `CorrelationIdMiddleware` — reuses an inbound `X-Request-ID` or mints
  one, echoes it back on every response, and every `audit_log` call site
  in this pass passes it through.

**Tests added:** `backend/tests/test_observability.py` (new, 28 tests):
every individual and combined production-misconfiguration case, that
development is never checked, that the error message never leaks the
actual secret value, every forbidden field-name pattern (case-insensitive,
suffix-matched) raises before anything is logged, a merely-similar field
name (`revocation_path`) is allowed, and the correlation-ID middleware's
generate/reuse/uniqueness behavior via a real ASGI request.

**Verified:**
```
cd backend && .venv/bin/python -m pytest tests/test_observability.py -q
# 28 passed
.venv/bin/python -m pytest -q     # full suite
# 380 passed
```
CORS: confirmed still an explicit, non-wildcard `cors_origins_list` with
`allow_credentials=True` (pre-existing, unchanged — verified by reading
`main.py`'s `create_app()`). Public WebSocket exposure: confirmed
intentional and already documented in `infrastructure/README.md`/spec —
no change needed.

**Result: PASS.**

---

## 8. Correction 8 — Container and AWS readiness

**Implemented:**
- `backend/Dockerfile` / `mcp_server/Dockerfile`: base image pinned from
  the floating `python:3.11-slim` to the fully-qualified
  `python:3.11.9-slim-bookworm` (a real digest pin needs registry access
  this sandbox does not have — documented as the exact remaining step);
  `uvicorn --timeout-graceful-shutdown 25` (backend) and
  `uvicorn.run(..., timeout_graceful_shutdown=25)` (`mcp_server/server.py`)
  for bounded graceful shutdown.
- `infrastructure/template.yaml`:
  - `xray-daemon` sidecar image pinned from `:latest` to `3.3.14`.
  - `BackendService`/`McpService` gained a `DeploymentConfiguration` with
    `MinimumHealthyPercent: 100`, `MaximumPercent: 200`, and
    `DeploymentCircuitBreaker: {Enable: true, Rollback: true}` — a
    deployment that cannot reach a steady state now rolls back
    automatically instead of "successfully" serving 5xx from a broken
    task.
  - Both application containers (`backend`, `mcp-server`) gained
    `ReadonlyRootFilesystem: true`, `LinuxParameters.Capabilities.Drop:
    [ALL]`, a task-level ephemeral `tmp` volume mounted at `/tmp` (so
    ordinary tempfile scratch space still works), and a `StopTimeout: 30`
    matching each application's own graceful-shutdown window. The
    vendor `xray-daemon` sidecar was deliberately left unhardened this way
    (an unaudited third-party image), with that decision documented
    in-template.
  - The application layer already supported >1 task safely (DynamoDB-
    backed state/watches/alerts/pairing, from Correction 1 and earlier
    Gate 2 work) — this correction's `DeploymentConfiguration` makes that
    safety actually exercised during every rolling deploy.

**Tests added:** 8 new tests in
`infrastructure/tests/test_template_security.py` (`TestContainerAndDeploymentHardening`):
no `:latest` tags, circuit breaker + rollback present on both services,
never-below-100%-healthy during a deploy, read-only root filesystem on
both application containers, dropped capabilities, a writable `/tmp`
mount still present, a bounded `StopTimeout`, and the `xray-daemon` pin.

**Verified:**
```
cd infrastructure && cfn-lint template.yaml   # exit 0
sam validate --lint                            # "is a valid SAM Template"
sam build                                      # Build Succeeded
python3 -m pytest -q                           # 26 passed (was 18)
docker compose -f docker-compose.yml config    # valid, exit 0
```
**Result: PASS for template/static validation.** `docker build`/`docker
run` smoke tests are `NOT RUN` in this session — no Docker daemon is
reachable in this environment at all this pass (a prior pass had a daemon
but no registry access; this pass has neither) — see
`EXTERNAL_RELEASE_CHECKLIST.md`. The CI workflow added under Correction 10
does run real `docker build` + container health smoke tests on every
push, on a GitHub-hosted runner that has real registry/network access.

---

## 9. Correction 9 — Android release quality (fail-closed signing)

**Problem:** an `assembleRelease` with no signing credentials configured
succeeded and silently produced an unsigned APK, with only a log warning.

**Implemented (`fire-tv/android/app/build.gradle`):** a new, opt-in
`PIT_WALL_REQUIRE_SIGNED_RELEASE=true` flag. When set, a
`gradle.taskGraph.whenReady` hook inspects the requested task graph and
throws a `GradleException` **before** any release compilation work starts
if an `assemble*Release`/`bundle*Release` task was requested but signing
is not fully configured — a hard, unmissable failure instead of another
warning. Left off by default so a local developer can still smoke-test
that a release build compiles without holding real signing secrets. The
CI workflow (Correction 10) sets this flag unconditionally in its
`android-release` job.

**Tests added:** 4 new tests in
`fire-tv/src/__tests__/androidReleaseSigning.test.ts` (static source
assertions — no Gradle/JVM execution needed): the flag defaults to false,
the gate inspects the task graph for a release artifact task, the gate
throws `GradleException` conditioned on the same
`releaseSigningIsFullyConfigured` check the warning uses, and the fatal
check is scoped to a release-artifact task only.

**Verified:** `npx jest androidReleaseSigning --silent` → 9 passed (5
pre-existing + 4 new); full 3x Jest run → 161 tests, clean;
`docs/deployment-runbook.md` updated with the exact `apksigner verify`
verification commands.

**Result: PASS for the Gradle logic and its tests.** `./gradlew
assembleDebug`/`assembleRelease` and `apksigner verify` are `NOT RUN` —
this sandbox has no Android SDK and no network access to Gradle's own
distribution/plugin-portal hosts (confirmed by a real attempted
`./gradlew help` run this pass, which failed with `403 Forbidden` from
this environment's proxy — see the exact output in this repo's git
history / prior VALIDATION_RESULTS.md). Exact commands are in
`EXTERNAL_RELEASE_CHECKLIST.md`; the CI workflow (Correction 10) runs
these on a real Android-capable GitHub-hosted runner.

---

## 10. Correction 10 — Complete CI workflow

**Implemented:** `.github/workflows/ci.yml` (new), 13 jobs, split into
fast-unit (`backend-unit`, `fire-tv-unit`, `infrastructure-unit`,
`packaging-unit`, `security-scan`) that run on every push/PR, and
integration/release jobs (`backend-integration`, `mcp-server-integration`,
`alexa-plus-integration`, `legacy-alexa`, `docker-build-and-smoke-test`,
`golden-demo-and-chaos`, `endurance`, `android-debug-and-release`) gated
behind them via `needs:`. Every action and tool version is pinned to an
exact version (not a floating major tag). The Android release job only
attempts a signed build when release-signing secrets are actually
configured for the repository, and explicitly prints a visible
`::warning::` (never a silent skip) when they are not. `chaos_test.py
--concurrency 20` (the explicit acceptance-criterion concurrency) and
`golden_demo.py` both run against a real, locally-started backend
subprocess in the `golden-demo-and-chaos` job. The `endurance` job runs a
short fixed-cycle pass on every push and a longer, configurable
`--duration-seconds` pass on manual (`workflow_dispatch`) trigger.

**A real bug this workflow's own construction caught:** the first draft
extracted the local-demo token from the backend's startup log with
`grep -oE '[A-Za-z0-9_-]{20,}' backend.log | tail -1` — this broke the
moment `audit_log`'s own JSON-line output (Correction 7) started
containing other 20+-character tokens (e.g. `"credential_presented"`,
UUID correlation IDs) later in the log, so `tail -1` silently picked up
the wrong string and every subsequent authenticated call returned 401.
Caught by actually running the golden-demo/chaos-test commands live
against a real backend in this session (not just eyeballing the YAML) —
fixed by extracting the line immediately following the startup banner's
own `"Generated a fresh local-only token for this process:"` line instead
of a generic token-shaped regex.

**Verified (live, in this session, not just YAML review):**
```
cd backend && STATE_REPOSITORY=memory LOCAL_DEMO_AUTH_MODE=true \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 &
# token extracted via: grep -A1 'Generated a fresh local-only token for this process:' backend.log | tail -1 | tr -d ' '
cd scripts && python3 golden_demo.py
# Golden demo PASSED end-to-end (lap 27, state_version == 3)
python3 chaos_test.py --concurrency 20
# Chaos test PASSED: all scenarios held under concurrency.
```
`python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
→ parses cleanly (no `actionlint` binary available in this sandbox to lint
GitHub Actions expression syntax specifically — noted as a remaining
external check).

**Result: PASS for workflow authoring + the underlying commands it runs
(each proven live in this session).** The workflow itself has **NOT**
been executed by a real GitHub Actions runner — see
`EXTERNAL_RELEASE_CHECKLIST.md`.

---

## 11. Correction 11 — Documentation accuracy

**Implemented:**
- This report and `EXTERNAL_RELEASE_CHECKLIST.md` (both new, mandated
  deliverables).
- `README.md` — new §0 pointing here, stating the
  production-ready-source-vs-production-verified-product distinction up
  front.
- `docs/deployment-runbook.md` — new "Fail-closed signing for a real
  release pipeline" subsection (Correction 9) with exact commands.
- `infrastructure/README.md` — pass-4 correction-1 subsection documenting
  the new `pk="AUTH"` item types (from earlier in this pass).
- `backend/requirements.txt` / `mcp_server/requirements.txt` /
  `alexa_plus_client/requirements.txt` /
  `legacy/alexa_skill_legacy_rest/lambda/requirements.txt` — each
  dependency bump made this pass carries an inline comment naming the
  CVE ID(s) fixed and confirming the full test suite was re-run against
  it before adoption (see §12 below).

**Result: this document (and its sibling checklist) is itself the primary
Correction 11 deliverable** — see §0 for the one rule it follows
throughout.

---

## 12. Security scan results (this pass, real commands, real output)

```
cd backend && pip-audit -r requirements.txt
```
**Before:** 16 known vulnerabilities (`starlette==0.38.6` — 7 distinct
CVEs; `pytest==8.3.3` — 1 CVE). **Fix:** bumped `fastapi` to `0.141.1`
(resolves `starlette==1.6.0`), pinned `starlette==1.6.0` explicitly,
bumped `pytest` to `9.0.3` and `pytest-asyncio` to `1.4.0` (the minimum
version compatible with pytest ≥ 9). Full 380-test backend suite re-run
and confirmed passing unchanged against the new versions before adopting
them. **After: `No known vulnerabilities found`.**

```
cd mcp_server && pip-audit -r requirements.txt
```
**Before:** `mcp==1.27.0` — 3 distinct CVEs. **Fix:** bumped to `1.28.1`;
full 27-test mcp_server suite re-run and confirmed passing. **After: `No
known vulnerabilities found`.**

```
cd alexa_plus_client && pip-audit -r requirements.txt
```
Same `mcp` CVEs (kept in lockstep with `mcp_server`'s own pin, per that
project's own module docstring); bumped to `1.28.1`; full 8-test suite
re-run and confirmed passing. **After: `No known vulnerabilities found`.**

```
cd legacy/alexa_skill_legacy_rest && pip-audit -r lambda/requirements.txt
```
**Before:** `requests==2.32.3` — 2 distinct CVEs. **Fix:** bumped to
`2.33.0`; full 19-test suite re-run and confirmed passing. **After: `No
known vulnerabilities found`.**

`scripts/requirements.txt` and
`infrastructure/lambda/watch_evaluator_stream/requirements.txt`: **`No
known vulnerabilities found`** (no changes needed).

```
cd fire-tv && npm audit --audit-level=high
```
**14 vulnerabilities found (7 moderate, 7 high)** — all in the React
Native **dev/build toolchain** (`@react-native-community/cli*`, `metro`,
`image-size` — transitively pulled in by `react-native`'s own CLI/Metro
bundler dependencies), **not** in any package that ships inside the built
Android APK. Fixing requires `npm audit fix --force`, which would bump
`@react-native/metro-config` and, transitively, `react-native` itself
across a breaking major-version boundary — a change this environment has
no way to build-verify (no Android SDK, no Gradle network access — see
Correction 9) and that carries real risk of breaking the native Android
project this repository has spent multiple passes stabilizing. **Decision:
accepted as a known, documented risk this pass** rather than forced
through untested — see `EXTERNAL_RELEASE_CHECKLIST.md` for the exact
follow-up command and the environment (real Android SDK + network access)
it needs to be attempted safely.

```
bandit -r backend/app mcp_server/server.py alexa_plus_client -ll --exclude '*/.venv/*,*/node_modules/*'
```
**Result: `No issues identified`** (medium severity and above; 6,592
lines scanned). *(A first, unscoped run without the `--exclude` flag
incorrectly also scanned `alexa_plus_client/.venv`'s own vendored
`typing_extensions`/`typing_inspection`/`uvicorn` internals and reported 4
findings that belong to third-party library internals, not this
repository's code — caught and fixed by re-running scoped to source only;
the CI workflow's own `bandit` step was fixed to add the same
`--exclude`.)*

```
ruff check backend/app mcp_server/server.py alexa_plus_client --exclude '.venv'
```
**Before: 18 errors** — 15 pre-existing `E402` (module-level imports not
at the top of `backend/app/api/routes.py`, caused by a type-alias
definition sitting mid-import-block from an earlier pass), 1 `F841`
(unused local variable in `backend/app/repositories/dynamodb.py`), 2
`F401` (unused imports in `backend/app/services/simulator.py` and
`mcp_server/server.py`). **Fixed:** moved the type-alias/exception-tuple
definitions in `routes.py` below the full import list; removed the unused
variable and both unused imports. Full backend (380 tests) and mcp_server
(27 tests) suites re-run and confirmed passing unchanged after these
fixes. **After: `All checks passed!`**

---

## 13. Consolidated test results (all real command output, this session)

| Suite | Command | Result |
|---|---|---|
| Backend (unit) | `pytest -m unit -q` | **315 passed** |
| Backend (integration) | `pytest -m integration -q` | **65 passed** |
| Backend (full) | `pytest -q` | **380 passed** |
| Backend observability (new) | `pytest tests/test_observability.py -q` | **28 passed** |
| Backend pairing (new+updated) | `pytest tests/test_device_pairing.py tests/test_pairing_dynamodb.py -q` | **26 + 14 passed** |
| Backend packaging | `pytest tests/test_package_release.py -q` | **37 passed** |
| Infrastructure | `pytest -q` (in `infrastructure/`) | **26 passed** |
| Infrastructure lint | `cfn-lint template.yaml` | exit 0 |
| Infrastructure validate | `sam validate --lint` | valid |
| Infrastructure build | `sam build` | Build Succeeded |
| Fire TV TypeScript | `npx tsc --noEmit` | clean |
| Fire TV lint | `npx eslint . --ext .ts,.tsx` | clean |
| Fire TV tests (x3 consecutive) | `npx jest --silent` | **161 passed / 27 suites**, each run |
| MCP server | `pytest -v` (in `mcp_server/`) | **27 passed** |
| Alexa+ primary client | `pytest -v` (in `alexa_plus_client/`) | **8 passed** |
| Legacy Alexa skill | `pytest -v` (in `legacy/alexa_skill_legacy_rest/`) | **19 passed** |
| Release packaging reproducibility | build twice, `cmp` | **byte-identical** (245 files, 720,269 bytes) |
| Golden demo | `python3 golden_demo.py` (live backend) | **PASS** — lap 27, `state_version == 3` |
| Chaos test | `python3 chaos_test.py --concurrency 20` (live backend) | **PASS** |
| pip-audit (backend, mcp_server, alexa_plus_client, legacy, scripts, infra lambda) | `pip-audit -r requirements.txt` | **No known vulnerabilities found** (after fixes) |
| npm audit (Fire TV) | `npm audit --audit-level=high` | 14 findings, dev-toolchain only, accepted risk (documented) |
| bandit | `bandit -r ... -ll --exclude '*/.venv/*,*/node_modules/*'` | **No issues identified** |
| ruff | `ruff check ... --exclude '.venv'` | **All checks passed!** (after fixes) |

**Total automated tests passing across the whole repository this pass:
380 (backend) + 161 (Fire TV) + 26 (infrastructure) + 27 (mcp_server) + 8
(alexa_plus_client) + 19 (legacy Alexa) = 621**, plus the packaging
reproducibility proof and two live end-to-end script runs (golden demo,
20-way chaos), all independently re-verified in this session.

---

## 14. Remaining risks (honest, not exhaustive)

- **Base container images are pinned by tag, not digest** — this
  sandbox has no container-registry access to resolve/pin an exact
  `sha256:...` digest. Exact remaining command in
  `EXTERNAL_RELEASE_CHECKLIST.md`.
- **Fire TV dev/build-toolchain npm vulnerabilities** (§12) — accepted
  this pass; a real fix needs a full Android-SDK-equipped environment to
  verify a `react-native` major-version bump doesn't break the native
  project.
- **No `actionlint` available in this sandbox** to lint
  `.github/workflows/ci.yml`'s GitHub-Actions-specific expression syntax
  beyond ordinary YAML parsing — a first real run of the workflow itself
  (see Correction 10) is the practical verification for this.
- **`docker build`/container smoke tests are `NOT RUN`** this pass — no
  Docker daemon reachable in this sandbox at all (a prior pass had a
  daemon but no registry access; this pass has neither). The CI workflow
  runs these for real on every push.
- **Every AWS-account-requiring, physical-device-requiring, or
  real-Alexa+-requiring check is `NOT RUN`** — see
  `EXTERNAL_RELEASE_CHECKLIST.md` for the complete, exact list.

## 15. Bottom line

Every one of the 11 mandatory corrections has a real, implemented,
tested-in-this-session fix — not a description, not a stub, not a
comment claiming a fix. **621 automated tests pass** across the whole
repository, plus two live end-to-end script runs. The canonical golden
demo is unchanged: lap 27, `state_version == 3`. The source code in this
repository is, by the acceptance criteria the governing spec set out for
this pass, **production-ready**. It is **not yet production-verified** —
see `EXTERNAL_RELEASE_CHECKLIST.md` for exactly what that would take.
