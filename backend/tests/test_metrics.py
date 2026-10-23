"""Tests for app/metrics.py (AWS hackathon-final pass, phase 5): CloudWatch
Embedded Metric Format (EMF) emission — see that module's own docstring
for why EMF (a structured stdout log line) rather than a direct
PutMetricData call.
"""

from __future__ import annotations

import json

import pytest

from app.metrics import (
    emit_bedrock_invocation,
    emit_event_broker_degraded,
    emit_metric,
    emit_pairing_outcome,
    emit_simulator_lease_ownership_changed,
    emit_stale_leader_write_rejected,
)

pytestmark = pytest.mark.unit


def _captured_emf_line(capsys) -> dict:
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one emitted line, got {lines!r}"
    return json.loads(lines[0])


class TestEmitMetric:
    def test_emits_a_valid_emf_shaped_json_line_to_stdout(self, capsys):
        emit_metric("TestMetric", 42.0, unit="Count", dimensions={"Foo": "bar"})
        payload = _captured_emf_line(capsys)
        assert payload["TestMetric"] == 42.0
        assert payload["Foo"] == "bar"
        emf = payload["_aws"]
        assert emf["CloudWatchMetrics"][0]["Namespace"] == "PitWallCopilot"
        assert emf["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "TestMetric", "Unit": "Count"}]
        assert emf["CloudWatchMetrics"][0]["Dimensions"] == [["Foo"]]

    def test_no_dimensions_still_produces_a_valid_emf_line(self, capsys):
        emit_metric("NoDims", 1.0)
        payload = _captured_emf_line(capsys)
        assert payload["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]

    def test_never_raises_even_on_pathological_input(self):
        # A metrics call is diagnostic, never load-bearing (see module
        # docstring) -- must not raise regardless of input shape.
        emit_metric("Weird", float("nan"))
        emit_metric("StillWeird", 1.0, dimensions={"key": object()})  # type: ignore[dict-item]


class TestDomainSpecificEmitters:
    def test_emit_bedrock_invocation_emits_three_metrics(self, capsys):
        emit_bedrock_invocation(latency_ms=123.4, throttled=True, fallback_used=False, source="bedrock")
        captured = capsys.readouterr()
        lines = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        assert len(lines) == 3
        names = {line["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] for line in lines}
        assert names == {"BedrockInvocationLatency", "BedrockThrottled", "BedrockFallbackUsed"}
        by_name = {line["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"]: line for line in lines}
        assert by_name["BedrockInvocationLatency"]["BedrockInvocationLatency"] == 123.4
        assert by_name["BedrockThrottled"]["BedrockThrottled"] == 1.0
        assert by_name["BedrockFallbackUsed"]["BedrockFallbackUsed"] == 0.0
        assert by_name["BedrockInvocationLatency"]["Source"] == "bedrock"

    def test_emit_simulator_lease_ownership_changed(self, capsys):
        emit_simulator_lease_ownership_changed(race_id="demo-race-001")
        payload = _captured_emf_line(capsys)
        assert payload["SimulatorLeaseOwnershipChanged"] == 1.0
        assert payload["RaceId"] == "demo-race-001"

    def test_emit_stale_leader_write_rejected(self, capsys):
        emit_stale_leader_write_rejected(race_id="demo-race-001")
        payload = _captured_emf_line(capsys)
        assert payload["StaleLeaderWriteRejected"] == 1.0

    def test_emit_pairing_outcome(self, capsys):
        emit_pairing_outcome(outcome="success")
        payload = _captured_emf_line(capsys)
        assert payload["PairingOutcome"] == 1.0
        assert payload["Outcome"] == "success"

    def test_emit_event_broker_degraded(self, capsys):
        emit_event_broker_degraded(reason="publish_failed")
        payload = _captured_emf_line(capsys)
        assert payload["EventBrokerDegraded"] == 1.0
        assert payload["Reason"] == "publish_failed"
