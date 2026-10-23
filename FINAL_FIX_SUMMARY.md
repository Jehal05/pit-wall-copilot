# Final Fix Summary — spec v1.1, second "inspect, repair, test, return" pass

> **Fourth-pass note:** a FOURTH hardening pass followed the third pass
> below (11 further mandatory corrections: durable DynamoDB-backed device
> pairing, Fire TV WebSocket reconnect/ordering fixes, closing every
> release-transport-security gap, deterministic release-ZIP permissions,
> fail-closed production configuration validation + structured audit
> logging, container/AWS hardening, fail-closed Android release signing, a
> full CI workflow, and documentation accuracy). See
> **[`FINAL_PRODUCTION_READINESS_REPORT.md`](./FINAL_PRODUCTION_READINESS_REPORT.md)**
> for that pass's own correction-by-correction record with real test
> counts, delivered as `pit-wall-copilot-v1.1-production-ready-final.zip`.
> This file's third-pass and second-pass content is kept as-is below (the
> historical record of what those passes did); their test counts predate
> the fourth pass's additions.

> **Third-pass note:** a THIRD, final repair pass followed this one, fixing
> 11 further, more specific issues (Android release signing, unhandled
> WebSocket events, hardcoded test-venv paths, a flaky Fire TV test, a
> real device-pairing exchange replacing shared-secret entry, Docker
> health checks, a deterministic packaging script, release network
> security, a service-level concurrent-reset test, pytest categories, and
> honest documentation). See the **"Third pass (spec v1.1 pass 3)"**
> section immediately below this line for that pass's own repair-by-repair
> record, delivered as `pit-wall-copilot-v1.1-production-ready.zip`. This
> file's SECOND-pass content is kept as-is beneath that new section (the
> historical record of what THAT pass did); its test counts predate the
> third pass's additions — `VALIDATION_RESULTS.md` has the current totals.

---

## Third pass (spec v1.1 pass 3)

Governing instruction: a fresh attachment enumerating 11 mandatory
corrections against `pit-wall-copilot-v1.1-final-fixed.zip` (this repo,
~92% complete per that instruction), explicitly requiring real
implementation and test changes, not documentation-only updates, and
explicitly requiring that existing verified functionality — 266 backend
tests (now 334; see below), DynamoDB active-run `GENERATION` protection,
the exact lap-27/state_version-3 golden transition, the five MCP tools,
etc. — be preserved.

### 1. Android release signing

