import argparse
import json
import sqlite3
import sys
from pathlib import Path

from containment.admission import development_policy, manifest_digest, resolve
from containment.backend import FakeBackend
from containment.fixtures import run_fixture
from containment.lifecycle import SimulationController, controller_lock
from containment.models import Outcome, Scenario, State
from containment.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Containment harness: local simulation only")
    parser.add_argument("--state-dir", type=Path, default=Path(".harness"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "simulate"):
        command = commands.add_parser(name)
        command.add_argument("scenario", type=Path)
    commands.add_parser("list")
    commands.add_parser("reconcile")
    fixture = commands.add_parser("evidence-fixture")
    fixture.add_argument("mode", choices=["positive", "negative", "leaked", "claim_only", "gap"])
    args = parser.parse_args(argv)
    try:
        scenario = None
        if args.command in {"validate", "simulate"}:
            scenario = Scenario.model_validate_json(args.scenario.read_text())
            manifest = resolve(scenario, development_policy())
            if args.command == "validate":
                print(json.dumps({"admission": "static_only", "digest": manifest_digest(manifest)}))
                return 0
        with controller_lock(args.state_dir / "controller.lock"):
            if args.command == "evidence-fixture":
                report = run_fixture(args.state_dir / "evidence.sqlite3", args.mode)
                print(json.dumps(report, indent=2))
                return 0
            store = Store(args.state_dir / "trials.sqlite3")
            try:
                controller = SimulationController(store, FakeBackend(args.state_dir / "resources"))
                if args.command == "simulate":
                    assert scenario is not None
                    ids = [controller.run(scenario, development_policy())]
                elif args.command == "reconcile":
                    ids = controller.reconcile()
                else:
                    from uuid import UUID

                    ids = [UUID(row["id"]) for row in store.records()]
                summaries = [store.summary(trial_id) for trial_id in ids]
                print(json.dumps(summaries, indent=2))
                return (
                    1
                    if any(
                        row["state"] == State.QUARANTINED or row["outcome"] == Outcome.ERROR
                        for row in summaries
                    )
                    else 0
                )
            finally:
                store.close()
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
