import json
from pathlib import Path

from containment.cli import main

EXAMPLE = Path(__file__).parents[1] / "examples" / "simulation.json"


def test_validate_creates_no_runtime_state(tmp_path, capsys):
    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "validate", str(EXAMPLE)]) == 0
    assert json.loads(capsys.readouterr().out)["admission"] == "static_only"
    assert not state.exists()


def test_invalid_input_does_not_create_state(tmp_path, scenario_data, capsys):
    scenario_data["deployment"] = "eks_ec2_vm"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(scenario_data))
    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "simulate", str(scenario)]) == 2
    assert "not allowed" in capsys.readouterr().err
    assert not state.exists()


def test_simulate_list_reconcile(tmp_path, capsys):
    base = ["--state-dir", str(tmp_path / "state")]
    assert main([*base, "simulate", str(EXAMPLE)]) == 0
    record = json.loads(capsys.readouterr().out)[0]
    assert record["state"] == "complete"
    assert record["outcome"] == "simulation_only"
    assert main([*base, "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == record["id"]
    assert main([*base, "reconcile"]) == 0
    assert json.loads(capsys.readouterr().out) == []