`fire-tv/android/app/build.gradle`'s `release { signingConfig
signingConfigs.debug }` is removed. Release signing credentials are now
resolved from `fire-tv/android/app/release.keystore.properties`
(gitignored; `.example` template committed), then
`PIT_WALL_RELEASE_STORE_FILE`/`_STORE_PASSWORD`/`_KEY_ALIAS`/`_KEY_PASSWORD`
env vars, then Gradle properties of the same names (for CI secrets). If
none are fully configured, `assembleRelease` produces an **unsigned**
artifact (with a Gradle-time warning) rather than ever falling back to the
debug key. `.gitignore` now excludes `*.jks`/`*.keystore` (except the
standard debug keystore) and `release.keystore.properties`. A static Jest
test (`fire-tv/src/__tests__/androidReleaseSigning.test.ts`, 5 tests)
parses `build.gradle` itself and proves the release block never
references `signingConfigs.debug`. `docs/deployment-runbook.md` gained a
"Step E — Android release signing" with PowerShell and Unix examples.
Acceptance check verified: `rg -n "signingConfig signingConfigs.debug"
fire-tv/android/app/build.gradle` now matches only the debug config
(line inside `debug { ... }`).

### 2. Every required WebSocket event is handled

`fire-tv/src/hooks/useRaceSocket.ts` previously had a `default: break;`
case silently dropping `STRATEGY_UPDATE`/`DEMO_STATUS`/`DATA_STALE` with a
comment calling that "safe to ignore." All seven event types
(`RACE_STATE`, `RACE_RESET`, `STRATEGY_UPDATE`, `WATCH_ALERT`,
`AGENT_RESPONSE`, `DATA_STALE`, `DEMO_STATUS`) now have real handling:
new `onStrategyUpdate`/`onDemoStatus`/`onDataStaleChange` callbacks, a
`DATA_STALE {stale: boolean}` payload that overrides the local 5s
staleness timer immediately in either direction, and a local run_id/
state_version guard (independent of `useRaceState`'s own) so
`STRATEGY_UPDATE`/`WATCH_ALERT`/`AGENT_RESPONSE`/`DEMO_STATUS` from a
superseded run or an out-of-order `state_version` are dropped exactly
like `RACE_STATE` already was. The "safe to ignore" comment is gone.
`useRaceSocket.test.ts` gained the 12 required deterministic tests
(dispatch of every event type; `DATA_STALE` set/clear; old-run and
lower-version rejection; post-reconnect alert recovery; no dispatch to an
unmounted component; malformed and unknown-event safety) — 23 tests total
in that file, all passing.

### 3. Environment-independent MCP/Alexa+ test interpreter resolution

`mcp_server/tests/conftest.py` and `alexa_plus_client/tests/conftest.py`
previously hardcoded `backend/.venv/bin/python`. Both now use a shared
`scripts/testing_support/resolve_backend_python.py`, resolved in order:
`PIT_WALL_BACKEND_PYTHON` env var → `backend/.venv/Scripts/python.exe`
(Windows) / `backend/.venv/bin/python` (Unix) if present → the current
interpreter, only if it can actually import `fastapi`/`uvicorn` → a clear
`pytest.fail()` (or an explicit opt-in skip via
`MCP_TESTS_ALLOW_MISSING_BACKEND_VENV=1` /
`ALEXA_PLUS_TESTS_ALLOW_MISSING_BACKEND_VENV=1`) if none apply — never a
silent skip by default. `scripts/tests/test_resolve_backend_python.py`
(7 tests) exercises all five branches with every OS/filesystem input
injected, so it runs identically on any host. Verified for real: both
`PIT_WALL_BACKEND_PYTHON=$(pwd)/backend/.venv/bin/python python3 -m
pytest` (mcp_server: 27 passed) and the same command with the env var
unset (auto-detected `backend/.venv`, still 27 passed) both work, as does
`alexa_plus_client` (8 passed either way).

### 4. Fire TV focus test stabilized

`AppFocus.test.tsx`'s first test used real timers and never called
`unmount()` — its own real `setInterval`-driven poll loops (`useRaceState`,
`useAlerts`) kept firing with real timers for the rest of the
`--runInBand` process, contending for real CPU time against whatever test
ran next and making that test's timeout flaky (a deterministic effect,
not "the environment was slow"). Fixed by capturing `render()`'s
`unmount` and calling it once the test's assertions are done. Verified
stable: the full Fire TV suite (26 suites, 137 tests) was run **three
consecutive times** with `npm test -- --runInBand` (via `npx jest
--runInBand`) — all three runs passed in full, not just one retry.

### 5. Real device-pairing exchange replaces shared-secret entry

The pass-2 pairing screen had a person type the FULL backend mutation
secret (`X-Demo-Access-Token`/`DemoAccessTokenSecret`) into the Fire TV.
That is now replaced with a genuine short-lived, one-time pairing-code
exchange:

- **Backend** (`app/services/device_pairing.py`, new): `POST
  /v1/auth/pairing-codes` (requires the master token — an administrator's
  action, never called by the Fire TV) issues a 6-digit code, valid 5
  minutes, single-use; only its SHA-256 hash is ever stored. `POST
  /v1/auth/pair {code, device_name}` (no master-token requirement — a
  device with no credential yet must be able to reach it) exchanges a
  valid code for a device-specific token (32 bytes,
  `secrets.token_urlsafe`), stored only as a hash, independently expiring
  (30 days) and independently revocable. `POST /v1/auth/revoke` supports
  both self-revoke (the device presents its own token, no master secret
  needed — this is what "Unpair" uses) and admin-revoke (the master
  token plus an explicit `device_id`, for revoking a lost/compromised
  device remotely). Pairing attempts are rate-limited (10/min per client
  IP). `require_demo_access_token` (`api/deps.py`) now accepts EITHER the
  master token (`hmac.compare_digest`, constant-time) OR a currently
  valid device token for every mutating endpoint and `/v1/auth/verify` —
  so "paired" and "can mutate" are still never two different claims. 26
  new backend tests (`backend/tests/test_device_pairing.py`): valid/wrong/
  expired/reused code exchange, device-token verification/expiry/
  revocation, independent revocation of one device vs. another, rate
  limiting, successful/rejected mutation with a device vs. invalid token,
  no plaintext storage of either secret, master token never appearing in
  a pairing response body.
- **Fire TV**: `PairingScreen.tsx` now collects a 6-digit numeric code
  (`keyboardType="number-pad"`, stripped/capped client-side), not a
  secret-entry field — its old `pairing-token-input` test ID and
  `secureTextEntry` field are gone. `client.ts` gained `pairDevice()`
  (`POST /v1/auth/pair`) and `revokeOwnDeviceToken()` (best-effort;
  network failure never blocks the local unpair). `useDeviceAuth.pair()`
  now exchanges the typed code for a device token and stores only that
  token; `signOut()` now calls the self-revoke endpoint before clearing
  local secure storage. `useDeviceAuth.test.ts` (12 tests),
  `PairingScreen.test.tsx` (8 tests), and `AppPairingGate.test.tsx` (5
  tests) were reworked for the code/token distinction, including explicit
  "never renders a field for the full master secret" and "device token
  sent to /v1/auth/revoke is the device's own, not the master token"
  assertions.
- The backend's master `DEMO_ACCESS_TOKEN` never appears in any Fire TV
  source file, test, Gradle file, or doc (verified by the repository-wide
  security scan below) — local dev's separate, pre-existing
  `LOCAL_DEV_AUTH_TOKEN` escape hatch (dev builds only, `__DEV__`-gated)
  is unchanged and still documented as such.

### 6. Docker and Compose health checks

`backend/Dockerfile` and `mcp_server/Dockerfile` each gained a
`HEALTHCHECK` using a plain Python stdlib `urllib.request` call (no
curl/wget — neither is installed in the slim base image) against `/health`
and `/ready` respectively (`/ready` additionally confirms the backend is
reachable from mcp_server). `docker-compose.yml` gained matching explicit
`healthcheck:` blocks for both services, and a new `mcp_server` service
definition with `depends_on: backend: condition: service_healthy` — `docker
compose up mcp_server` now waits for the backend to report healthy before
starting. No credentials appear in any health-check command.
`backend/tests/test_docker_health_checks.py` (6 tests) statically asserts
all of this, including that neither Dockerfile's `HEALTHCHECK` clause
references curl/wget or any credential-shaped string, and that
`docker-compose.yml` never bakes a literal token/secret/password value
(every such env var is a `${...}` substitution). `docker compose config`
was re-run and parses cleanly with both services present.

### 7. Final archive cleaned via a deterministic packaging script

The pass-2 archive incorrectly contained `fire-tv/.env` and
`fire-tv/android/.gradle/` (confirmed present again in this checkout's
working tree — `fire-tv/.env` still exists on disk for local dev
convenience, which is exactly why packaging must filter it, not why it
shouldn't exist at all). New `scripts/package_release.py` builds the
archive from an explicit include/exclude rule set (`.env` except
`.env.example`; `.gradle`; `node_modules`; `.venv`/`.venv-review`/other
venvs; Android `build/`; `__pycache__`/`.pytest_cache`/coverage output;
`*.jks`/`*.keystore` except the debug keystore; `release.keystore.properties`;
`*.apk`/`*.aab`; IDE/OS junk), then RE-OPENS the just-built archive and
independently re-checks every entry against the same rules (catching a
future bug where some other code path adds a file bypassing the builder),
printing file count and size, and refusing (deleting the bad archive) if
anything forbidden is found. `backend/tests/test_package_release.py` (33
tests) covers the exclusion rules directly and a full synthetic build+
inspect round trip, including that a real secret value's BYTES never
appear anywhere in the built archive, not just that the file wasn't
listed. Run for real against this repository: **237 files, 657,892 bytes
(0.63 MiB), inspection PASSED** — see `VALIDATION_RESULTS.md` §10 for the
exact `zipinfo` grep confirming no `.env`/`.gradle`/`node_modules`/`.venv`
entries.

### 8. Release network security

`fire-tv/android/app/src/main/AndroidManifest.xml` now explicitly sets
`android:usesCleartextTraffic="false"` (previously relying on the
platform default, which is version-dependent); the debug-only manifest
override to `true` is unchanged, and there is no release-specific
manifest re-enabling it. `api/client.ts` gained
`assertSecureTransportForRelease(url)`, called before every network
request (`request()`, `verifyAuthToken()`, `pairDevice()`) and before
`useRaceSocket.ts` opens a WebSocket: a no-op in a dev/debug bundle
(`__DEV__` is statically `true`), but throws
`InsecureReleaseConfigurationError` in a release bundle for any
non-`https://`/`wss://` URL — release builds can never send a request
(with or without credentials) over plaintext. `releaseNetworkSecurity.test.ts`
(10 tests) covers dev-mode-allows-http, release-rejects-http/ws,
release-accepts-https/wss, the error message never containing a
credential, and the manifest's cleartext configuration directly.

