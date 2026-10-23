# Local Demo Checklist — Pit Wall Copilot v1.1 (`local-demo-fixed-aws-pending`)

This checklist is the quick, honest, run-it-yourself companion to
[`LOCAL-DEMO-VALIDATION-REPORT.md`](./LOCAL-DEMO-VALIDATION-REPORT.md). It
covers **local backend functionality only**. Nothing on this checklist
proves, tests, or claims anything about a real AWS deployment.

## Before you start

- [ ] You are running this on your own machine (or this sandbox), not
      against any AWS resource.
- [ ] `backend/.env` (copied from `backend/.env.example`) has
      `STATE_REPOSITORY=memory`, `LLM_PROVIDER=template`,
      `LOCAL_DEMO_AUTH_MODE=true`, and no blank `KEY=` lines (an empty
      value is not the same as an unset one for this project's settings
      loader — delete the whole line instead of leaving it blank).
- [ ] No real AWS credentials are present in your shell environment for
      this exercise (none are needed).

## Start the backend

- [ ] `cd backend && .venv\Scripts\Activate.ps1` (or `source .venv/bin/activate`)
- [ ] `uvicorn app.main:app --host 127.0.0.1 --port 8123`
- [ ] Confirm the startup banner prints a freshly generated token and
      copy it — never a hardcoded or previously-used value.
- [ ] `$env:PIT_WALL_DEMO_ACCESS_TOKEN = "<the printed token>"`

## Run the complete local demo

- [ ] `./LOCAL-DEMO-POWERSHELL-SCRIPT.ps1`
- [ ] All 24 steps report **PASS** in the final summary.
- [ ] The token is never printed anywhere in the script's own output
      (only its character length is shown).

## Specific fixes to sanity-check yourself

- [ ] **Token rotation (issue 1):** `git grep`/`grep -r` your extracted
      release for the string `Ay-hqIqjo670hb_6X-2VpqzXJqE7_hYm` (the
      previously-exposed value) returns nothing.
- [ ] **Strategy question (issue 2):** ask
      `"Should CAR_04 pit now to undercut CAR_03?"` via
      `POST /v1/agent/query` — response `status` is `SUPPORTED`, not
      `UNSUPPORTED`, and its `evidence` matches a direct
      `POST /v1/races/demo-race-001/strategy/undercut` call for the same
      two drivers.
- [ ] **Regulation retrieval (issue 3):** `GET /v1/regulations?query=pit%20lane%20speed%20limit`
      returns `found: true` with an excerpt that says a mandatory limit
      applies but never states a numeric km/h or mph value.
- [ ] **Simulator completion (issue 4):** after `/v1/demo/start`, polling
      `/v1/demo/status` shows `RUNNING` while advancing and `COMPLETED`
      exactly once lap 28 is reached — never stuck showing `RUNNING`
      forever at lap 28, and no further `state_version` increases after
      that.
- [ ] **No real network in tests (issue 5):** `cd backend && pytest -q`
      passes without any AWS credentials configured and without internet
      egress. `pytest tests/test_llm_client.py -v` in particular shows
      the Bedrock-error test using a `Stubber`, not a real network call.
- [ ] **Complete demo script (issue 6):** `LOCAL-DEMO-POWERSHELL-SCRIPT.ps1`
      exists at the repository root, reads its token from
      `$env:PIT_WALL_DEMO_ACCESS_TOKEN`, and never hard-codes a run/watch/
      alert/calculation ID.
- [ ] **Honest documentation (issue 7):** the README's banner and this
      checklist both say, in plain language, that AWS deployment is
      still pending — never that this release is AWS-validated.

## What this checklist does NOT and CANNOT establish

- Real AWS deployment (ECS/Fargate, real ALB, real DynamoDB at scale).
- A real Bedrock model response (this demo uses the deterministic
  template explanation provider only).
- Multi-instance / multi-process behavior (`EVENT_BROKER=redis`) — this
  demo runs `event_broker=memory`, correct only for one backend process.
- A physical Fire TV device or a real Alexa+ voice journey.
- Production-grade load, failover, or rollback behavior.

See `LOCAL-DEMO-VALIDATION-REPORT.md` for the full verdict and the exact
commands/output behind every line above.
