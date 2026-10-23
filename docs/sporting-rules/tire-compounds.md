# Tire Compounds and Degradation

Doc-Id: sr-tires-001
Keywords: tire, tyre, compound, soft, medium, hard, degradation, tire age

Three dry-weather tire compounds are available for this demo series: SOFT,
MEDIUM and HARD. They trade outright pace for durability:

- **SOFT** tires are the fastest compound over a single lap when new, but
  their performance drops off the quickest as the laps accumulate.
- **MEDIUM** tires sit in between — a balanced choice for a stint of
  moderate length.
- **HARD** tires are the slowest compound when new, but degrade the most
  slowly, making them well suited to a long stint late in a race.

A tire's age — the number of laps completed since it was fitted — governs
how much time it has lost relative to when it was fresh, growing roughly
in proportion to laps run. When a car pits for a new set, its tire age
resets to zero for the following lap. Choosing which compound to fit at a
stop is itself a strategic decision that trades immediate pace (a switch
to SOFT) against durability later in the stint (a switch to HARD) — Pit
Wall Copilot's strategy engine treats this compound choice as an input to
its calculations, not something it decides on the team's behalf.
