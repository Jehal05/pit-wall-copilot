from __future__ import annotations

import json

import pytest

from app.config import DEFAULT_SCENARIO_PATH
from app.domain.scenario import ScenarioValidationError, load_scenario


def test_scenario_loads_and_matches_spec_narrative():
    scenario = load_scenario(DEFAULT_SCENARIO_PATH)
    assert scenario.scenario_id == "scenario-demo-01"
    assert scenario.race_id == "demo-race-001"
    assert scenario.model_seed == 48127
    assert scenario.start_lap == 24
    assert scenario.end_lap == 28
    assert set(scenario.cars.keys()) == {"CAR_03", "CAR_04", "CAR_07", "CAR_11"}

    gaps = {lap.lap: lap.gap_car04_to_car03_ms for lap in scenario.laps}
    assert gaps == {24: 3400, 25: 2900, 26: 2400, 27: 2000, 28: 1800}


def test_scenario_validation_rejects_malformed_json(tmp_path):
    bad_file = tmp_path / "broken.json"
    bad_file.write_text("{ this is not valid json", encoding="utf-8")

    with pytest.raises(ScenarioValidationError):
        load_scenario(bad_file)


def test_scenario_validation_rejects_missing_driver_data(tmp_path):
    valid = json.loads(DEFAULT_SCENARIO_PATH.read_text(encoding="utf-8"))
    # Remove one car's data from a single lap to simulate malformed input.
    del valid["laps"][0]["drivers"]["CAR_11"]
    bad_file = tmp_path / "missing-driver.json"
    bad_file.write_text(json.dumps(valid), encoding="utf-8")

    with pytest.raises(ScenarioValidationError):
        load_scenario(bad_file)


def test_scenario_validation_rejects_out_of_order_laps(tmp_path):
    valid = json.loads(DEFAULT_SCENARIO_PATH.read_text(encoding="utf-8"))
    valid["laps"][0], valid["laps"][1] = valid["laps"][1], valid["laps"][0]
    bad_file = tmp_path / "out-of-order.json"
    bad_file.write_text(json.dumps(valid), encoding="utf-8")

    with pytest.raises(ScenarioValidationError):
        load_scenario(bad_file)


def test_scenario_validation_rejects_missing_file(tmp_path):
    with pytest.raises(ScenarioValidationError):
        load_scenario(tmp_path / "does-not-exist.json")