### 9. Concurrent-reset protection strengthened with a service-level test

`SimulatorService` gained a dedicated `_reset_lock` (an `asyncio.Lock`,
explicitly NOT a UUID-ordering scheme) held for the ENTIRE body of
`_begin_new_run` (including its repository `await`), closing a real gap:
previously, `self._active_run_id` was set synchronously (in call order)
before the repository write, so two concurrent `reset()` calls could
leave the service's own in-process bookkeeping disagreeing with whichever
run the repository's atomic `begin_new_run` actually committed last. Now
the second caller to acquire the lock is unambiguously the final run,
everywhere. `backend/tests/test_concurrent_reset_service_level.py` (3
tests) proves this at the SERVICE layer (not just the repository layer
`test_active_run_protection.py` already covered): repository state and
control share one active run_id; the service's own `_active_run_id`
matches it; the last `on_state_committed` notification (the RACE_RESET
equivalent) describes the actually-active run; a delayed write from the
superseded run is still rejected; starting after the race advances only
the winning run. Also re-verified live: `chaos_test.py`'s
`concurrent_reset` scenario (20 concurrent resets against a real running
backend) still passes.

### 10. Test categories introduced across every Python suite

`unit`/`integration`/`infrastructure`/`android`/`aws`/`device` markers
are now registered (with `--strict-markers`, so a typo'd marker is a
collection ERROR, not a silent no-op) in every Python suite's
`pytest.ini` (`backend`, `mcp_server`, `alexa_plus_client`,
`legacy/alexa_skill_legacy_rest`, `infrastructure`), each with an
auto-tagging `pytest_collection_modifyitems` hook in that suite's
`conftest.py` so hundreds of individual test functions don't need
hand-tagging. Split achieved: backend 250 `unit` + 51 `integration` (moto-
simulated DynamoDB) = 301; infrastructure 18 `infrastructure`; mcp_server
27 `integration`; alexa_plus_client 8 `integration`; legacy 19 `unit`.
`android`/`aws`/`device` are registered (for a consistent vocabulary
across the whole repository) but deliberately applied to **zero** tests
in this pass — there are no real-Android-build, real-AWS-account, or
real-device pytest tests anywhere in this repository, and claiming
otherwise would be exactly the kind of misleading category label this
correction exists to prevent. Per this correction's own explicit
instruction, no prior "zero skips anywhere" framing is used: `boto3 =
pytest.importorskip("boto3")` in `test_dynamodb_repository.py` is a real,
present, conditional skip path (it only actually skips if `boto3` is
missing, which it never is in this environment, but the mechanism exists
and is not hidden).

