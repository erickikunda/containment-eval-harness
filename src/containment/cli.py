import argparse
import json
import sqlite3
import sys
from pathlib import Path

from containment.admission import (
    development_policy,
    local_model_policy,
    manifest_digest,
    replay_policy,
    resolve,
)
from containment.aws_inspection import AwsSnapshot, InspectionTarget, assess, inspection_policy
from containment.backend import FakeBackend
from containment.deployment import (
    AssetManifest,
    DeploymentConfig,
    check_assets_for_profile,
    deployment_plan,
)
from containment.fixtures import run_fixture
from containment.lifecycle import controller_lock
from containment.local_model import LocalModelScript
from containment.models import Outcome, Scenario, State
from containment.preflight import preflight
from containment.replay import ReplayScript
from containment.store import Store
from containment.supervised_lifecycle import supervised_controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Containment harness: local simulation and deployment preparation"
    )
    parser.add_argument("--state-dir", type=Path, default=Path(".harness"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "simulate", "replay", "local-model"):
        command = commands.add_parser(name)
        command.add_argument("scenario", type=Path)
        if name in {"replay", "local-model"}:
            command.add_argument("script", type=Path)
        if name == "validate":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--replay", action="store_true")
            mode.add_argument("--local-model", action="store_true")
    commands.add_parser("list")
    commands.add_parser("reconcile")
    fixture = commands.add_parser("evidence-fixture")
    fixture.add_argument("mode", choices=["positive", "negative", "leaked", "claim_only", "gap"])
    for name in ("deployment-plan", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("config", type=Path)
        command.add_argument("assets", type=Path)
        if name == "preflight":
            command.add_argument("--asset-root", type=Path, required=True)
    inspection = commands.add_parser("aws-inspect")
    inspection.add_argument("config", type=Path)
    inspection.add_argument("assets", type=Path)
    inspection.add_argument("target", type=Path)
    mode = inspection.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", type=Path)
    mode.add_argument("--live", action="store_true")
    inspection.add_argument("--aws-profile")
    inspection.add_argument("--save-snapshot", type=Path)
    policy_command = commands.add_parser("aws-inspection-policy")
    policy_command.add_argument("config", type=Path)
    policy_command.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "aws-inspection-policy":
            config = DeploymentConfig.model_validate_json(args.config.read_text())
            target = InspectionTarget.model_validate_json(args.target.read_text())
            print(json.dumps(inspection_policy(config, target), indent=2))
            return 0
        if args.command == "aws-inspect":
            config = DeploymentConfig.model_validate_json(args.config.read_text())
            assets = AssetManifest.model_validate_json(args.assets.read_text())
            target = InspectionTarget.model_validate_json(args.target.read_text())
            check_assets_for_profile(config, assets)
            if args.snapshot:
                if args.aws_profile or args.save_snapshot:
                    raise ValueError("--aws-profile and --save-snapshot are only valid with --live")
                with args.snapshot.open("rb") as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Snapshot exceeds 8 MiB")
                snapshot = AwsSnapshot.model_validate_json(raw)
            else:
                try:
                    from containment.aws_reader import live_snapshot
                except ImportError as exc:
                    raise RuntimeError(
                        "Live inspection requires installation with the 'aws' extra"
                    ) from exc
                snapshot = live_snapshot(config, assets, target, args.aws_profile)
                if args.save_snapshot:
                    # Do not overwrite an earlier observation or follow an existing symlink.
                    with args.save_snapshot.open("x") as stream:
                        stream.write(snapshot.model_dump_json(indent=2) + "\n")
            report = assess(
                config, assets, target, snapshot, source="snapshot" if args.snapshot else "live_aws"
            )
            print(json.dumps(report, indent=2))
            return 1  # Metadata alone never authorizes experiment execution.
        if args.command in {"deployment-plan", "preflight"}:
            config = DeploymentConfig.model_validate_json(args.config.read_text())
            assets = AssetManifest.model_validate_json(args.assets.read_text())
            if args.command == "deployment-plan":
                print(json.dumps(deployment_plan(config, assets), indent=2))
                return 0
            print(json.dumps(preflight(config, assets, args.asset_root), indent=2))
            return 1  # Local checks cannot establish live AWS containment.
        scenario = None
        replay = None
        policy = (
            local_model_policy()
            if args.command == "local-model" or getattr(args, "local_model", False)
            else replay_policy()
            if args.command == "replay" or getattr(args, "replay", False)
            else development_policy()
        )
        if args.command in {"validate", "simulate", "replay", "local-model"}:
            scenario = Scenario.model_validate_json(args.scenario.read_text())
            manifest = resolve(scenario, policy)
            if args.command in {"replay", "local-model"}:
                with args.script.open("rb") as stream:
                    raw = stream.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ValueError("Replay script exceeds 1 MiB")
                script_type = LocalModelScript if args.command == "local-model" else ReplayScript
                replay = script_type.model_validate_json(raw)
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
                if args.command == "list":
                    from uuid import UUID

                    ids = [UUID(row["id"]) for row in store.records()]
                else:
                    with supervised_controller(
                        store, FakeBackend(args.state_dir / "resources"), args.state_dir
                    ) as controller:
                        if args.command in {"simulate", "replay", "local-model"}:
                            assert scenario is not None
                            ids = [controller.run(scenario, policy, replay)]
                        else:
                            ids = controller.reconcile()
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
