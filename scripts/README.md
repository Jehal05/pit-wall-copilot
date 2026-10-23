# `scripts/` — golden demo, chaos, endurance and preflight scripts (spec v1.1 repair 15; phase 8 preflight added)

**One-command verification, from a clean checkout, no prior setup assumed:**

```bash
scripts/preflight.sh          # every locally-runnable check; builds backend/.venv
                               # and fire-tv/node_modules itself if missing
scripts/preflight.sh --quick  # same, minus the (unattempted-anyway-here) reminder
                               # to run chaos_test.py/endurance_test.py by hand
```

Prints one PASS/FAIL/NOT RUN table at the end covering: backend pytest,
`cfn-lint`/`sam validate --lint`, infrastructure pytest, `scripts/`'s own
pytest, Fire TV `tsc --noEmit` + Jest, the npm-audit exception-policy
gate, `golden_demo.py` against a backend it starts itself, and
`package_release.py`'s reproducible-ZIP build. A NOT RUN row (real AWS
deployment, Android release signing, a physical Fire TV device, a real
Alexa+ account) names the exact command someone with that access would
run — never silently skipped, never claimed as passing. Exit code is 0
only if every check that actually ran passed; a NOT RUN row never
affects it. `scripts/judge_demo.sh` (below) is the narrower,
judge-facing sibling of this: it only re-verifies the golden demo path
itself, fast, right before a live demo — `preflight.sh` is the full
release-readiness sweep.

These three scripts exercise a REAL, already-running backend over real
HTTP — they are not part of `backend/tests/*` (which each spin up their
own subprocess-per-test-session backend and never share state across
tests) and are not run automatically by `pytest`. Run them by hand (or
wire them into a release checklist / CI job) against a backend you
already have running, local or deployed.

| Script | What it checks |
|---|---|
| `golden_demo.py` | The canonical demo narrative, end-to-end, once: reset → create watch → start → alert fires → explain, each step asserted. |
| `chaos_test.py` | The same invariants `backend/tests/*` prove in-process, now under real concurrent HTTP load (concurrent watch creation, concurrent state reads, concurrent resets). |
| `endurance_test.py` | Repeated full cycles of the golden narrative against ONE long-running backend process, checking correctness stays consistent across cycles and watching for a latency trend that could indicate a leak. |

None of these touch AWS/DynamoDB/Lambda directly — they only ever speak
to the backend's own public HTTP API, exactly like Fire TV,
`alexa_plus_client/`, and `mcp_server/` all do.

## Setup

```powershell
cd scripts
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Running against a local backend

```powershell
# Terminal 1 (repository root, backend/.venv active):
cd backend
$env:LOCAL_DEMO_AUTH_MODE = "true"
python -m uvicorn app.main:app
# Copy the token printed once at startup ("LOCAL_DEMO_AUTH_MODE is on...").

# Terminal 2 (repository root, scripts/.venv active):
cd scripts
$env:PIT_WALL_DEMO_ACCESS_TOKEN = "<paste the token from terminal 1>"
python golden_demo.py
python chaos_test.py --concurrency 20
python endurance_test.py --cycles 10
```

## Running against a deployed backend

Set `PIT_WALL_API_BASE_URL` (default `http://localhost:8000`) and
`PIT_WALL_DEMO_ACCESS_TOKEN` (the same secret the deployment's
`DemoAccessTokenSecret` in `infrastructure/template.yaml` holds — see
`docs/deployment-runbook.md` Step A) before running any of the three
scripts above.

## What has actually been run, in this environment

All three scripts have been run against a real local backend subprocess
(`STATE_REPOSITORY=memory`, `LOCAL_DEMO_AUTH_MODE=true`) in this
environment — see `IMPLEMENTATION_STATUS.md` (removed in the repo cleanup; see git history) / `VALIDATION_RESULTS.md` (removed in the repo cleanup; see git history)
for the exact captured output. They have NOT been run against a deployed
(AWS) backend — there is no AWS account available here (the same
constraint documented throughout `docs/deployment-runbook.md`).
