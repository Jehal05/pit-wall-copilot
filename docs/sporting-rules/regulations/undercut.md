# The Undercut

Regulation-Id: reg-undercut-001
Version: 1.0

## Undercut definition

Fact-Id: reg-undercut-001-definition
Keywords: undercut, pits first, fresh tires, emerge ahead, before a rival

An undercut is a strategic pit-stop sequence in which a car pits for fresh
tires before a rival it is racing on track. Because a fresh set of tires
produces faster lap times than a worn set, the pitting car can gain enough
time on its out-lap and the following lap to emerge ahead of the rival
once the rival makes its own stop.

## Undercut window factors

Fact-Id: reg-undercut-001-window-factors
Keywords: undercut window, gap before the stop, degradation rate, pit stop cost, expected margin

Whether an undercut succeeds depends on the size of the gap between the
two cars immediately before the stop, how quickly tire performance falls
off with age for the compound in use, and how much time a pit stop costs
relative to staying on track. The strategy engine's `expected_margin_ms`
turning negative and its `window` reading CLOSED reflects an undercut that
would not succeed given these three factors.

## The overcut counter-strategy

Fact-Id: reg-undercut-001-overcut
Keywords: overcut, stays out, extra laps, older tires, clean-air lap time

The overcut is the counter-strategy to an undercut: the trailing car's
rival, instead of pitting immediately in response, stays out for one or
more additional laps on older tires, banking extra clean-air lap time
before finally pitting.