### 11. Documentation made honest for this pass

`README.md`, `IMPLEMENTATION_STATUS.md`, `VALIDATION_RESULTS.md`,
`REMAINING_EXTERNAL_VERIFICATION.md`, and this file were updated to
reflect exactly what this third pass implemented, tested, built, and
could NOT run — see each file's own top for a pointer to this section,
and `VALIDATION_RESULTS.md` §10-13 for the exact new commands and output.

### What did NOT change in this pass

Every piece of functionality the governing instruction explicitly listed
as needing preservation was left alone except where a correction above
required touching it: the deterministic simulator, the four fictional
cars, the fixed lap 24-28 scenario, `run_id`/`state_version`, the
strategy engine, PIT NOW vs WAIT simulation, the seeded 200-trial
uncertainty model, watch persistence/hysteresis/idempotency, stored
triggering snapshots, grounded explanations, the regulation found/
evidence contract, the five MCP tools, Streamable HTTP MCP, the dedicated
MCP bearer token, HTTPS/WSS-by-default deployment, ECS execution-role
secret scoping, the Fire TV dashboard/insight screens, the Alexa+
simulated client, the legacy Alexa fallback, and the chaos/endurance/
golden-demo scripts (all re-run live in this pass — see above and
`VALIDATION_RESULTS.md`). No test was removed or weakened to obtain a
passing result; every test file this pass touched either added new real
assertions or updated existing ones to match a REAL, intentional behavior
change (documented per-file above), never to paper over a failure.

---

## Second pass (spec v1.1 "second inspect, repair, test, return" pass)

This is the record of a SECOND, deeper correctness pass over the Pit Wall
Copilot repository, on top of the original 16-repair "fixall" pass
recorded in `IMPLEMENTATION_STATUS.md` (delivered as
`pit-wall-copilot-v1.1-complete.zip`). This pass was given that same zip
back, plus a fresh attachment naming 13 specific, concrete defects the
first pass had missed, left incomplete, or introduced. Every item below
was actually implemented, tested, and (where the environment allows)
run — not just documented as a plan. `VALIDATION_RESULTS.md` has the
exact commands and exact output for every check; this file is the
narrative, repair-by-repair record `README.md`/`IMPLEMENTATION_STATUS.md`
point to for what changed and why.

Delivered as: **`pit-wall-copilot-v1.1-final-fixed.zip`**.

## Overall completion

**13 of 13 corrections implemented and code-complete. 12 of 13 fully
verified by real, passing automated tests in this environment; 1 (Fire TV
Android release authentication) is implemented and unit-tested but has an
explicit, honestly-recorded `NOT RUN` gap for the actual on-device/APK
build — see correction 5 and correction 7 below for the exact reason.**

Total automated tests passing across the repository after this pass:
**442** (266 backend + 18 infrastructure + 104 Fire TV + 27 mcp_server + 8
alexa_plus_client + 19 legacy Alexa skill), up from 350 at the start of
this pass, plus `golden_demo.py` and `chaos_test.py` re-run successfully
against a live local backend on the final code. Zero test skips anywhere
in the repository (`pytest -rs` across every Python suite confirms none).

---

## Correction 1 — DynamoDB stale-run resurrection

**Problem:** a stale/previous run's race state could resurrect and
overwrite the current run's displayed state via a delayed DynamoDB read,
because nothing enforced "only the currently ACTIVE run may update
displayed state."

**Fix:** an explicit active-run mechanism (`activeRunIdRef` on the Fire TV
side, an equivalent guard on the backend side) that only a genuine
new-run establishment (a reset response, a `RACE_RESET` event) may change;
an ordinary state read/poll response is never allowed to switch the
active run on its own, even if its `run_id` differs from the currently
displayed one.

**Verified by:** 8 named regression tests, covering both the in-memory and
DynamoDB repository modes — see `backend/tests/test_active_run_protection.py`
and `fire-tv/src/__tests__/useRaceState.test.ts`.

---

## Correction 2 — ECS Secrets Manager execution-role permissions

