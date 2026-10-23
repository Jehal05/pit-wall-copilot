# Judge Demo — 3-Minute Script

This is the live-presentation talk track for judging. It follows the
exact deterministic "golden path" (lap 24 → lap 27 undercut alert → lap
28 completion) that `scripts/golden_demo.py` verifies end-to-end over
real HTTP, and that `backend/tests/test_websocket_events.py` /
`README.md`'s own captured transcript document byte-for-byte. Nothing
below is aspirational — every number quoted is the actual output of a
real, already-tested run.

**Before judges arrive**, run the one-command pre-flight check so the
live demo never stalls on an environment problem in front of judges:

```bash
./scripts/judge_demo.sh
# Starts a local backend (if one isn't already up), waits for /health,
# then re-runs scripts/golden_demo.py end-to-end against it. Prints the
# demo token and a ready-to-paste reset command on success. Exits
# non-zero (and leaves the backend log for inspection) if anything in
# the golden path fails — never claims "ready" on a partial pass.
#
# Stop it afterward with: ./scripts/judge_demo.sh --stop
```

To verify against an already-deployed AWS stack instead of a local
backend, set the token and pass `--base-url` (see
`docs/deployment-runbook.md` for how to read the deployed token out of
Secrets Manager):

```bash
PIT_WALL_DEMO_ACCESS_TOKEN=<deployed-token> ./scripts/judge_demo.sh --base-url https://<BackendUrl-output>
```

This has been run end-to-end in this environment (local backend, real
HTTP, not a mock) and passes: reset → watch created CLOSED → demo started
→ alert fires at lap 27/state_version 3 → grounded explanation returned
with zero grounding warnings.

---

## 0:00–0:30 — The problem, in one breath

> "A race engineer watching for an undercut window has to mentally track
> tire age, gap, and pit-lane time loss for every rival, every lap, while
> also talking on the radio. Pit Wall Copilot does that tracking
> continuously and only interrupts with a **voice alert** the moment a
> strategy window actually opens — with the exact numbers behind the
> call, never a black-box 'trust me.'"

Show the Fire TV screen (or Alexa+ device) idle on lap 24, Car 4 chasing
Car 3.

## 0:30–1:15 — Ask it a question, get a grounded answer

On Alexa+ (or the Fire TV agent panel), ask: **"Is there an undercut
window for Car 4 on Car 3?"**

Backend call being made live (same one `README.md`'s captured transcript
shows):

```
POST /v1/races/demo-race-001/strategy/undercut
{"candidate_driver": "CAR_04", "target_driver": "CAR_03"}
→ window=CLOSED expected_margin_ms=-320 confidence.probability_ahead=0.455
```

> "Right now it's CLOSED — Car 4 would come out 320ms *behind*. This
> isn't a canned line; it's a live Monte-Carlo simulation (200 trials)
> against the current gap and tire ages, and the voice answer you just
> heard cites these exact numbers — the LLM narrates, it never invents a
> number that isn't in this response."

## 1:15–2:15 — Watch the alert fire itself, unprompted

Let the demo race continue running (it was already `/v1/demo/start`ed
pre-session). As it crosses lap 27, a **WATCH_ALERT** pushes over the
authenticated WebSocket to the Fire TV screen, with no new question
asked. **Correction (phase 6 accuracy pass):** this push is real and
verified end-to-end (see `fire-tv/src/__tests__/AppWebSocketEvents.test.tsx`
and `scripts/golden_demo.py`'s own alert-arrives-unprompted assertion),
but it reaches the **Fire TV app only** — there is no proactive-push
channel to Alexa+ anywhere in this codebase (Alexa+ integrates only as a
*reactive* MCP tool caller — see `alexa_plus_client/README.md` — it
answers a question when asked, it does not receive unprompted
notifications). Do not tell judges Alexa+ will notify them; if
demonstrating on an Alexa+ device, ask "why now?" *after* the Fire TV
screen shows the alert to get the same grounded explanation reactively.

```
lap=27 state_version=3 → watch flips CLOSED → OPEN
window=OPEN expected_margin_ms=1385 confidence.probability_ahead=0.775
```

> "The window just opened on its own — the Fire TV app didn't poll for
> this, the backend pushed it the moment the simulation crossed the
> threshold. Ask 'why now?' — on the Fire TV panel, or reactively via
> Alexa+ — and the answer cites the *exact* lap-27 snapshot — and that
> snapshot is now frozen: even after the race moves to lap 28, re-fetching
> this alert returns the identical lap=27, state_version=3 evidence it
> fired on. The evidence behind a call never quietly changes after the
> fact."

(Optional, if time and a real AWS account are available:) point out the
`Explanation` came from a real Bedrock `invoke_model` call
(`LLM_PROVIDER=bedrock`), not the deterministic template fallback, and
that it ran off the request thread (`run_in_threadpool`) so it never
blocked any other judge's simultaneous demo session on the same backend.

## 2:15–2:45 — The guardrail that matters most

> "One more thing, because trust matters more than a flashy demo: ask it
> for a strategy call on a driver that doesn't exist."

```
POST /v1/races/demo-race-001/strategy/undercut
{"candidate_driver": "CAR_99", "target_driver": "CAR_03"}
→ HTTP 422 Unprocessable Entity
```

> "It refuses outright — 422, not a guess, not a hallucinated number. The
> same discipline applies to every number the LLM narrates: `ExplanationEngine`
> independently re-checks every figure the model returns against the
> grounded facts it was given, so the model can phrase a sentence, but it
> can never invent a lap time, a margin, or a probability."

## 2:45–3:00 — Close

> "That's the core loop: continuous, grounded simulation; proactive,
> evidence-frozen alerts pushed to the Fire TV app; reactive voice access
> through Alexa+ and the Fire TV agent panel; and a hard refusal to ever
> present a number it can't trace back to real race data. It's built to
> run on ECS Fargate behind WAF-rate-limited ALBs, DynamoDB-backed with
> point-in-time recovery, and every credential is a Secrets-Manager-
> generated, constant-time-compared token — validated by `cfn-lint`/`sam
> validate` but not yet exercised against a real AWS account; see
> `AWS_ARCHITECTURE.md` for the full picture."

---

## If something goes wrong live

- **WebSocket alert doesn't fire in time**: fall back to
  `GET /v1/races/demo-race-001/watches/{watch_id}` polling on screen — the
  REST fallback this same app already uses when a WebSocket drops (see
  `fire-tv/src/hooks/useRaceState.ts`'s documented 2s-poll fallback).
  Narrate: "the app itself falls back to this exact same poll within 5s if
  the socket ever drops — you're seeing the resilience path, not a bug."
- **Backend not reachable**: `curl -i $PIT_WALL_API_BASE_URL/health` — if
  that fails, restart with the pre-flight command at the top of this
  document. `scripts/golden_demo.py`'s exit code is the fastest way to
  confirm the whole path (not just `/health`) is actually sound before
  judges see anything.
- **A question judges ask that isn't in this script**: the agent/Alexa+
  surface answers open-ended race-state questions too (`POST
  /v1/agent/query`), not just the two canned strategy calls above — it's
  safe to take a live, unscripted question.

## Reset between judge groups

```bash
curl -s -X POST $PIT_WALL_API_BASE_URL/v1/demo/reset -H "X-Demo-Access-Token: $TOKEN"
```

Returns the demo race to lap 24, state_version 0, with a fresh `run_id` —
every client (Fire TV, Alexa+) picks this up automatically over the
WebSocket's `RACE_RESET` event, no app restart needed.
