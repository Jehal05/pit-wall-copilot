# Pit Wall Copilot web dashboard

A single-file browser dashboard (`index.html`, no build step) for the local demo. Fictional data only; not AWS-validated or production-ready.

- **Themes:** Broadcast (modern F1 timing-screen look) and Heritage (classic racing green and brass). Switch in the top bar.
- **Replay mode (default):** plays the lap 24 to 28 scenario using real responses recorded from the local backend (state, undercut calculation, alert, explanation, pit-lane regulation, a safe refusal). Works by opening `index.html` directly. Start, Pause, Reset, speed and a scrubber are available.
- **Live backend mode:** talks to a backend you run yourself.
  1. `cd backend && STATE_REPOSITORY=memory LLM_PROVIDER=template LOCAL_DEMO_AUTH_MODE=true uvicorn app.main:app --port 8123` (it prints a demo token).
  2. `cd web-dashboard && python -m http.server 8081` (port 8081 is an allowed CORS origin).
  3. Open http://localhost:8081, choose Live backend, paste the token, Connect, then Start.

Car spacing on the circuit comes from the real `gap_to_leader_ms` values; the circuit outline is fictional. The pit-lane rule shows the text on file and states that no number is given.

![Broadcast](screenshots/broadcast-alert.png)
![Heritage](screenshots/heritage-alert.png)
