# Gate 5 deployment runbook

> **Note (spec v1.1 repair 10):** this runbook's Alexa steps deploy the
> **legacy, optional, non-primary** `legacy/alexa_skill_legacy_rest/`
> classic REST-based skill. The **primary** Alexa+ integration is
> `alexa_plus_client/` (a real MCP client against `mcp_server/`'s
> Streamable HTTP transport) — see `alexa_plus_client/README.md`'s own
> "What a real deployment would additionally require" section for the
> equivalent steps for that integration instead.

*** HONEST STATUS — READ THIS FIRST ***

Gate 5's own scope (README §15) is "deploy to real infrastructure and
close the loop on what Gate 4 built but could not exercise for real."
Spec v1.1 repair 12 replaced the placeholder-Lambda `infrastructure/template.yaml`
with a real, `cfn-lint`/`sam validate`-clean SAM template and added a real
`mcp_server/Dockerfile`. **None of the steps in this document have been
executed against a real AWS account** — there is no AWS account, no Alexa
Developer Console access, no Docker daemon, and no real Echo device
reachable in the environment that built this repository (see
`backend/Dockerfile`'s, `mcp_server/Dockerfile`'s and
`infrastructure/template.yaml`'s own module-docstring notices). What *has*
been done, in that environment, and can be checked against what's in the
repo:

- `infrastructure/template.yaml` was schema/syntax-validated with
  `cfn-lint` (zero findings) and `sam validate --lint` (passes).
- `sam build` was actually run against **both** Lambda functions
  (`WatchEvaluatorStreamFunction`, `AlexaSkillLegacyFunction`) — real
  `pip install` into the build artifacts directory, real file copying
  (including the Watch Evaluator's Makefile-based build reusing
  `backend/app/` and `scenarios/` directly — see
  `infrastructure/lambda/watch_evaluator_stream/Makefile`'s own comment
  for why), and the resulting packaged `handler` function was confirmed
  importable and (with a synthetic DynamoDB Streams event and a `moto`-
  mocked table) actually invokable — see
  `backend/tests/lambda_handlers/test_watch_evaluator_stream.py`. This
  proves the packaging and application logic are correct, not that a real
  DynamoDB Streams event-source mapping invokes it correctly end-to-end.
- The Alexa Lambda packaging step below (`sam build`, equivalently
  `pip install --target` + zip) was actually run against
  `legacy/alexa_skill_legacy_rest/lambda/requirements.txt` and the
  resulting package's `lambda_function.handler` was confirmed importable.
- `docker build` for `backend/Dockerfile` and `mcp_server/Dockerfile` was
  actually **attempted** in this environment (a real Docker daemon is
  available here) and failed at the first layer, `FROM python:3.11-slim`,
  with a 403 from `registry-1.docker.io` — this environment's outbound
  network allowlist permits the standard package registries (npm, PyPI,
  ...) but no container registry, so no base image can be pulled here.
  See `backend/Dockerfile`'s module docstring for the exact command and
  error. Both Dockerfiles were written and reviewed against their
  respective `requirements.txt`/env-var contracts; building them for the
  first time is real work for whoever picks this up with an unrestricted
  Docker daemon.
- Every other command below is written against the documented AWS
  CLI/Alexa Developer Console/ask-sdk/ECS behavior and this repo's own
  actually-tested code (backend, MCP server, Alexa handler, Watch
  Evaluator Lambda all have their own real local/subprocess/moto-backed
  test suites — see the top-level README), but has not itself been run
  against real AWS.

This document exists so that whoever picks up Gate 5 with real AWS/Alexa
access has an accurate, concrete checklist — not so a reader mistakes it
for a report of what was verified. Expect first-contact surprises (a
typo'd ARN, an IAM permission this template didn't anticipate, a Bedrock
model that needs an access request approved first, a VPC/subnet id that
doesn't have the routing this template assumes) — that discovery is the
actual point of Gate 5, and no amount of local/mocked testing catches it
in advance.

---

## Prerequisites

- An AWS account with permission to create DynamoDB tables (+ Streams),
  Lambda functions, IAM roles, ECS clusters/services/task definitions,
  Application Load Balancers, WAFv2 WebACLs, Secrets Manager secrets, ECR
  repositories and CloudWatch alarms.
- An existing VPC with at least two subnets in different AZs (this
  template does not create a VPC — see `infrastructure/template.yaml`'s
  own module docstring for why).
- The AWS CLI and the SAM CLI, authenticated (`aws sts get-caller-identity`
  should work).
- A Docker daemon, to build `backend/Dockerfile` and `mcp_server/Dockerfile`.
- An Amazon developer account with Alexa Skills Kit access, for the
  Developer Console steps (legacy skill only).
- (Step D only) Bedrock model access granted for the model id in
  `BEDROCK_MODEL_ID` (`.env.example`'s default is
  `anthropic.claude-3-haiku-20240307-v1:0`) — this is a manual request/
  approval in the Bedrock console per AWS account, independent of IAM
  permissions.
- **An ACM certificate for the domain(s) you'll point at the two ALBs
  this template creates — pass its ARN as the `CertificateArn` parameter
  below.** Spec v1.1 correction 4 makes secure transport
  (HTTPS/WSS) the required deployment default: `sam deploy` /
  `cloudformation create-stack` for the backend or mcp_server service
  (i.e. any deploy that sets `BackendImageUri` and/or
  `McpServerImageUri`) is REFUSED before touching any resource — via the
  template's `Rules` section — unless you supply either a `CertificateArn`
  or the explicit `AllowInsecureDemoHttp=true` opt-in. Reserve
  `AllowInsecureDemoHttp=true` for a throwaway local/demo stack you
  understand serves plaintext HTTP/WS — never pass a bearer token
  (`DEMO_ACCESS_TOKEN`, `PIT_WALL_MCP_ACCESS_TOKEN`) to that stack over a
  public network in that mode.

---

## Step A — Provision infrastructure, then deploy the backend

Unlike the pre-repair-12 version of this runbook, the DynamoDB table, the
Watch Evaluator Lambda, the legacy Alexa Lambda, both ECR repositories,
both ALBs' networking, the WAF WebACL and the Secrets Manager secret are
all provisioned by **one** `sam deploy` — there is no separate
`aws cloudformation deploy` step for the table alone anymore.

1. **Build and deploy the stack** (`BackendImageUri`/`McpServerImageUri`
   are deliberately left unset on this first pass — see
   `infrastructure/template.yaml`'s `BackendImageProvided`/
   `McpImageProvided` conditions; the ECS services, task definitions and
   their CloudWatch alarm are skipped entirely until an image URI is
   supplied, so this first deploy only creates the table, the two
   Lambdas, the ECR repositories, the ALBs/security groups/WAF, and the
   Secrets Manager secret):

   ```bash
   cd infrastructure
   sam build
   sam deploy --guided \
     --stack-name pit-wall-copilot \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides \
       VpcId=vpc-REPLACE-ME \
       SubnetIds=subnet-REPLACE-ME-1,subnet-REPLACE-ME-2 \
       RaceId=demo-race-001
   # --guided writes a samconfig.toml so subsequent deploys can drop it.
   ```

2. **Build and push the backend image** to the ECR repository the stack
   just created (`BackendEcrRepositoryUri` in the stack outputs):

   ```bash
   docker build -f backend/Dockerfile -t pit-wall-copilot-backend .
   aws ecr get-login-password | docker login --username AWS --password-stdin "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='BackendEcrRepositoryUri'].OutputValue" --output text | cut -d/ -f1)"
   docker tag pit-wall-copilot-backend:latest "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='BackendEcrRepositoryUri'].OutputValue" --output text):latest"
   docker push "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='BackendEcrRepositoryUri'].OutputValue" --output text):latest"
   ```

3. **Re-deploy with `BackendImageUri` set**, which brings up the backend's
   ECS Fargate service, ALB listener/target group and its CloudWatch
   alarm (all previously skipped via the `BackendImageProvided`
   condition). This is the deploy the Rules section (spec v1.1
   correction 4) actually gates: it is refused unless `CertificateArn` is
   supplied here (recommended — shown below) or `AllowInsecureDemoHttp=true`
   is passed explicitly instead:

   ```bash
   sam deploy \
     --parameter-overrides \
       VpcId=vpc-REPLACE-ME \
       SubnetIds=subnet-REPLACE-ME-1,subnet-REPLACE-ME-2 \
       RaceId=demo-race-001 \
       CertificateArn=arn:aws:acm:REPLACE-ME-REGION:REPLACE-ME-ACCOUNT:certificate/REPLACE-ME \
       BackendImageUri="$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='BackendEcrRepositoryUri'].OutputValue" --output text):latest"
   # Local/demo-only alternative to CertificateArn=... above:
   #   AllowInsecureDemoHttp=true
   # — serves plain HTTP/WS on :80 instead of HTTPS/WSS on :443. Never use
   # this for anything that will carry a real bearer token over a public
   # network (see the prerequisites section's warning above).
   ```

4. **Verify**: with `CertificateArn` set, `BackendUrl` (and the curl
   commands below) use `https://`; with `AllowInsecureDemoHttp=true`
   instead, they use plain `http://` — the stack's `BackendUrl` output
   always reflects which one actually applies, so use that value rather
   than assuming a scheme. `curl <BackendUrl output>/health` should
   return `{"status": "ok"}` (see `app/api/routes.py`), and
   `curl <BackendUrl output>/v1/races/demo-race-001/state` should
   auto-initialize and return lap 24 — the same behavior already proven
   locally by the automated test suite, now over a real network hop
   through the ALB. `<BackendUrl output>` is the stack's `BackendUrl`
   output (only present once `BackendImageUri` was supplied — see step 3).

5. **Watch Evaluator sanity check**: create a watch against the deployed
   backend (`POST /v1/races/demo-race-001/watches`), advance the race
   past the known lap 24→28 CLOSED→OPEN transition (the demo's `POST
   /v1/races/demo-race-001/demo/start` does this automatically over
   real time), then `GET /v1/races/demo-race-001/alerts` — a real alert
   appearing there, without anything else being invoked directly, is the
   proof that DynamoDB Streams → `WatchEvaluatorStreamFunction` →
   `AlertRepository` actually worked end-to-end for the first time (this
   exact flow is what `backend/tests/lambda_handlers/test_watch_evaluator_stream.py`
   already proves against a **mocked** table — this step is what proves
   it against a **real** one).

---

## Step B — Deploy `mcp_server` and register it with a real MCP host

`mcp_server/server.py` already speaks the real MCP Streamable HTTP
protocol (verified in this repo's own test suite over an actual
`mcp.client.streamable_http` connection — see
`mcp_server/tests/test_streamable_http_transport.py`).

1. **Build and push the mcp_server image**, same pattern as the backend
   (`McpServerEcrRepositoryUri` stack output):

   ```bash
   docker build -f mcp_server/Dockerfile -t pit-wall-copilot-mcp-server .
   aws ecr get-login-password | docker login --username AWS --password-stdin "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='McpServerEcrRepositoryUri'].OutputValue" --output text | cut -d/ -f1)"
   docker tag pit-wall-copilot-mcp-server:latest "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='McpServerEcrRepositoryUri'].OutputValue" --output text):latest"
   docker push "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='McpServerEcrRepositoryUri'].OutputValue" --output text):latest"
   ```

2. **Re-deploy with `McpServerImageUri` set** (brings up its ECS Fargate
   service + ALB, same as backend Step A.3 — same `CertificateArn` (or
   explicit `AllowInsecureDemoHttp=true`) requirement applies here too,
   for the same reason):

   ```bash
   sam deploy \
     --parameter-overrides \
       VpcId=vpc-REPLACE-ME \
       SubnetIds=subnet-REPLACE-ME-1,subnet-REPLACE-ME-2 \
       RaceId=demo-race-001 \
       CertificateArn=arn:aws:acm:REPLACE-ME-REGION:REPLACE-ME-ACCOUNT:certificate/REPLACE-ME \
       BackendImageUri="<same value as Step A.3>" \
       McpServerImageUri="$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='McpServerEcrRepositoryUri'].OutputValue" --output text):latest"
   ```

3. **Verify**: `curl <McpServerUrl output>/health` (scheme follows
   `McpServerUrl`'s own output, same as `BackendUrl` above) should list
   exactly the 5 P0 tools (`get_race_state`, `calculate_undercut`,
   `simulate_strategy`, `create_strategy_watch`, `get_regulation`).

4. **Read the generated `PIT_WALL_MCP_ACCESS_TOKEN`** (spec v1.1
   correction 3: since this ALB is internet-facing, `template.yaml` now
   ALWAYS mints a dedicated `McpAccessTokenSecret` — a genuinely separate
   secret from the backend's `DemoAccessTokenSecret`, never reused as the
   MCP bearer token — and injects it into the MCP task's
   `PIT_WALL_MCP_ACCESS_TOKEN` env var automatically; there is nothing
   left to wire up manually here. Retrieve its value (to hand to a real
   MCP client — see step 5) with:

   ```bash
   aws secretsmanager get-secret-value \
     --secret-id "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='McpAccessTokenSecretArn'].OutputValue" --output text)" \
     --query SecretString --output text
   ```

   Every request to `/mcp` on the deployed ALB now requires
   `Authorization: Bearer <that value>` — see
   `mcp_server/server.py`'s `_AuthMiddleware` and
   `mcp_server/tests/test_mcp_auth.py` for the enforced contract
   (missing/malformed/wrong tokens all get 401 before reaching any tool;
   `/health`/`/ready` stay reachable without credentials for the ALB's own
   health checks, and never echo the token).

5. **Point `alexa_plus_client/` (or any other real MCP host) at the
   deployed URL, with the token from step 4** instead of
   `http://127.0.0.1:9000` with no auth — see
   `alexa_plus_client/README.md`'s "What a real deployment would
   additionally require" section, which this step directly satisfies its
   point 1. Never pass the backend's `DemoAccessTokenSecret` value here —
   the two tokens are intentionally not interchangeable.