**Problem:** `template.yaml` granted `secretsmanager:GetSecretValue` on
the ECS **task role**, but ECS Fargate resolves a task definition's
`Secrets:` (the mechanism that injects a secret as an env var before the
container even starts) using the **execution role**, not the task role —
so the secret injection this template relied on would have failed at
container startup in a real deployment.

**Fix:** moved the `DemoAccessTokenRead` policy from `BackendTaskRole` to
`BackendTaskExecutionRole`; `McpTaskExecutionRole` was given an equivalent
grant for both `DemoAccessTokenSecret` and the new `McpAccessTokenSecret`
(correction 3); `McpTaskRole` now correctly has no direct AWS API
permissions of its own, since it makes none.

**Verified by:** `infrastructure/tests/test_template_security.py`'s
`TestExecutionRoleSecretPermissions` class (new this pass), plus
`cfn-lint`/`sam validate --lint`/`sam build` all passing against the
corrected template.

---

## Correction 3 — Secure the public MCP deployment

**Problem:** the deployed `mcp_server` had no dedicated authentication of
its own — either no auth at all, or (if wired carelessly) the same token
the backend's mutation endpoints use, which is exactly the kind of
credential reuse that turns one leak into two compromised systems.

**Fix:** a dedicated `McpAccessTokenSecret` (Secrets Manager, generated,
never reused from `DemoAccessTokenSecret`); `mcp_server/server.py`'s
`_AuthMiddleware` now requires a `Bearer <token>` header on every request
to the MCP path whenever `PIT_WALL_MCP_ACCESS_TOKEN` is configured (which
the template always sets for a deployed ECS service), compared with
`hmac.compare_digest` (constant-time, not `==`) to avoid a timing side
channel. `/health`/`/ready` remain reachable without auth (so the ALB
health check keeps working) and never leak the token in their response.

**Verified by:** `mcp_server/tests/test_mcp_auth.py`'s 8 new e2e tests
(missing header, malformed header, wrong token, correct token allows
`initialize`/`list_tools`/`call_tool`, health/ready reachable and
token-free, an unauthorized client is rejected from listing or invoking
tools) plus `infrastructure/tests/test_template_security.py`'s
`TestMcpAccessTokenSecret` class confirming the secret is dedicated and
execution-role-scoped, never task-role-scoped or reused.

---

## Correction 4 — Secure transport (HTTPS/WSS) as the deployment default

**Problem:** the ALB listeners could serve plaintext HTTP by default, and
nothing at deploy time forced a choice between "configure TLS" and
"explicitly accept plaintext for a local demo."

**Fix:** a new `AllowInsecureDemoHttp` parameter (default `"false"`) plus
a top-level CloudFormation `Rules:` section that refuses to deploy the
backend/mcp_server ECS services unless EITHER `CertificateArn` is set
(the required secure default) OR `AllowInsecureDemoHttp=true` was passed
explicitly. `BackendUrl`/`McpServerUrl` stack outputs derive `https://` vs
`http://` from whether a certificate was configured, and Fire TV's
`toWebSocketUrl` derives `wss://`/`ws://` from that same URL's scheme —
there is no second, independently-configured WebSocket URL anywhere that
could drift and silently downgrade to plaintext against an `https://`
backend.

**Verified by:** `infrastructure/tests/test_template_security.py`'s
`TestSecureTransportDefault` class, `fire-tv/src/__tests__/useRaceSocket.test.ts`'s
new scheme-derivation tests (https→wss, http→ws, never-plaintext-from-https),
and `cfn-lint`/`sam validate --lint`/`sam build` all passing.

---

## Correction 5 — Fire TV release authentication (device pairing)

**Problem:** a release build of the Fire TV app had no usable way to
authenticate against a real deployed backend other than a bundled/dev
credential — which correction 5's own constraints explicitly forbid
shipping in a release build.

**Fix:** a complete runtime device-pairing flow, built from scratch:
- Backend: a new `GET /v1/auth/verify` endpoint (same auth dependency as
  every other mutating endpoint) that the Fire TV app calls to validate a
  candidate token before storing it.
- Fire TV: `react-native-keychain`-backed secure storage (Android
  Keystore, never `AsyncStorage` or a plaintext file) for the paired
  token; a `useDeviceAuth` state-machine hook (`checking` →
  `unpaired`/`paired`/`pairing`); a D-pad-navigable `PairingScreen`;
  explicit UNPAIR action; a live 401 from the backend (`AuthExpiredError`)
  demotes an already-paired device back to the pairing screen rather than
  failing silently or retrying forever.
- `resolveAuthToken()` only ever falls back to the local dev token when
  `__DEV__` is true (statically `false` in a release bundle) — a release
  build can never silently use a demo credential.

**Verified by:** 21 new Fire TV tests (`secureTokenStore.test.ts`,
`useDeviceAuth.test.ts`, `PairingScreen.test.tsx`, `AppPairingGate.test.tsx`)
plus 5 new backend tests for `/v1/auth/verify` (correct/wrong/missing
token, fails closed when unconfigured, never mutates demo state) — all
passing. `npm ci`/`jest`/`tsc --noEmit`/`eslint`/`react-native config` all
run and pass against the final code (`react-native-keychain` autolinks
correctly).

