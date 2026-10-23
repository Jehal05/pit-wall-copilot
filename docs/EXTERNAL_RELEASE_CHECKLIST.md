# External Release Checklist — exact commands for everything this environment could not verify

This is the companion to
the archived readiness report.
Every item here is marked `NOT RUN` in that report for a specific, stated
reason (no Android SDK, no container registry access, no AWS account, no
physical device). This document is the exact, copy-pasteable command list
for a person (or CI runner) with the right environment to actually run
them — running through this list end to end, all green, is what turns
"production-ready source" into a "production-verified product."

Commands are given for both Unix/macOS (bash) and Windows 11 PowerShell
where the two differ.

---

## 1. Signed Android APK build and verification

**Needs:** Android SDK + build-tools 34.0.0, JDK 17, a real release
keystore, network access to `services.gradle.org` and the Gradle Plugin
Portal (this sandbox's proxy blocks both — confirmed via a real attempted
`./gradlew help` run, `403 Forbidden`).

```bash
cd fire-tv
npm ci
cd android
./gradlew assembleDebug
# Expect: BUILD SUCCESSFUL; app/build/outputs/apk/debug/app-debug.apk

keytool -genkeypair -v -keystore /secure/path/pit-wall-copilot-release.jks \
  -alias pit-wall-copilot-release -keyalg RSA -keysize 2048 -validity 10000
export PIT_WALL_REQUIRE_SIGNED_RELEASE=true
export PIT_WALL_RELEASE_STORE_FILE=/secure/path/pit-wall-copilot-release.jks
export PIT_WALL_RELEASE_STORE_PASSWORD='...'
export PIT_WALL_RELEASE_KEY_ALIAS=pit-wall-copilot-release
export PIT_WALL_RELEASE_KEY_PASSWORD='...'
./gradlew assembleRelease
# Expect: BUILD SUCCESSFUL; app/build/outputs/apk/release/app-release.apk
# With PIT_WALL_REQUIRE_SIGNED_RELEASE=true and any of the four signing
# values missing, this MUST fail with a GradleException instead of
# producing an unsigned APK (spec v1.1 pass 4 correction 9) — verify this
# failure mode too, by unsetting one value and re-running.

apksigner verify --verbose --print-certs app/build/outputs/apk/release/app-release.apk
# Expect: "Verifies", and a certificate fingerprint.
keytool -list -v -keystore app/debug.keystore -storepass android | grep SHA256
# Confirm this fingerprint is DIFFERENT from the release APK's.
```

PowerShell equivalent: replace `export X=Y` with `$env:X = "Y"` and
`./gradlew` with `.\gradlew.bat`.

**Pass/fail:** PASS only if `assembleRelease` succeeds, `apksigner verify`
reports the APK verifies, and the release certificate fingerprint differs
from the debug keystore's.

---

## 2. Docker image build and container smoke test

**Needs:** a real Docker daemon with registry access to `docker.io`
(`python:3.11.9-slim-bookworm`). Confirmed unavailable in this sandbox
this pass (`Cannot connect to the Docker daemon`).

```bash
docker compose -f docker-compose.yml config   # already verified in this sandbox — should still pass
docker build -f backend/Dockerfile -t pit-wall-copilot-backend:local .
docker build -f mcp_server/Dockerfile -t pit-wall-copilot-mcp-server:local .

# AWS hackathon-final pass, P1-9: this sequence used to (a) remove
# backend-smoke BEFORE starting mcp-smoke, and (b) point mcp-smoke at
# http://host.docker.internal:8000 -- which only resolves to the host on
# Docker Desktop (macOS/Windows), not on plain Linux Docker Engine
# (the CI runner's actual environment) without an explicit
# `--add-host=host.docker.internal:host-gateway` flag this never passed.
# Both containers now share one explicit network and resolve each other
# by container name (exactly as docker-compose.yml's own services do),
# and the backend stays up for the FULL duration of both smoke tests —
# see .github/workflows/ci.yml's `docker-build-and-smoke-test` job for
# the automated, CI-enforced version of this same sequence (bounded-
# timeout waits, an EXIT trap that always prints both containers' logs
# before cleanup, and a real Trivy vulnerability scan of both images).

docker network create pit-wall-smoke

docker run -d --name backend-smoke --network pit-wall-smoke -p 8000:8000 \
  -e STATE_REPOSITORY=memory -e LOCAL_DEMO_AUTH_MODE=true \
  pit-wall-copilot-backend:local
curl -fsS http://127.0.0.1:8000/health
# Expect: {"status":"ok",...}
docker logs backend-smoke   # confirm no traceback, uvicorn started cleanly

docker run -d --name mcp-smoke --network pit-wall-smoke -p 9000:9000 \
  -e PIT_WALL_API_BASE_URL=http://backend-smoke:8000 \
  -e PIT_WALL_DEMO_ACCESS_TOKEN=smoke-test-token \
  pit-wall-copilot-mcp-server:local
curl -fsS http://127.0.0.1:9000/ready
# Expect: {"status":"ready","backend":"reachable"} -- NOT just /health,
# which would "pass" even with an unreachable backend (see
# mcp_server/server.py's build_streamable_http_app docstring).
docker logs mcp-smoke

docker rm -f backend-smoke mcp-smoke
docker network rm pit-wall-smoke
```

