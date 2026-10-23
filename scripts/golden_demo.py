#!/usr/bin/env python3
"""Golden demo script (spec v1.1 repair 15).

Drives the canonical Pit Wall Copilot demo narrative against a REAL,
already-running backend (local `uvicorn app.main:app`, or a deployed one —
set PIT_WALL_API_BASE_URL) end-to-end over real HTTP, and prints a
line-by-line transcript with PASS/FAIL verdicts. This is the same
narrative every client (Fire TV, alexa_plus_client, the legacy Alexa
skill) exercises through its own UI/voice surface; this script exists so
that narrative can be rehearsed and sanity-checked directly against the
backend, independent of any one client, before a real demo — and so CI or
a release checklist has one command that either confirms "the golden path
still works" or fails loudly.

Usage:
    PIT_WALL_DEMO_ACCESS_TOKEN=<token> python scripts/golden_demo.py
    PIT_WALL_API_BASE_URL=https://api.example.com PIT_WALL_DEMO_ACCESS_TOKEN=<token> python scripts/golden_demo.py

Exit code 0 on success, 1 if any step's assertion fails or the alert never
fires within the timeout — never a silent partial pass.
"""

from __future__ import annotations

import sys
import time

from _client import DEFAULT_BASE_URL, DEFAULT_RACE_ID, build_client, eprint, require_demo_access_token

ALERT_WAIT_TIMEOUT_SECONDS = 20
ALERT_POLL_INTERVAL_SECONDS = 0.5


class GoldenDemoFailure(RuntimeError):
    pass


def _step(label: str) -> None:
    print(f"\n=== {label} ===")


def _assert(condition: bool, ok_message: str, fail_message: str | None = None) -> None:
    if not condition:
        raise GoldenDemoFailure(fail_message or ok_message)
    print(f"  OK: {ok_message}")