**NOT fully verified — honestly recorded:** an actual Gradle
`assembleDebug`/`assembleRelease` build was attempted twice this pass
(the repo's own Gradle wrapper, and a system-installed Gradle bypassing
the wrapper download) and failed both times for confirmed,
environment-specific reasons — no Android SDK installed, and this
environment's network allowlist blocks every host Gradle needs
(`services.gradle.org`, the Gradle Plugin Portal, Maven Central, Google's
Maven repo). See `VALIDATION_RESULTS.md` §3 for the exact commands and
exact errors. This is a real gap in what could be verified here, not a
claim of a passing build.

---

## Correction 6 — Golden demo opens on lap 27, not lap 26

**Problem:** the "golden demo" narrative's undercut watch transitioned
CLOSED→OPEN on lap 26, contradicting the spec's own description of the
canonical demo narrative (lap 27), and several places in the repo's
documentation had baked in the wrong lap's numbers as if they were
correct.

**Root cause, found by direct calculation, not guesswork:**
`scenarios/scenario-demo-01.json`'s `watch_thresholds.open_probability`
was `0.65`, which put the deterministic crossing point between lap 25
(probability 0.59) and lap 26 (0.705) — one lap early.

**Fix — the smallest possible change, never a hardcoded lap check:** the
single scalar `open_probability` was tuned from `0.65` to `0.72`, which
moves the crossing to land exactly between lap 26 (0.705, still below
threshold) and lap 27 (0.775, at/above threshold). Nothing in
`backend/app/` was changed, and a repo-wide grep confirms no
`if lap == 27`-style special case exists anywhere — the correct lap
emerges from the same deterministic Monte Carlo strategy calculation
every other lap's status already came from.

**Verified by:** `backend/tests/test_strategy_engine.py`'s new
`test_golden_narrative_exact_window_and_probability_per_lap` (parametrized
over every lap 24-28, asserting the exact status AND probability
`scenario-demo-01.json` now produces) and
`test_golden_narrative_transition_lands_exactly_on_lap_27`;
`scripts/golden_demo.py` now hard-asserts `triggering_lap == 27` and
`state_version == 3` (with an explicit comment: must fail on lap 25, 26,
or 28) and was run live against a real backend subprocess, twice, both
times passing; `backend/tests/test_watch_dynamodb_repository.py`'s
DynamoDB-backed equivalent updated the same way. Every stale "lap 26"
reference this pass could find — `README.md` (4 locations, with freshly
re-captured real transcript values), `VALIDATION_RESULTS.md`,
`legacy/alexa_skill_legacy_rest/tests/test_lambda_function.py`'s mocked
narrative string — was corrected; a final repo-wide grep for "lap 26" /
"0.705" / stale margin values confirms none remain outside intentional
historical/explanatory references to the bug itself.

---

## Correction 7 — Build and verify the Android/Fire TV project

**Fix / verification performed:** `npm ci`, `npx jest --runInBand`
(104/104 passing), `npx tsc --noEmit` (clean), `npx eslint . --ext
.ts,.tsx` (clean), and `npx react-native config` (confirms
`react-native-keychain` autolinks) were all run against the final code
and all pass.

**NOT RUN, honestly recorded:** `./gradlew assembleDebug` and
`./gradlew assembleRelease` — see correction 5's own "NOT fully verified"
note above for the exact two independent reasons and exact error text
(also in `VALIDATION_RESULTS.md` §3). This correction's scope is
otherwise identical to, and satisfied by, the same work correction 5
required.

---

## Correction 8 — Fire TV real-time behavior review