Then, scan both built images for known vulnerabilities (the same check
CI now runs as a hard gate — see `docker-build-and-smoke-test` above):
```bash
trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed pit-wall-copilot-backend:local
trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed pit-wall-copilot-mcp-server:local
```

Then, to pin the base image by digest rather than tag (spec v1.1 pass 4
correction 8's own noted remaining step):
```bash
docker pull python:3.11.9-slim-bookworm
docker inspect --format='{{index .RepoDigests 0}}' python:3.11.9-slim-bookworm
# Replace `FROM python:3.11.9-slim-bookworm` in backend/Dockerfile and
# mcp_server/Dockerfile with `FROM python@sha256:<digest>`.
```

**Pass/fail:** PASS only if both images build without error and both
containers report a healthy `/health` response.

---

## 3. AWS deployment (SAM/CloudFormation, ECS, DynamoDB, Lambda, Streams)

**Needs:** a real AWS account, an existing VPC + ≥2 subnets, and (for a
secure deployment — the required default) an ACM certificate ARN.

```bash
cd infrastructure
sam build
sam deploy --guided \
  --parameter-overrides \
    VpcId=vpc-xxxxxxxx \
    SubnetIds=subnet-xxxxxxxx,subnet-yyyyyyyy \
    CertificateArn=arn:aws:acm:...:certificate/xxxx \
    BackendImageUri=<pushed backend ECR image URI> \
    McpServerImageUri=<pushed mcp_server ECR image URI>
```

After deploy, verify each of these for real (not by re-reading the
template):
- `aws dynamodb describe-table --table-name <TableName>` — confirm
  `StreamSpecification` and `TimeToLiveSpecification` are both active.
- `aws ecs describe-services --cluster <cluster> --services <backend
  service>` — confirm `desiredCount`/`runningCount` match and
  `deploymentConfiguration.deploymentCircuitBreaker.enable == true`.
- Trigger a rolling deployment with a deliberately broken image (e.g. one
  that fails its health check) and confirm the circuit breaker actually
  rolls the service back automatically (spec v1.1 pass 4 correction 8).
- Pair a device against the deployed backend, restart the ECS task (`aws
  ecs update-service --force-new-deployment`), and confirm the
  previously-paired device token still works — the actual, real-AWS
  version of what `test_pairing_dynamodb.py`'s moto-simulated tests
  already prove logically.
- Confirm the DynamoDB Streams-triggered Watch Evaluator Lambda actually
  fires and produces an alert: watch CloudWatch Logs for
  `<stack>-watch-evaluator-stream` while driving the deployed demo through
  a lap that should trigger one.
- Confirm WAF, the two CloudWatch alarms, and X-Ray tracing are all
  receiving real data (AWS Console or `aws cloudwatch describe-alarms` /
  `aws xray get-trace-summaries`).

**Pass/fail:** PASS only after every item above is confirmed against the
real, deployed stack — a clean `sam deploy` alone is necessary but not
sufficient.

---

## 4. Physical Fire TV device: pairing, reconnect, WebSocket events

**Needs:** a physical Amazon Fire TV device (or Fire TV emulator with real
network access), the signed release APK from §1, and a deployed backend
from §3 (or a local backend reachable from the device's network).

1. Sideload the release APK; launch it; confirm the pairing screen
   appears (no stored device token yet).
2. On an authorized admin machine: `POST /v1/auth/pairing-codes` with the
   master token; read the returned 6-digit code aloud/type it into the
   Fire TV pairing screen.
3. Confirm pairing succeeds and the device token is stored (Fire TV
   proceeds past the pairing screen).
4. **Reconnect-after-reset (spec v1.1 pass 4 correction 2):** with the app
   open on run A, kill the device's Wi-Fi, trigger a backend reset (now
   run B) while disconnected, then restore Wi-Fi. Confirm the app
   recovers to run B automatically, without a stale run-A overlay/alert
   surviving.
5. **Every WebSocket event (correction 4):** trigger each of `RACE_RESET`,
   `WATCH_ALERT`, `STRATEGY_UPDATE`, `AGENT_RESPONSE` from the backend/
   Alexa+ side and confirm each causes the correct visible change on the
   actual TV screen (not just in a unit test).
6. **Release transport security (correction 5):** confirm (e.g. via a
   network proxy on the same LAN) that the release app never sends a
   request over plaintext HTTP/WS, including during unpair.
7. Unpair the device via the Fire TV UI; confirm it returns to the pairing
   screen and the stored token is gone.

**Pass/fail:** PASS only after all seven steps are confirmed on the actual
device.

---

## 5. Real Alexa+ voice journeys

**Needs:** a real Alexa+-enabled device/console, the deployed `mcp_server`
ECS service from §3 (internet-facing, bearer-token-protected), and the
deployed backend.

Run each of the six required voice journeys for real (the simulated
client's 8 automated tests already prove the logic; this proves the real
voice pipeline):
1. "Check the undercut for car 4."
2. "Create a watch for [condition]."
3. "Why now?" (after an alert has fired).
4. "Should I pit now or wait?"
5. A supported sporting-regulations question.
6. An unsupported/hallucination-trap regulations question (must decline,
   never invent an answer).

Also confirm: an `AGENT_RESPONSE` triggered by one of these voice queries
appears on a Fire TV that is simultaneously connected and watching the
same race (the live version of `AppWebSocketEvents.test.tsx`'s automated
proof).

**Pass/fail:** PASS only after all six journeys succeed against the real
voice pipeline, plus the cross-device AGENT_RESPONSE confirmation.

---

## 6. Multi-hour endurance testing

**Needs:** a long-running backend (local or deployed) and enough wall-clock
time.

```bash
cd scripts
export PIT_WALL_API_BASE_URL=<backend URL>
export PIT_WALL_DEMO_ACCESS_TOKEN=<token>
python endurance_test.py --duration-seconds 14400   # 4 hours
```
Watch for: memory growth, latency growth, any error after the first
several hundred cycles. The `endurance` job in `.github/workflows/ci.yml`
runs a short, fixed-cycle version on every push and a
`workflow_dispatch`-triggered, longer configurable-duration version on
demand — this multi-hour run is a superset of that, run manually.

**Pass/fail:** PASS only if latency and memory stay flat (no unbounded
growth) across the full duration with zero unhandled errors.

---

## 7. Rollback procedure

If a deployed release needs to be rolled back:

1. **ECS services (backend, mcp_server):** the deployment circuit breaker
   (Correction 8) already auto-rolls-back a deployment that never reaches
   a healthy steady state. For a deployment that DID succeed but is later
   found to be bad: `aws ecs update-service --cluster <cluster> --service
   <service> --task-definition <previous-task-def-arn>` to roll back to
   the last known-good task definition (ECS keeps prior revisions).
2. **DynamoDB:** no destructive schema migration exists in this pass — no
   DynamoDB-specific rollback is needed for these changes. If point-in-time
   recovery is enabled on the table (not currently provisioned by
   `template.yaml` — a documented gap, not a claim it exists),
   `aws dynamodb restore-table-to-point-in-time` would be the mechanism
   for accidental data loss.
3. **Android release:** if a bad signed APK reaches distribution, halt
   the rollout at whatever distribution channel is in use (Amazon Appstore
   console, MDM push, etc.) and re-publish the previous known-good signed
   APK — this repository does not itself manage app-store distribution.
4. **Backend/mcp_server release ZIP:** `pit-wall-copilot-v1.1-production-ready-final.zip`
   (this pass's deliverable) is additive over
   `pit-wall-copilot-v1.1-production-ready.zip` (pass 3's) — reverting to
   the prior ZIP and redeploying its container images is always available
   as a full rollback if this pass's changes need to be backed out
   entirely.

## 8. Backup and recovery considerations

- **DynamoDB** is the single source of durable state (race state,
  watches, alerts, pairing codes, device tokens). Enable point-in-time
  recovery (`aws dynamodb update-continuous-backups
  --point-in-time-recovery-specification Enabled=true`) before real
  production traffic — not currently provisioned by `template.yaml`.
- **Secrets** (`DemoAccessTokenSecret`, `McpAccessTokenSecret`) are
  generated and owned by Secrets Manager; back up by exporting/rotating
  through Secrets Manager's own versioning, never by copying the plaintext
  value anywhere.
- **CloudWatch Logs** (30-day retention on both log groups) are the audit
  trail for `audit_log`'s structured events (Correction 7) — export to
  long-term storage (S3 + Glacier, or a SIEM) before 30 days if longer
  retention is a compliance requirement; this repository does not
  currently provision that export.

---

## 9. Actionlint (GitHub Actions workflow syntax)

No `actionlint` binary is available in this sandbox. Before relying on
`.github/workflows/ci.yml` in a real repository:
```bash
# Install: https://github.com/rhysd/actionlint
actionlint .github/workflows/ci.yml
```
This sandbox validated the file only as generic YAML
(`python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
— parses cleanly) and by manually tracing each job's commands against
ones already proven to work live in this session (see
`FINAL_PRODUCTION_READINESS_REPORT.md` §10). A real run of the workflow on
an actual GitHub Actions runner is the definitive verification.
