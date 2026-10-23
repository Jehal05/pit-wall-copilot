# Judge quickstart

AWS hackathon-final pass, phase 9. The fastest path from a clean checkout
to watching the golden demo pass, with nothing assumed pre-installed.

## 1. One command, no setup

```bash
./scripts/judge_demo.sh
```

This builds `backend/.venv` itself if it doesn't already exist (phase 8
fix — no manual `pip install` step required first), starts a local
backend, waits for it to become healthy, then re-runs
`scripts/golden_demo.py` against it end-to-end over real HTTP. On
success it prints a demo access token and a ready-to-paste reset
command. It never claims "ready" on a partial pass — a non-zero exit
means don't demo yet, and the backend log is left in place for
inspection.

Stop the backend afterward with `./scripts/judge_demo.sh --stop`.

## 2. Want the full picture, not just the demo path?

```bash
./scripts/preflight.sh
```

Runs every check this environment can actually run — backend/
infrastructure/Fire TV test suites, `cfn-lint`/`sam validate --lint`, the
npm-audit exception gate, the golden demo, and the reproducible release
ZIP build — and prints one PASS/FAIL/NOT RUN table. The NOT RUN rows
(real AWS deploy, Android signing, a physical Fire TV device, a real
Alexa+ account) are genuine environment limitations, not skipped checks;
each names the exact command to run it for real.

## 3. Follow the script

Once the pre-flight above passes, follow **[`JUDGE_DEMO.md`](JUDGE_DEMO.md)**
(same content, also reachable as `HACKATHON_DEMO_SCRIPT.md`) — a timed
3-minute talk track that asks a live question, watches an alert fire
itself unprompted on the Fire TV app, and demonstrates the refusal
guardrail on an invalid driver.

## 4. Questions about scope

See **[`JUDGE_QA.md`](JUDGE_QA.md)** for what's real vs. simulated in
this submission, and **[`JUDGING_EVIDENCE.md`](JUDGING_EVIDENCE.md)** /
**[`FINAL_READINESS_REPORT.md`](FINAL_READINESS_REPORT.md)** for the
full, itemized, currently-verified evidence and test counts.

## Where things live, if you want to look at code

| Area | Path |
|---|---|
| Deterministic strategy engine (the numerical source of truth) | `backend/app/services/strategy_engine.py` |
| LLM explanation layer (Bedrock or template, never calculates) | `backend/app/services/explanation_engine.py` |
| Real-time WebSocket push to Fire TV | `backend/app/services/event_broker.py`, `backend/app/api/routes.py` |
| Fire TV app | `fire-tv/src/` |
| Alexa+ integration (simulated MCP client — see `JUDGE_QA.md`) | `alexa_plus_client/` |
| MCP server both clients call | `mcp_server/` |
| Infrastructure-as-code | `infrastructure/template.yaml` |
| Observability (phase 5, tracing now on both ECS services) | `backend/app/tracing.py`, `mcp_server/tracing.py`, `backend/app/metrics.py`, `docs/observability-runbook.md` |
