# Yellow Flags, Safety Car and Track Status

Regulation-Id: reg-trackstatus-001
Version: 1.0

## Track status definitions

Fact-Id: reg-trackstatus-001-definitions
Keywords: green flag, yellow flag, safety car, red flag, track status

Track status has exactly four values in this fictional series: GREEN
(normal racing conditions), YELLOW (a local caution restricting overtaking
in the affected sector), SAFETY CAR (a full-course caution where a safety
car leads the field at reduced speed), and RED (the session is stopped
entirely). No other track-status value exists.

## Safety car reduces pit-loss cost

Fact-Id: reg-trackstatus-001-sc-pitloss
Keywords: safety car, compresses the field, pit loss, reduced cost, bunches up

Because a safety-car period compresses the field and slows every car
equally, a pit stop taken under a safety car costs dramatically less real
track position than the same stop taken under green-flag conditions. The
strategy engine's track-state environmental adjustment is largest under
SAFETY_CAR conditions specifically for this reason.