---

## Step C — Create and deploy the legacy Alexa skill

1. **Package the Lambda code**: `sam build` (Step A.1) already did this
   for `AlexaSkillLegacyFunction` — no separate manual `pip install
   --target` + zip is needed anymore (the previous version of this
   runbook's manual packaging commands are superseded by SAM's own
   `PythonPipBuilder`, confirmed working in this repo's build environment
   — see the honest-status note at the top of this document).

2. **Create the skill** in the [Alexa Developer
   Console](https://developer.amazon.com/alexa/console/ask): new custom
   skill, then replace its interaction model (JSON editor) with the
   contents of `legacy/alexa_skill_legacy_rest/models/interaction-model.json`
   verbatim, and build the model.

3. **Point the skill at the Lambda** (Endpoint tab: paste the
   `AlexaSkillLegacyFunctionArn` from the stack's outputs), and grant
   Alexa permission to invoke it:

   ```bash
   aws lambda add-permission \
     --function-name "$(aws cloudformation describe-stacks --stack-name pit-wall-copilot --query "Stacks[0].Outputs[?OutputKey=='AlexaSkillLegacyFunctionArn'].OutputValue" --output text | awk -F: '{print $NF}')" \
     --statement-id alexa-skills-kit-trigger \
     --action lambda:InvokeFunction \
     --principal alexa-appkit.amazon.com \
     --event-source-token <your-skill-id-from-the-console>
   ```

4. **Test in the Console's voice simulator** first (fast iteration, no
   device needed): "open pit wall copilot", "watch the undercut for car
   four against car three", "explain the latest alert" — comparing what
   you hear against this repo's own captured local-dispatch transcripts
   (`legacy/alexa_skill_legacy_rest/tests/test_lambda_function.py` asserts
   the same responses without a real device in the loop).

5. **Test on a real Echo device or Fire TV with Alexa+** once the
   simulator round-trips cleanly, and enable the skill for your account
   if it isn't auto-available in development mode.

---

## Step D — Exercise `BedrockLLMClient` for the first time

1. In the Bedrock console, request access to the model in
   `BEDROCK_MODEL_ID` (`.env.example`'s default:
   `anthropic.claude-3-haiku-20240307-v1:0`) for your account/region —
   this is a separate approval step from IAM permissions.
2. Set `BedrockModelArn` in the stack's `--parameter-overrides` to that
   model's exact ARN and re-run `sam deploy` — this grants the backend
   ECS task role `bedrock:InvokeModel` on exactly that ARN (least
   privilege; see `BackendTaskRole`'s `HasBedrockModel`-conditioned
   policy statement — no grant at all is made until this parameter is
   set).
3. Set `LLM_PROVIDER=bedrock` (plus `BEDROCK_MODEL_ID`/`BEDROCK_REGION`
   if you want non-default values) as an additional environment variable
   on the backend task definition and redeploy the service.
4. Call `GET /v1/races/demo-race-001/alerts/{alert_id}/explain` for a
   real alert and inspect `grounding_warnings` — an empty tuple means the
   grounding guard (`explanation_engine.py`) found every number in the
   real model's narrative traceable to the given facts, exactly as it
   already does for `TemplateLLMClient` in this repo's own test suite,
   now against genuinely non-deterministic model output for the first
   time.
5. Compare the real model's phrasing against `TemplateLLMClient`'s fixed
   templates for the same alert (switch `LLM_PROVIDER` back to `template`
   and re-request the same `alert_id` — remember `/explain` now caches
   per `alert_id`, so pass a *different*, not-yet-explained alert, or
   restart the service to clear the cache, to get a fresh comparison
   rather than the first cached result both times).

---

## Step E — Android release signing

`fire-tv/android/app/build.gradle` never signs a release build with the
debug keystore. Release credentials are supplied through one of three
equivalent mechanisms — pick whichever fits your environment:

1. **A local properties file** (fastest for a one-off local release build):

   ```bash
   cd fire-tv/android/app
   cp release.keystore.properties.example release.keystore.properties
   # edit release.keystore.properties with real values — this file is gitignored
   ```

2. **Environment variables** (Unix/macOS):

   ```bash
   export PIT_WALL_RELEASE_STORE_FILE=/absolute/path/to/pit-wall-copilot-release.jks
   export PIT_WALL_RELEASE_STORE_PASSWORD='...'
   export PIT_WALL_RELEASE_KEY_ALIAS=pit-wall-copilot-release
   export PIT_WALL_RELEASE_KEY_PASSWORD='...'
   cd fire-tv/android
   ./gradlew assembleRelease
   ```

3. **Environment variables** (Windows PowerShell):

   ```powershell
   $env:PIT_WALL_RELEASE_STORE_FILE = "C:\absolute\path\to\pit-wall-copilot-release.jks"
   $env:PIT_WALL_RELEASE_STORE_PASSWORD = "..."
   $env:PIT_WALL_RELEASE_KEY_ALIAS = "pit-wall-copilot-release"
   $env:PIT_WALL_RELEASE_KEY_PASSWORD = "..."
   cd fire-tv\android
   .\gradlew.bat assembleRelease
   ```

   In CI, supply the same four names as Gradle properties backed by your
   secret store (e.g. `-PPIT_WALL_RELEASE_STORE_FILE=...`) instead of
   environment variables, so the values never appear in a shell history
   or job log.

If none of the four values are fully present, `assembleRelease` still
succeeds but produces an **unsigned** artifact — Gradle prints a warning
at configuration time, and the build never silently falls back to the
debug key. Generate a real release keystore with the standard `keytool`
command from the [React Native signed-APK docs](https://reactnative.dev/docs/signed-apk-android)
before shipping a real release build.

### Fail-closed signing for a real release pipeline (spec v1.1 pass 4, mandatory correction 9)

A local, unsigned `assembleRelease` is intentionally still allowed (it's
useful to sanity-check that a release build compiles at all without
holding real signing secrets), but a pipeline whose whole job is to
produce a *deliverable, signed* APK/AAB must never succeed while quietly
handing back an unsigned one. Set
`PIT_WALL_REQUIRE_SIGNED_RELEASE=true` (as an environment variable or
`-P` Gradle property) to turn on a hard, fail-closed gate:
`fire-tv/android/app/build.gradle` then throws a `GradleException`
*before* running any `assemble*Release`/`bundle*Release` task if the four
signing values above are not all fully configured, rather than letting
the task complete and quietly emit an unsigned artifact:

```bash
cd fire-tv/android
export PIT_WALL_REQUIRE_SIGNED_RELEASE=true
export PIT_WALL_RELEASE_STORE_FILE=/absolute/path/to/pit-wall-copilot-release.jks
export PIT_WALL_RELEASE_STORE_PASSWORD='...'
export PIT_WALL_RELEASE_KEY_ALIAS=pit-wall-copilot-release
export PIT_WALL_RELEASE_KEY_PASSWORD='...'
./gradlew assembleRelease
```

The CI workflow's `android-release` job (see
`.github/workflows/ci.yml`) sets `PIT_WALL_REQUIRE_SIGNED_RELEASE=true`
unconditionally, so a run of that job with missing/misconfigured signing
secrets fails loudly (red job, clear error message) instead of uploading
an unsigned APK as if it were a real release artifact.

After a successful signed build, verify the release certificate is not
the debug certificate:

```bash
apksigner verify --verbose --print-certs \
  fire-tv/android/app/build/outputs/apk/release/app-release.apk
```

and confirm the printed certificate fingerprint differs from
`fire-tv/android/app/debug.keystore`'s own (`keytool -list -v -keystore
fire-tv/android/app/debug.keystore -storepass android`).

Verify no debug-signing reference remains outside the debug config:

```bash
rg -n "signingConfig signingConfigs.debug" fire-tv/android/app/build.gradle
# Expected: exactly one match, inside the `debug { ... }` block.
```

---

## Step F — Pair a Fire TV device against a deployed backend

A release Fire TV build never accepts the master `PIT_WALL_DEMO_ACCESS_TOKEN`
(or its Secrets Manager equivalent) typed in on the device — it only ever
holds a short-lived, revocable, device-specific token obtained through a
one-time pairing code. Once the backend from Step A is deployed:

1. **Generate a pairing code** (admin-only — requires the master token):

   ```bash
   curl -X POST "https://<BackendUrl>/v1/auth/pairing-codes" \
        -H "X-Demo-Access-Token: <master demo access token>"
   # -> {"code": "482913", "expires_in_seconds": 300}
   ```

   The code is a 6-digit, D-pad-enterable string, valid for 5 minutes and
   single-use. The backend stores only a SHA-256 hash of it, never the
   code itself.

2. **Enter the code on the Fire TV.** On first launch (or after
   "UNPAIR"), the app shows a pairing screen with a numeric-only, 6-digit
   input — there is no field that can hold the full master secret.
   Entering the code calls `POST /v1/auth/pair`, which returns a
   device-specific token (also stored server-side only as a hash) that
   the app persists in the Android Keystore via `react-native-keychain`.

3. **Confirm the device is independently revocable** without affecting
   any other paired device:

   ```bash
   curl -X POST "https://<BackendUrl>/v1/auth/revoke" \
        -H "X-Demo-Access-Token: <master demo access token>" \
        -H "Content-Type: application/json" \
        -d '{"device_id": "<device_id from the pair response>"}'
   ```

   The Fire TV should fall back to the pairing screen the next time it
   makes an authenticated request (a real `401` from `GET /v1/auth/verify`
   or any protected mutation), and no other paired device should be
   affected.

4. A device can also self-unpair from the Fire TV's own "UNPAIR" action,
   which calls `POST /v1/auth/revoke` with its own device token (no
   master token involved) before clearing local storage.

This flow, including the rate limiting on `POST /v1/auth/pair` and the
one-time/expiring nature of the code, is covered by
`backend/tests/test_device_pairing.py` (26 tests) and the Fire TV
`useDeviceAuth.test.ts` / `PairingScreen.test.tsx` / `AppPairingGate.test.tsx`
suites; only the real-device exercise above (an actual Fire TV against a
real deployed backend) remains outside what this environment can run —
see `REMAINING_EXTERNAL_VERIFICATION.md` §1.

---

## Backup and restoration (RaceStateTable)

AWS hackathon-final pass, P1-11: `RaceStateTable` is the only durable copy
of race state, watches/alerts, and device-pairing state once
`STATE_REPOSITORY=dynamodb` is in use (see `backend/app/config.py`).
`infrastructure/template.yaml` provisions three independent layers of
protection against losing it — this section is the honest, actually-usable
procedure for each, not just a list of settings.

1. **Point-in-time recovery (PITR)** — `RaceStateTable`'s
   `PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled: true`
   gives a continuous, per-second restore window covering the trailing 35
   days (DynamoDB's fixed PITR window; not configurable). This is the
   layer that actually matters for the failure mode this table is most
   exposed to: an application bug or an operator's mistaken
   `aws dynamodb delete-item`/`update-item` call, not an AZ or region
   outage (DynamoDB's own multi-AZ replication already covers that
   regardless of PITR).

   To restore to a point in time:

   ```bash
   aws dynamodb restore-table-to-point-in-time \
     --source-table-name pit-wall-race-state \
     --target-table-name pit-wall-race-state-restored \
     --restore-date-time 2026-01-15T14:30:00Z
   ```

   **This always restores into a NEW table name** — DynamoDB cannot
   restore in place over the live table. The practical recovery procedure
   is therefore: restore into a new table, verify its contents, then
   either (a) point a maintenance-window deployment's
   `DYNAMODB_TABLE_NAME` at the restored table name and retire the old
   one, or (b) selectively copy the specific items that were lost/
   corrupted back into the live table (`aws dynamodb batch-write-item`) —
   which of the two is appropriate depends entirely on the scope of what
   needs to be recovered, and is a judgment call for whoever is running
   the restore, not something this runbook can decide in advance.
   **RPO**: effectively zero (per-second granularity, any point in the
   last 35 days). **RTO**: dominated by how long the restore itself takes
   to complete, which scales with table size — DynamoDB does not publish
   a fixed SLA for this; budget at least tens of minutes for a
   demo-scale table and verify the actual figure against the table's real
   size before relying on a specific number.

2. **`DeletionPolicy: Retain` / `UpdateReplacePolicy: Retain`** — the
   table itself is configured to survive both a stack deletion
   (`aws cloudformation delete-stack`) and a CloudFormation *replacement*
   update (the kind AWS forces for certain property changes, such as
   `KeySchema`/`AttributeDefinitions`, that cannot be applied in place).
   Neither operation deletes the underlying DynamoDB table; both leave it
   orphaned (no longer tracked by the stack) but fully intact and
   queryable. To bring an orphaned table back under stack management,
   use `aws cloudformation import-resources` (or delete the orphaned
   table manually once its data is confirmed no longer needed — this is
   never done automatically).

3. **Customer-managed KMS encryption (`RaceStateTableKey`)** — at-rest
   encryption is explicit (SSE-KMS via a dedicated key this stack
   provisions and controls) rather than the implicit AWS-owned default
   key DynamoDB otherwise uses silently. This does not change the
   PITR/backup procedures above (a PITR restore transparently re-
   encrypts under whichever key is configured at restore time), but it
   does mean: **the KMS key itself must never be scheduled for deletion
   while the table (or any of its PITR recovery points) still exists** —
   doing so makes every encrypted item permanently unreadable, which is a
   strictly worse failure mode than losing the table's CloudFormation
   management. `RaceStateTableKey` has no `DeletionPolicy` override
   (defaults to `Delete`, i.e. CloudFormation schedules key deletion if
   the resource is ever removed from the template) — treat any template
   change that would remove or replace this resource with the same
   caution as a manual `aws kms schedule-key-deletion` call, and confirm
   no other resource still depends on the key before it is ever allowed
   to proceed.

**What is NOT covered by any of this**: PITR restores the table's
*current* schema/data, not an application-level export/import — there is
no separate scheduled backup job or S3 export in this template (DynamoDB's
PITR/on-demand-backup feature makes a dedicated export job unnecessary for
this table's scale). If a genuinely offline, portable backup is ever
needed (e.g. before a risky migration), use
`aws dynamodb create-backup --table-name pit-wall-race-state --backup-name <name>`
(on-demand backup, retained until explicitly deleted, independent of the
35-day PITR window) rather than inventing an ad-hoc export script.

---

## Secret rotation (`DEMO_ACCESS_TOKEN` / `PIT_WALL_MCP_ACCESS_TOKEN`)

AWS hackathon-final pass, P2-12: neither secret has automatic Secrets
Manager rotation configured (no rotation Lambda is attached to
`DemoAccessTokenSecret`/`McpAccessTokenSecret` in `infrastructure/
template.yaml`) — for a demo/hackathon-scale deployment, a documented
manual procedure is the honest, appropriately-scoped choice over wiring
up a rotation Lambda for a secret with no real production rotation
cadence requirement yet. Rotate either token manually with the steps
below; do this immediately if a token is ever suspected to have leaked
(e.g. committed to a public repo, pasted into a shared channel).

**Important asymmetry — read this before rotating**: the two ECS
Fargate services (`BackendService`, `McpServerService`) resolve
`DEMO_ACCESS_TOKEN`/`PIT_WALL_MCP_ACCESS_TOKEN` from Secrets Manager via
the task definition's `Secrets:` block, which the ECS agent re-resolves
**every time a task starts** — so those two services pick up a rotated
value on their next deployment/restart, nothing more is needed there.
`AlexaSkillLegacyFunction` (the legacy Alexa skill Lambda; see Step C)
is different: its `PIT_WALL_DEMO_ACCESS_TOKEN` environment variable is
set via a CloudFormation **dynamic reference**
(`{{resolve:secretsmanager:...}}`), which CloudFormation resolves once,
**at stack-deploy time**, and bakes directly into the function's
configuration — rotating the underlying secret value does NOT update
this Lambda on its own. If this step is skipped, the legacy Alexa skill
silently keeps authenticating with the OLD token after a rotation, and
starts failing with 401s against the backend as soon as the new token
takes effect there.

1. **Rotate the backend/master token** (`DEMO_ACCESS_TOKEN`):
   ```bash
   aws secretsmanager update-secret \
     --secret-id "<stack-name>/demo-access-token" \
     --generate-secret-string '{"PasswordLength":32,"ExcludePunctuation":true}'
   ```
   or supply a specific value with `--secret-string` instead, if the new
   value needs to be known in advance (e.g. to hand to an administrator
   out of band).

2. **Roll the ECS services** so `BackendService` (and, if the MCP token
   was rotated instead/as well, `McpServerService`) pick up the new
   value — a plain restart is enough; no image change is required:
   ```bash
   aws ecs update-service --cluster <cluster-name> --service <backend-service-name> --force-new-deployment
   ```
   Wait for the new deployment to reach steady state
   (`aws ecs wait services-stable ...`) before treating the rotation as
   complete — until then, some running tasks still hold the OLD token.

3. **Re-deploy the CloudFormation stack** (even with an unchanged
   template — a no-op `sam deploy`/`aws cloudformation deploy` still
   forces CloudFormation to re-resolve every dynamic reference) so
   `AlexaSkillLegacyFunction` picks up the new `DEMO_ACCESS_TOKEN`. This
   step is REQUIRED after every `DEMO_ACCESS_TOKEN` rotation if the
   legacy Alexa skill is in use; it is not needed for an
   `PIT_WALL_MCP_ACCESS_TOKEN`-only rotation (that token is not used by
   this Lambda).

4. **Paired Fire TV devices are unaffected.** A device's own token
   (issued through `/v1/auth/pairing-codes` → `/v1/auth/pair`, see Step
   F) is an independent secret from the master token — see
   `require_demo_access_token`'s own docstring in
   `backend/app/api/deps.py` for why both are accepted on mutating
   endpoints, and why the master token is never itself handed to a Fire
   TV. Rotating `DEMO_ACCESS_TOKEN` does not invalidate any already-paired
   device; use `POST /v1/auth/revoke` (with the master token) to revoke a
   specific device's own token if THAT is what actually needs rotating.

5. **Verify**: `curl -H "X-Demo-Access-Token: <new-token>" .../v1/demo/status`
   returns 200; the OLD token now returns 401 from every ECS-backed
   route. If the legacy Alexa skill is deployed, exercise one intent
   through it (Step C) to confirm it picked up the new token via the
   stack re-deploy in step 3.

---

## What "Gate 5 done" looks like

- [ ] Backend reachable over HTTP(S) through its ALB,
      `STATE_REPOSITORY=dynamodb`, `/health` and a full watch → alert →
      explain round-trip work against the real table.
- [ ] The Watch Evaluator Lambda has actually fired from a real DynamoDB
      Streams event (not just the mocked test in
      `backend/tests/lambda_handlers/`) and published a real alert — Step
      A.5 above.
- [ ] `mcp_server` is reachable through its own ALB and a real MCP host
      (or `alexa_plus_client/`) lists and successfully calls all 5 tools
      against it.
- [ ] The legacy Alexa skill is live in the Developer Console, its Lambda
      endpoint responds, and the golden demo ("watch the undercut for car
      four against car three" → wait → "explain the latest alert") works
      in the voice simulator.
- [ ] The same golden demo works on a real Echo device, or the
      `alexa_plus_client/` journeys work against a real Alexa+ device —
      see `alexa_plus_client/README.md`.
- [ ] `BedrockLLMClient` has produced at least one real, grounding-guard-
      checked narrative from an actual Bedrock model call.
- [ ] WAF's rate-based rules, the CloudWatch alarms, and X-Ray tracing
      have all been observed to actually fire/populate at least once
      (a WAF block under a synthetic burst of requests against both the
      site-wide and pairing-scoped rate limits; the
      `WatchEvaluatorErrorsAlarm`/`BackendUnhealthyHostsAlarm`/
      `RaceStateTableThrottleAlarm`/`RaceStateTableSystemErrorsAlarm`/
      `PairingFailuresAlarm`/`PairingThrottlingAlarm` alarms transitioning
      state under an induced failure; an X-Ray service map showing a real
      request's trace) — provisioning these resources is not the same as
      confirming they behave as intended under real load.
- [ ] A real `restore-table-to-point-in-time` has actually been exercised
      at least once against a real (non-production) deployment of this
      table — see "Backup and restoration" above — so the restore
      procedure is proven, not just configured.
- [ ] Whatever broke on first real contact (there will be something) is
      fixed and the fix is reflected back into this repo's code/tests,
      not just patched live in the AWS console.
