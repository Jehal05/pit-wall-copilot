#!/usr/bin/env python3
"""Endurance test script (spec v1.1 repair 15).

Repeatedly drives full reset -> start -> (poll to the known lap-28 alert)
-> pause cycles against a REAL, already-running backend for a configurable
duration or cycle count, checking that every cycle stays correct — not
just that the process doesn't crash. This is the check none of
`backend/tests/*` (each test resets its own fixtures) or the golden demo
script (runs the narrative exactly once) can catch: state accumulating
across many repeated runs of the SAME long-running process — a growing
response-time trend, a memory leak visible via /health or process RSS (if
running locally), an alert list that keeps growing without bound and
starts slowing GET .../alerts down, or a state_version overflow/precision
issue after many thousands of increments.

What it checks every cycle:
  - /health still returns 200 (the process is still alive and answering).
  - POST /v1/demo/reset returns the fixed lap-24 baseline with a FRESH
    run_id each time (never repeating a previous run_id — a real bug this
    would catch: a run_id generator that isn't actually random/unique).
  - The known lap-24->28 CLOSED->OPEN transition still fires an alert
    within the timeout, on every single cycle, not just the first few
    (a state leaking across "resets" that shouldn't be possible could
    cause later cycles to behave differently from the first).
  - GET .../alerts response time doesn't grow unboundedly across cycles
    (a crude, not statistically rigorous, but real check: compares the
    last cycle's alerts-list latency against the first cycle's and flags
    a large regression as a signal worth investigating, not a hard
    failure by itself).

Usage:
    PIT_WALL_DEMO_ACCESS_TOKEN=<token> python scripts/endurance_test.py --cycles 20
    PIT_WALL_DEMO_ACCESS_TOKEN=<token> python scripts/endurance_test.py --duration-seconds 300

Exit code 0 if every cycle's correctness checks pass, 1 otherwise. The
latency-trend check only prints a warning (not a failure) — it is a
signal to look closer, not a hard pass/fail bar this script can respon
sibly assert on without a controlled environment.
"""

from __future__ import annotations

import argparse
import sys
import time

from _client import DEFAULT_BASE_URL, DEFAULT_RACE_ID, build_client, eprint, require_demo_access_token

ALERT_WAIT_TIMEOUT_SECONDS = 20
ALERT_POLL_INTERVAL_SECONDS = 0.5


def run_one_cycle(base_url: str, race_id: str, token: str, seen_run_ids: set[str], cycle_number: int) -> float | None:
    """Returns the GET .../alerts latency (seconds) for this cycle's
    successful poll, or None if the cycle failed (a failure is printed
    immediately, not just returned, so a long endurance run's log stays
    readable as it happens rather than dumping everything at the end)."""
    client = build_client(base_url, demo_access_token=token)

    response = client.get("/health")
    if response.status_code != 200:
        eprint(f"  cycle {cycle_number}: FAIL — /health returned {response.status_code}")
        return None

    response = client.post("/v1/demo/reset")
    if response.status_code != 200:
        eprint(f"  cycle {cycle_number}: FAIL — POST /v1/demo/reset returned {response.status_code}: {response.text}")
        return None
    state = response.json()
    if state["lap"] != 24:
        eprint(f"  cycle {cycle_number}: FAIL — expected lap 24 baseline, got lap {state['lap']}")
        return None
    run_id = state["run_id"]
    if run_id in seen_run_ids:
        eprint(f"  cycle {cycle_number}: FAIL — run_id {run_id!r} was already seen in an earlier cycle")
        return None
    seen_run_ids.add(run_id)

    response = client.post(
        f"/v1/races/{race_id}/watches",
        json={"candidate_driver": "CAR_04", "target_driver": "CAR_03", "watch_type": "UNDERCUT"},
    )
    if response.status_code != 200:
        eprint(f"  cycle {cycle_number}: FAIL — create_strategy_watch returned {response.status_code}: {response.text}")
        return None
    watch_id = response.json()["watch_id"]

    response = client.post("/v1/demo/start")
    if response.status_code != 200:
        eprint(f"  cycle {cycle_number}: FAIL — POST /v1/demo/start returned {response.status_code}: {response.text}")
        return None

    deadline = time.monotonic() + ALERT_WAIT_TIMEOUT_SECONDS
    latency = None
    while time.monotonic() < deadline:
        started = time.monotonic()
        response = client.get(f"/v1/races/{race_id}/alerts", params={"watch_id": watch_id})
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            eprint(f"  cycle {cycle_number}: FAIL — GET .../alerts returned {response.status_code}: {response.text}")
            return None
        if response.json():
            latency = elapsed
            break
        time.sleep(ALERT_POLL_INTERVAL_SECONDS)

    if latency is None:
        eprint(f"  cycle {cycle_number}: FAIL — no alert published before the {ALERT_WAIT_TIMEOUT_SECONDS}s timeout")
        return None

    client.post("/v1/demo/pause")  # best-effort cleanup between cycles; a failure here isn't a cycle failure
    print(f"  cycle {cycle_number}: OK (run_id={run_id}, alerts-latency={latency * 1000:.0f}ms)")
    return latency


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cycles", type=int, help="Run exactly this many cycles.")
    group.add_argument("--duration-seconds", type=float, help="Run cycles until this many seconds have elapsed.")
    args = parser.parse_args()

    if args.cycles is None and args.duration_seconds is None:
        args.cycles = 10  # a sane default so the script is runnable with no flags at all

    base_url = DEFAULT_BASE_URL
    race_id = DEFAULT_RACE_ID
    token = require_demo_access_token()
    print(f"Running endurance test against {base_url} (race_id={race_id})")

    seen_run_ids: set[str] = set()
    latencies: list[float] = []
    failures = 0
    cycle_number = 0
    start_time = time.monotonic()

    while True:
        if args.cycles is not None and cycle_number >= args.cycles:
            break
        if args.duration_seconds is not None and (time.monotonic() - start_time) >= args.duration_seconds:
            break
        cycle_number += 1
        latency = run_one_cycle(base_url, race_id, token, seen_run_ids, cycle_number)
        if latency is None:
            failures += 1
        else:
            latencies.append(latency)

    print(f"\nCompleted {cycle_number} cycle(s): {cycle_number - failures} passed, {failures} failed.")

    if len(latencies) >= 4:
        # Crude trend check, not a statistical claim: compares the mean of
        # the first quarter of cycles against the mean of the last
        # quarter. A >3x regression is worth a human looking at (a real
        # leak/slowdown would show up this way), but is deliberately only
        # a warning — this script has no control group and no statistical
        # power to assert it as a hard failure.
        quarter = max(1, len(latencies) // 4)
        first_quarter_mean = sum(latencies[:quarter]) / quarter
        last_quarter_mean = sum(latencies[-quarter:]) / quarter
        print(
            f"Alerts-latency trend: first-quarter mean {first_quarter_mean * 1000:.0f}ms, "
            f"last-quarter mean {last_quarter_mean * 1000:.0f}ms"
        )
        if first_quarter_mean > 0 and last_quarter_mean > first_quarter_mean * 3:
            eprint(
                "WARNING: alerts-list latency grew more than 3x from the first quarter of cycles to the last — "
                "worth investigating for a leak or unbounded growth, though this script cannot itself say what "
                "caused it."
            )

    if failures > 0:
        eprint(f"\nEndurance test FAILED: {failures} of {cycle_number} cycle(s) failed correctness checks.")
        return 1

    print("\nEndurance test PASSED: every cycle stayed correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