def run(base_url: str, race_id: str, demo_access_token: str) -> None:
    client = build_client(base_url, demo_access_token=demo_access_token)

    _step("Health check")
    response = client.get("/health")
    _assert(response.status_code == 200, "GET /health returned 200", f"GET /health returned {response.status_code}")
    _assert(
        response.json().get("status") == "ok",
        "/health body reports status=ok",
        f"unexpected /health body: {response.json()}",
    )

    _step("Reset the demo (fresh run_id, fixed lap-24 baseline)")
    response = client.post("/v1/demo/reset")
    _assert(
        response.status_code == 200,
        "POST /v1/demo/reset returned 200",
        f"POST /v1/demo/reset returned {response.status_code}: {response.text}",
    )
    baseline_state = response.json()
    _assert(
        baseline_state["lap"] == 24,
        "reset returned the fixed lap-24 baseline",
        f"expected the fixed lap-24 baseline, got lap {baseline_state['lap']}",
    )
    run_id = baseline_state["run_id"]
    print(f"  run_id = {run_id}")

    _step("Create a watch: CAR_04 undercut against CAR_03 (spec's own canonical pairing)")
    response = client.post(
        f"/v1/races/{race_id}/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03", "watch_type": "UNDERCUT"},
    )
    _assert(
        response.status_code == 200,
        "create_strategy_watch returned 200",
        f"create_strategy_watch returned {response.status_code}: {response.text}",
    )
    watch = response.json()
    watch_id = watch["watch_id"]
    _assert(
        watch["status"] == "CLOSED",
        "new watch's baseline status is CLOSED",
        f"expected the known lap-24 baseline status CLOSED, got {watch['status']}",
    )
    print(f"  watch_id = {watch_id}")

    _step("Start the demo (advances the race in real time toward the known lap-28 transition)")
    response = client.post("/v1/demo/start")
    _assert(
        response.status_code == 200,
        "POST /v1/demo/start returned 200",
        f"POST /v1/demo/start returned {response.status_code}: {response.text}",
    )

    _step(f"Poll for the watch to open and an alert to publish (up to {ALERT_WAIT_TIMEOUT_SECONDS}s)")
    deadline = time.monotonic() + ALERT_WAIT_TIMEOUT_SECONDS
    alert = None
    while time.monotonic() < deadline:
        response = client.get(f"/v1/races/{race_id}/alerts", params={"watch_id": watch_id})
        _assert(
            response.status_code == 200,
            "GET .../alerts returned 200",
            f"GET .../alerts returned {response.status_code}: {response.text}",
        )
        alerts = response.json()
        if alerts:
            alert = alerts[0]
            break
        time.sleep(ALERT_POLL_INTERVAL_SECONDS)
    _assert(
        alert is not None,
        "an alert was published before the timeout",
        "no alert published before the timeout — the golden path did not complete",
    )
    _assert(
        alert["previous_status"] == "CLOSED",
        "the watch's previous status was CLOSED",
        f"expected previous_status CLOSED, got {alert['previous_status']}",
    )
    _assert(
        alert["new_status"] == "OPEN",
        "the watch transitioned to OPEN",
        f"expected the watch to transition to OPEN, got {alert['new_status']}",
    )
    # Spec v1.1 correction 6: the golden narrative's CLOSED->OPEN
    # transition SHALL fire on lap 27 (never lap 25, 26, or 28) —
    # this is the exact assertion the spec's fix instruction calls for:
    # "the script must fail if the alert happens on lap 25, 26, or 28."
    # This lap value emerges from the deterministic strategy calculation
    # against scenarios/scenario-demo-01.json's watch_thresholds
    # (open_probability=0.72) — nothing in this script or the backend
    # hardcodes "lap 27" anywhere; this assertion is what PROVES that
    # value rather than assuming it.
    triggering_lap = alert["triggering_snapshot"]["lap"]
    _assert(
        triggering_lap == 27,
        "the alert's triggering snapshot is lap 27",
        f"expected the alert to fire on lap 27 exactly, got lap {triggering_lap} "
        "(spec v1.1 correction 6: the transition must land on lap 27, never 25/26/28)",
    )
    _assert(
        alert["state_version"] == 3,
        "the alert's state_version is 3 (lap 24=v0, 25=v1, 26=v2, 27=v3)",
        f"expected state_version 3, got {alert['state_version']}",
    )
    print(f"  alert_id = {alert['alert_id']}")
    print(f"  triggering lap = {triggering_lap}, state_version = {alert['state_version']}")

    _step("Explain the alert (grounded narrative, spec sections 33-35)")
    response = client.get(f"/v1/races/{race_id}/alerts/{alert['alert_id']}/explain")
    _assert(
        response.status_code == 200,
        "GET .../explain returned 200",
        f"GET .../explain returned {response.status_code}: {response.text}",
    )
    explanation = response.json()
    _assert(bool(explanation.get("narrative")), "explanation narrative is non-empty", "explanation narrative was empty")
    _assert(
        explanation.get("grounding_warnings") == [],
        "grounding guard flagged no unsupported numbers",
        f"grounding guard flagged unsupported numbers: {explanation.get('grounding_warnings')}",
    )
    print(f"  narrative: {explanation['narrative']!r}")

    # Spec v1.1's required verification-commands section names this exact
    # summary block as the golden demo's expected output — printed here
    # verbatim (in addition to, not instead of, the more verbose
    # step-by-step transcript above) so a reviewer or CI log can grep for
    # it directly.
    _step("Summary")
    print(f"Reset lap: {baseline_state['lap']}")
    print(f"Initial watch status: {watch['status']}")
    print(f"Alert lap: {triggering_lap}")
    print(f"Previous status: {alert['previous_status']}")
    print(f"New status: {alert['new_status']}")
    print("Golden demo PASSED end-to-end")


def main() -> int:
    base_url = DEFAULT_BASE_URL
    race_id = DEFAULT_RACE_ID
    demo_access_token = require_demo_access_token()
    print(f"Running golden demo against {base_url} (race_id={race_id})")
    try:
        run(base_url, race_id, demo_access_token)
    except GoldenDemoFailure as exc:
        eprint(f"\nGolden demo FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — surface any transport/HTTP error plainly, not a traceback dump
        eprint(f"\nGolden demo FAILED with an unexpected error: {exc!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
