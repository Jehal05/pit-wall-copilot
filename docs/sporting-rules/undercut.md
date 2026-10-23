# The Undercut

Doc-Id: sr-undercut-001
Keywords: undercut, pit stop, pit window, track position, overcut, fresh tires

An undercut is a strategic pit-stop sequence in which a car pits for fresh
tires before a rival it is racing on track. Because a fresh set of tires
produces faster lap times than a worn set, the pitting car can gain enough
time on its out-lap and the following lap to emerge ahead of the rival
once the rival makes its own stop — even though the pitting car spent time
stationary in the pit lane.

The undercut is only effective within a limited window: it depends on the
size of the gap between the two cars immediately before the stop, how
quickly tire performance falls off with age for the compound in use, and
how much time a pit stop costs relative to staying on track. A car that
pits too early relative to its rival, or that trails by too large a gap,
will typically remain behind even after the rival responds — this is
reflected in Pit Wall Copilot's calculated `expected_margin_ms` turning
negative and its `window` reading `CLOSED`.

The counter-strategy is the "overcut": the trailing car's rival, instead
of pitting immediately in response, stays out for one or more additional
laps on older tires, banking extra clean-air lap time before finally
pitting. Whether an undercut attempt actually succeeds against a
rival that responds on the very next lap is exactly the counterfactual
Pit Wall Copilot's `calculate_undercut` tool simulates.