**Reviewed, found already correct, given additional direct test
coverage — no functional code changes were needed.** `useRaceSocket.ts`
is the primary path (WebSocket), with `useRaceState.ts`'s REST poll loop
running unconditionally underneath as the mandated fallback — both feed
the exact same `acceptIncomingState` acceptance guard, so whichever
transport delivers a given state first "wins" and the other's delivery of
the same state is a harmless no-op. Staleness (>5s since the last message
of any kind) is tracked and surfaced via a `StaleDataBadge`; a
`RACE_RESET` event and a genuine reset both go through the single
`establishActiveRun` path (correction 1's own active-run protection);
out-of-order/replayed/stale-run state is rejected by the same guard
regardless of which transport delivered it.

**Verified by:** existing coverage in `useRaceSocket.test.ts` (primary/
fallback dispatch, reconnect with backoff, staleness) and
`useRaceState.test.ts` (out-of-order rejection, active-run switching) —
all passing; confirmed by direct code reading that `App.tsx` wires both
hooks correctly (`onRaceState`/`onReset` → `acceptIncomingState`/
`establishActiveRun`, `isStale` → the badge).

---

## Correction 9 — DynamoDB alert atomicity

**Problem found on review (not assumed — traced through an actual crash
scenario):** the original two-step write — `AlertRepository
.record_transition()` (idempotency marker + alert item) followed by a
SEPARATE `WatchRepository.update_watch()` call — left a real
crash-recovery gap against DynamoDB's durability. If the process died
between the two writes (a Lambda timeout, an OOM, a bad deploy), the
alert would be durably committed but the watch would durably keep its OLD
status. That's safely idempotent against a RETRY of the exact same
state_version, but NOT against the next, genuinely newer state_version:
evaluating it against the still-stale watch could recompute and durably
record a SECOND, duplicate alert for a transition that already fired.

**Fix:** a new `TransitionWriter` abstraction
(`backend/app/repositories/watch_base.py`) with two implementations —
`InMemoryTransitionWriter` (lock-ordered, for the demo/test single-process
mode, where a crash wipes everything symmetrically so this specific gap
can't occur) and `DynamoDBTransitionWriter`, which commits the
idempotency marker, the alert item, AND the watch update together in a
single `TransactWriteItems` call: either all three commit, or none do.
`WatchEvaluator` uses it for every status-CHANGING evaluation;
`repositories/factory.py`'s `build_transition_writer` wires the correct
one for the configured `STATE_REPOSITORY`, in both `app/main.py` (the
long-running backend) and `lambda_handlers/watch_evaluator_stream.py`
(the DynamoDB-Streams-triggered Lambda — the deployment this fix matters
most for, since a real Lambda invocation can be killed mid-execution far
more plausibly than a long-lived process). `infrastructure/template.yaml`
was updated to grant the now-required `dynamodb:TransactWriteItems`
permission to both the backend task role and the Lambda's role
(`DynamoDBCrudPolicy` does not include it).

**A real implementation bug found and fixed during this work:** the first
`DynamoDBTransitionWriter` implementation reused
`alert_repository._table.meta.client` for the raw `transact_write_items`
call — this silently double-serializes every attribute value, because a
boto3 `resource.meta.client` has the DynamoDB high-level
Python-native-to-AttributeValue transform attached to it for EVERY call
made through that client instance, not just calls made through the
`Table` object. Caught immediately by the new tests (not shipped
un-caught); fixed by giving `DynamoDBTransitionWriter` its own
independent `boto3.client("dynamodb", ...)`.

**Verified by:** `backend/tests/test_transition_writer.py` (new, 10
tests, parametrized over both backends): successful atomic commit,
idempotent replay, whole-transaction rollback on a losing watch-condition
race (proving NEITHER the alert nor the idempotency marker is left
behind — the property a two-step write cannot guarantee), a clean retry
after a rollback, and an end-to-end `WatchEvaluator` test confirming the
real wiring (not just the writer in isolation) goes through the atomic
path. `cfn-lint`/`sam validate --lint`/`sam build` confirm the new IAM
grant is valid; the full 266-test backend suite and the live
`golden_demo.py` run both pass on the final code with the atomic writer
wired in for real.

---

## Correction 10 — Deep AWS architecture validation

Beyond `cfn-lint`/`sam validate --lint`/`sam build` (all passing — see
`VALIDATION_RESULTS.md` §6), a targeted deep review covered WAF
associations, ALB/target-group configuration, Lambda retry/DLQ wiring,
X-Ray, Bedrock IAM scoping, every `Condition:`'s usage, and stack Outputs
completeness. Confirmed already correct: the WAFv2 WebACL IS associated
with both ALBs (rate-limited by source IP); listener/certificate
conditioning has no dangling reference when no certificate is supplied;
security groups correctly scope ALB↔task traffic (task security groups
only accept the container port FROM the ALB security group, never
`0.0.0.0/0`); the Watch Evaluator Lambda's DLQ is genuinely wired as its
`OnFailure` destination, not just created and unreferenced; `BedrockInvoke`
is scoped to the specific configured model ARN, not `"*"`, and resolves to
no policy at all when no Bedrock model is configured; every `Condition:`
is referenced somewhere, none are dead.

**Real gaps found and fixed:** neither target group had
`DeregistrationDelaySeconds` set (defaulting to CloudFormation's 300s,
which would make every rolling deploy slow for no benefit in a demo
stack with no long-lived connections needing a slow drain) — set to 30s
on both. The MCP target group was missing `HealthCheckIntervalSeconds`
(defaulting to 30s, inconsistent with the backend's tuned 15s) — matched
to 15s. The Watch Evaluator Lambda's `EventSourceMapping` had no
`MaximumRecordAgeInSeconds` (defaulting to -1/infinite, meaning a
persistently failing record could retry for up to the stream's full 24h
retention before ever reaching the DLQ, directly contradicting the
mapping's own "near-real-time" comment) — set to 1 hour. Missing stack
Outputs a deployer would want (`RaceStateTable.Arn`, the ECS cluster
name, both ECS service names for `describe-services`/`exec` during manual
debugging) were added.

**Honestly documented, not silently left implicit:** the backend ECS task
has an X-Ray daemon sidecar and IAM grant; `mcp_server`'s task has
neither. On inspection, this asymmetry is currently harmless — NEITHER
service's Python code actually instruments the X-Ray SDK anywhere (a
targeted grep found none outside vendored dependencies), so the
backend's own sidecar+grant are equally unused by application code today.
A code comment now records this explicitly rather than leaving a reader
to assume X-Ray tracing is active.

**Verified by:** `cfn-lint`/`sam validate --lint`/`sam build` all passing
again after every fix above; `infrastructure/tests/test_template_security.py`'s
18 tests continue to pass unchanged.

---

## Correction 11 — Build and validate Docker images

**Attempted for real, not assumed unavailable.** Unlike the first
"fixall" pass (where no Docker daemon existed in that environment at
all), THIS environment does have `docker`/`dockerd` available — `dockerd`
was started and `docker build -f backend/Dockerfile ...` and
`docker build -f mcp_server/Dockerfile ...` were both actually run. Both
failed at the very first layer, `FROM python:3.11-slim`, with a 403 from
`registry-1.docker.io`: this environment's outbound proxy allowlist
permits the standard package registries (npm, PyPI, ...) but no container
registry at all, so no base image — for ANY Dockerfile — can be pulled
here. This is a network-policy restriction, not a defect in either
Dockerfile; both were reviewed against their `requirements.txt`/env-var
contracts. `backend/Dockerfile`, `mcp_server/Dockerfile`, and
`docker-compose.yml`'s own module docstrings were updated to record the
exact command and exact error, replacing the previous (now-inaccurate)
"no Docker daemon available" claim from the first pass.

**Result: `NOT RUN`**, honestly and precisely — a genuinely different,
more specific reason than the first pass recorded, not a copy-pasted
excuse.

---

## Correction 12 — Test reliability and honesty

**Audited every `pytest.skip`/`xfail`/Jest `.skip`/`xit`/`test.todo` call
in the repository.** Found and fixed one real skip-hiding instance:
`backend/tests/test_watch_evaluator.py`'s
`test_hysteresis_band_does_not_flip_status` used to `pytest.skip(...)` if
a synthetic setup didn't force a watch OPEN as expected — silently hiding
that test's real assertions (all on the pure, deterministic
`_apply_hysteresis` function) behind what would look like nothing ran.
Replaced with a hard `assert` and an explanatory failure message: the
underlying calculation is fully deterministic, so this precondition
either always holds (confirmed: it does, every run) or there's a real
regression to surface, never an environment difference to skip past.

The two other `pytest.skip()` call sites found
(`alexa_plus_client/tests/conftest.py`, `mcp_server/tests/conftest.py`)
were reviewed and confirmed to already follow the correct, honest
pattern: they `pytest.fail()` by default when the backend virtualenv is
missing, and only skip when an explicit, clearly-named environment
variable (`ALEXA_PLUS_TESTS_ALLOW_MISSING_BACKEND_VENV=1` /
`MCP_TESTS_ALLOW_MISSING_BACKEND_VENV=1`) opts into it — this is CI
failing loudly by default, not hiding a gap.

Confirmed: no custom pytest markers exist anywhere that could be used to
selectively hide categories of tests; no `pytest-randomly` or similar
plugin is installed in any suite (test order is deterministic by
default, file/definition order); Jest has no custom test sequencer
configured either.

**Verified by:** `pytest -rs` across the full backend suite (266 tests)
after the fix confirms zero skips; the full suite re-run passes.

---

## Correction 13 — Review all claims and documentation; final packaging

This document, plus the fixes described throughout it, IS that review.
Specific documentation corrections made this pass, beyond the
per-correction notes above:
- `README.md`: 4 separate stale lap-26/probability/margin transcript
  locations corrected with freshly re-captured real values; the "five
  consecutive live runs" repeatability claim rewritten to honestly
  describe what was actually re-verified this pass (one fresh live
  capture plus new deterministic per-lap tests and a strengthened
  `golden_demo.py`), rather than repeating an unverified run count.
- `VALIDATION_RESULTS.md`: fully rewritten with this pass's real,
  current test counts and re-run results (see that file).
- `IMPLEMENTATION_STATUS.md`: a banner note added pointing to this file
  for the second pass, without altering its own historical record of the
  first pass.
- `docs/deployment-runbook.md`: Docker section corrected to describe the
  actual attempted-and-failed-for-a-specific-reason result instead of the
  stale "no daemon available" claim; deployment steps B.4/B.5 updated for
  the new dedicated MCP token; `CertificateArn`/`AllowInsecureDemoHttp`
  requirement documented for corrections 4/9.
- `backend/Dockerfile`/`mcp_server/Dockerfile`/`docker-compose.yml`:
  "HONEST STATUS" docstrings updated with the exact command and exact
  error from this pass's real `docker build` attempts.

**`REMAINING_EXTERNAL_VERIFICATION.md`** (new, this pass) consolidates
every `NOT RUN` item across both fixall passes into one place with the
exact reason and exact command needed to complete it.

**Packaging:** this repository was repackaged as
**`pit-wall-copilot-v1.1-final-fixed.zip`** after every fix above was
implemented, tested, and (where possible) run — see the top-level
delivery message for its exact location.
