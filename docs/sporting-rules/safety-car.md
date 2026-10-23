# Yellow Flags, Safety Car and Track Status

Doc-Id: sr-trackstatus-001
Keywords: safety car, yellow flag, red flag, track status, caution, green flag

Track status governs how fast cars are permitted to travel and materially
changes pit-stop economics:

- **GREEN** — normal racing conditions; no additional restriction.
- **YELLOW** — a local caution, typically for an incident or hazard;
  overtaking is restricted in the affected sector and cars must be
  prepared to slow.
- **SAFETY CAR** — a full-course caution in which a safety car leads the
  field at reduced speed; the entire field bunches up behind it, which
  sharply reduces the time penalty of pitting during that period, since
  every car on track is running far slower than racing pace regardless of
  whether it stops.
- **RED** — the session is stopped entirely.

Because a safety-car period compresses the field and slows every car
equally, a pit stop taken under a safety car costs dramatically less real
track position than the same stop taken under green-flag conditions. Pit
Wall Copilot's strategy engine reflects this by applying a track-state
environmental adjustment to its lap-time model, which is largest under
SAFETY_CAR conditions — teams and Alexa+ users should read a strategy
recommendation's window status differently once a safety car is called,
since the underlying calculation already accounts for it.
