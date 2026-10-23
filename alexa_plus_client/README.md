# `alexa_plus_client/` — the primary Alexa+ integration (spec v1.1 repair 10)

**Primary architecture:**

```text
Alexa+  --Streamable HTTP MCP-->  five domain tools  -->  backend
```

`mcp_server/server.py` (repository root) already serves the required
Streamable HTTP MCP transport with exactly the five P0 domain tools
(`get_race_state`, `calculate_undercut`, `simulate_strategy`,
`create_strategy_watch`, `get_regulation` — spec v1.1 repair 7). This
directory is the other half of the integration: it plays the "Alexa+"
role against that same transport.

## What this actually is — and isn't

**There is no Alexa+ developer/onboarding account available in this
environment** (the same documented constraint as Bedrock and Fire TV
device access elsewhere in this repository — see `../`IMPLEMENTATION_STATUS.md` (removed in the repo cleanup; see git history)).
`simulated_client.py` is therefore a **simulated Alexa+ client**, clearly
labeled as such in its own module docstring: it holds a real MCP
`ClientSession` over a real Streamable HTTP connection to a real,
locally-running `mcp_server` instance, and turns a small, fixed set of
example utterances into real `tools/call` requests — a genuine
`initialize` → `tools/list` → `tools/call` round trip, with every spoken
number coming straight from the deterministic backend calculation.

What it deliberately does **not** attempt to simulate is Alexa+'s own
proprietary natural-language routing (turning arbitrary free-form speech
into the correct tool call with the correct arguments) — that is Amazon's
hosted agentic runtime, and nothing in this repository can honestly stand
in for it without an actual Alexa+ deployment. `resolve_driver_word()` is
a simple, explicitly-documented word/digit lookup standing in for what a
real Alexa+ interaction model's slot-type resolution would do.

**This client, and `../legacy/alexa_skill_legacy_rest/`, have not been
deployed to, registered with, or exercised against any real Alexa+
account, device, or voice input.** See "What a real deployment would
additionally require" below for exactly what that would take.

## The six required primary voice journeys (spec v1.1 repair 10)

All six are implemented as methods on `SimulatedAlexaPlusClient`
(`simulated_client.py`) and covered end-to-end in
`tests/test_voice_journeys.py`, each run against a real backend
subprocess and a real `mcp_server` Streamable HTTP server:

| Journey | Method | Backend surface used |
|---|---|---|
| Check undercut | `check_undercut(candidate_word, target_word)` | MCP tool `calculate_undercut` |
| Create watch | `create_watch(candidate_word, target_word)` | MCP tool `create_strategy_watch` |
| Why now? | `why_now(driver_word=None)` | `GET .../alerts` + `GET .../alerts/{id}/explain` (REST — see below) |
| PIT NOW vs WAIT 3 LAPS | `simulate_pit_now_vs_wait(driver_word, wait_laps=3)` | MCP tool `simulate_strategy` |
| Supported regulation question | `ask_regulation(query)` | MCP tool `get_regulation` |
| Unsupported regulation question | `ask_regulation(query)` (same method — see below) | MCP tool `get_regulation` |

### Why "Why now?" is REST, not MCP

Spec v1.1 repair 7 is explicit that `explain_alert` and `list_alerts`
"may remain as REST endpoints, but they must not be advertised as
additional P0 MCP domain tools." A real Alexa+ skill answering "why now"
would reach for exactly the same two REST endpoints, for exactly the
same reason — this is not a shortcut taken only by the simulation.

### Follow-up context (stable ids)

Spec v1.1 repair 10: "Preserve follow-up context using stable watch_id,
alert_id and calculation_id." `ConversationState` (in `simulated_client.py`)
holds these across turns of one simulated session — `create_watch()`
records `last_watch_id`; `check_undercut()`/`simulate_pit_now_vs_wait()`
record `last_calculation_id`; `why_now()` records `last_alert_id` and, if
no explicit driver is named, falls back to `last_watch_id` — the same
role the legacy skill's ask-sdk-core session attributes played, now owned
by this client instead.

### A voice-created watch appears on Fire TV

`create_watch()` calls the same `create_strategy_watch` MCP tool, which
calls the same `POST /v1/races/{race_id}/watches` backend endpoint Fire
TV's own WATCH button (`useInsightActions.ts`'s `openWatch`) calls. There
is nothing voice-specific about how a watch is stored — Fire TV polls
(and, since spec v1.1 repair 3, receives via `WATCH_ALERT` WebSocket
push) the exact same watch/alert records regardless of whether the watch
was created by a button press or a voice command.
`test_create_watch_journey_and_voice_created_watch_reaches_the_backend`
in `tests/test_voice_journeys.py` confirms this directly, independent of
the MCP path, by reading the created watch back with a plain backend GET.

## Running it (Windows PowerShell)

```powershell
cd alexa_plus_client
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### Run the tests

Requires `backend/.venv` to already be set up (§6 of the root README) —
these tests launch a real backend subprocess and a real `mcp_server`
Streamable HTTP server for the duration of each test.

```powershell
.venv\Scripts\Activate.ps1
python -m pytest -v
```

Expected: **8 passed**.

### Use it interactively against a backend + mcp_server you already have running

```powershell
# Terminal 1 (repository root, backend/.venv active):
cd backend; python -m uvicorn app.main:app --reload

# Terminal 2 (repository root, mcp_server/.venv active):
cd mcp_server; $env:PIT_WALL_DEMO_ACCESS_TOKEN = "<your local demo token>"; python server.py

# Terminal 3 (repository root, alexa_plus_client/.venv active):
cd alexa_plus_client
python -c "
import asyncio
from simulated_client import connected_session, SimulatedAlexaPlusClient

async def main():
    async with connected_session('http://127.0.0.1:9000') as session:
        client = SimulatedAlexaPlusClient(session, race_id='demo-race-001', backend_base_url='http://127.0.0.1:8000')
        print(await client.check_undercut('four', 'three'))
        print(await client.create_watch('four', 'three'))
        print(await client.ask_regulation('What is an undercut?'))
        await client.aclose()

asyncio.run(main())
"
```

## What a real deployment would additionally require

**Not done in this repository** — this section is best-effort guidance
based on Alexa+'s publicly documented "Actions"/agentic-tooling model as
of this repository's knowledge cutoff, not a verified, tested deployment
recipe. The exact console screens and configuration format may have
changed; consult Amazon's current Alexa+ developer documentation before
relying on this.

1. **Deploy `mcp_server/` somewhere Alexa+ can reach it over HTTPS.**
   `PIT_WALL_MCP_HOST`/`PIT_WALL_MCP_PORT` currently default to
   `127.0.0.1:9000` for local development; a real deployment needs a
   public(-ish), TLS-terminated endpoint — see `../infrastructure/`'s
   MCP Streamable HTTP / AgentCore-compatible deployment templates and
   `../README.md`'s infrastructure section for what is and isn't
   deployable here without a real AWS account.
2. **Register the MCP endpoint as an Alexa+ "Action"/tool add-on** in the
   Alexa Developer Console, pointing at that deployed URL's `/mcp` path,
   and set `PIT_WALL_MCP_ACCESS_TOKEN` (server-side) to a real secret so
   the `_AuthMiddleware` in `mcp_server/server.py` enforces it — see that
   file's own docstring for the exact bearer-token contract this server
   already implements and expects the caller to present.
3. **Set `PIT_WALL_DEMO_ACCESS_TOKEN`** on the MCP server's deployment
   environment to whatever the backend's own `DEMO_ACCESS_TOKEN` is
   configured with (spec v1.1 repair 11 — no default/bundled value
   exists for either), so `create_strategy_watch` can authenticate its
   mutating call to the backend.
4. **Test each of the six journeys above through the real Alexa+
   console simulator or a real Echo/Alexa+ device**, confirming the
   agentic runtime resolves a spoken utterance to the correct tool call
   with the correct arguments — this is the one piece this repository
   cannot verify locally (see "What this actually is" above).
5. **Do not claim real Alexa+ validation without having actually done
   step 4** — this repository has not, and says so throughout
   (`IMPLEMENTATION_STATUS.md` (removed in the repo cleanup; see git history), `VALIDATION_RESULTS.md` (removed in the repo cleanup; see git history), this file).
