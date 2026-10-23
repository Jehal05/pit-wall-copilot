# Telemetry roadmap (not built)

Status: **proposal only**. Nothing on this page is implemented, deployed or validated. The dashboard's Telemetry and Strategy pages run on a browser-side simulation (`web-dashboard/index.html`). This repository does not create or call any AWS resource, and the release name stays `source-validated-aws-pending`.

## What exists today
- A simulated race model in the browser (tyre wear, fuel burn, dirty air, weather, safety car).
- Engineer tools computed from that model: sector deltas, ghost lap, cliff prediction, fuel and ERS, undercut matrix, drop map, safety-car outlook, Monte Carlo, briefing.
- A simulated Alexa+ client that answers from the same functions and shows the tool calls it used.

## Proposed production shape
1. **Ingestion.** Telemetry from a sim or game feed over UDP (20 to 60 Hz) into a small gateway that normalizes packets and forwards them on a WebSocket to the dashboard. Batch to 10 Hz for the UI.
2. **Stream bus.** Redis Streams for a single-node setup, or Kafka if several consumers (strategy, radio, archive) need replay. Partition by session and car.
3. **Historical data.** FastF1 for past-session lap, tyre and weather data to calibrate degradation curves, instead of the invented constants used now. Respect its data terms.
4. **Strategy service.** The existing `StrategyEngine` fed by the stream; the browser formulas (cliff, fresh-tyre gain, drop map) would move server-side and be unit-tested against recorded sessions.
5. **Radio.** A speech-to-text step for team radio, then the same rule-based tone and action extraction. A language model could summarize, but only behind a reviewed prompt and with no quoted words attributed to real drivers.
6. **Voice.** The Alexa+ skill and a Bedrock-backed model stay **not built and not validated**; they need real AWS access that this project does not use.

## Open questions
- Licensing of any real timing source; this fan project is unofficial and uses no official data.
- Latency budget from packet to pit-wall display.
- How to evaluate recommendations (replay against recorded races, compare with actual team decisions).
