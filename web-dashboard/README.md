# Pit Wall Copilot web dashboard

A single-file browser dashboard (`index.html`, no build step) for the local demo. Fictional data only; not AWS-validated or production-ready.

- **Themes:** Grand Prix (bright F1.com-style white, red and navy) and Heritage (classic racing green and brass, light). Switch in the top bar.
- **Copilot panel (Alexa+ simulated):** answers stream in with a visible tool-call trace (the real engine, agent and regulation calls behind each answer), a team-radio style proactive alert when the window opens, and a "Show the call" pit-lane animation of the engine's projected outcome. Also: live telemetry strip, probability and gap trend charts, and a race-control feed.
- **Race simulator (new, browser model):** a lap-by-lap simulation from lap 25 to 58 seeded from the scenario's real cars (positions, gaps, tyres, base pace) and the engine's pit plan. It models tyre wear and cliff, fuel burn, dirty air, pit loss, safety cars and overtaking odds, so cars really pass each other, pit and rejoin. Controls: seed, OKA strategy (pit lap 29 or 36), speed, and a Safety car what-if. Includes a lap chart, lap-time chart, tyre wear bars, race-control feed, spoken team radio and a classification at the flag. Same seed gives the same race. It is an independent illustrative model, separate from the backend strategy engine, and not AWS-validated.
- **Voice:** type, tap a phrase or use the mic (browser speech recognition where supported); answers can be spoken aloud, and a proactive notification fires when the undercut window opens. Not a real Alexa device.
- **Replay mode (default):** plays the lap 24 to 28 scenario using real responses recorded from the local backend (state, undercut calculation, alert, explanation, pit-lane regulation, a safe refusal). Works by opening `index.html` directly. Start, Pause, Reset, speed and a scrubber are available.
- **Live backend mode:** talks to a backend you run yourself.
  1. `cd backend && STATE_REPOSITORY=memory LLM_PROVIDER=template LOCAL_DEMO_AUTH_MODE=true uvicorn app.main:app --port 8123` (it prints a demo token).
  2. `cd web-dashboard && python -m http.server 8081` (port 8081 is an allowed CORS origin).
  3. Open http://localhost:8081, choose Live backend, paste the token, Connect, then Start.

Car spacing on the circuit comes from the real `gap_to_leader_ms` values; the circuit outline is fictional. The pit-lane rule shows the text on file and states that no number is given.

![Grand Prix](screenshots/grand-prix-simulator-safety-car.png)
![Heritage](screenshots/heritage-simulator.png)
