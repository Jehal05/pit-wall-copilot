# Tire Compounds and Degradation

Regulation-Id: reg-tires-001
Version: 1.0

## Available compounds

Fact-Id: reg-tires-001-compounds
Keywords: compound, soft, medium, hard, available, dry, three

Three dry-weather tire compounds are available in this fictional demo
series: SOFT, MEDIUM and HARD. SOFT is the fastest compound over a single
lap when new but degrades the quickest; HARD is the slowest when new but
degrades the most slowly; MEDIUM sits between the two. No other dry
compound exists in this series.

## Tire age reset on a pit stop

Fact-Id: reg-tires-001-age-reset
Keywords: tire age, reset, pit stop, new set, zero

A tire's age is the number of laps completed since it was fitted. When a
car pits and fits a new set of tires, that car's tire age resets to zero
for the following lap. There is no partial or gradual reset — the reset
is immediate and complete on the out-lap.

## Degradation grows with tire age

Fact-Id: reg-tires-001-degradation-growth
Keywords: degradation, tire age, growth, laps, wear, proportion

Degradation — the time a tire has lost relative to when it was fresh —
grows roughly in proportion to the number of laps completed on that set.
Pit Wall Copilot's strategy engine applies this relationship uniformly
across the whole life of a set; there is no separate rule, threshold, or
special-cased lap number at which the degradation model changes, resets,
is ignored, or is replaced by a different calculation.
