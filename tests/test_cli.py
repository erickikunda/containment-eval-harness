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


def test_evidence_fixture_cli(tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path), "evidence-fixture", "positive"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["seal"]["complete"] is True
    assert report["verdict"]["target_outcome"] == "fixture_crossing_confirmed"
    assert report["verdict"]["simulation_only"] is True


def test_deployment_plan_and_preflight_create_no_state(tmp_path, capsys):
    examples = Path(__file__).parents[1] / "deployment" / "examples"
    state = tmp_path / "state"
    args = [str(examples / "eks-fargate-app.json"), str(examples / "assets.json")]
    assert main(["--state-dir", str(state), "deployment-plan", *args]) == 0
    assert json.loads(capsys.readouterr().out)["execution_authorized"] is False
    assert (
        main(
            [
                "--state-dir",
                str(state),
                "preflight",
                *args,
                "--asset-root",
                str(examples / "assets"),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["readiness"] == "blocked"
    assert report["checks"][0]["status"] == "pass"
    assert not state.exists()
