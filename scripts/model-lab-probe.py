"""Test-only wire rehearsal; the fixture is explicitly not an inference runtime."""

import json
import os
import signal
import sys
import time
from pathlib import Path
from uuid import UUID

from model_stub import model_server
from probe import check_runtime

from containment.cli import main
from containment.evidence import Collector
from containment.store import Store

ROOT = Path("/var/lib/containment")


def run(mode):
    def transform(path, reply):
        if path == "/completion":
            if mode == "timeout":
                time.sleep(1)
            if mode == "crash":
                print(json.dumps({"fixture": True, "crash_after_request": True}), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        return reply

    with model_server(transform) as (port, calls):
        config = json.loads(Path("/examples/local-model-script.json").read_text())
        config.update(
            port=port,
            model_identity="wire-fixture-no-model",
            runtime_identity="test-only-http-server",
        )
        if mode == "timeout":
            config["request_timeout_seconds"] = 0.2
        config_path = ROOT / "model-config.json"
        config_path.write_text(json.dumps(config))
        result = main(
            [
                "--state-dir",
                str(ROOT),
                "local-model",
                "/examples/local-model-scenario.json",
                str(config_path),
            ]
        )
        assert result == (1 if mode == "timeout" else 0)
        assert len(calls) == (2 if mode == "timeout" else 6)


def verify():
    store = Store(ROOT / "trials.sqlite3")
    collector = Collector(ROOT / "evidence.sqlite3")
    try:
        rows = store.records()
        assert len(rows) == 3
        assert {row["outcome"] for row in rows} == {"simulation_only", "error", "interrupted"}
        for row in rows:
            trial = UUID(row["id"])
            report = store.summary(trial)
            assert report["state"] == "complete"
            run = report["replay"]
            assert run["accounting"] == "server_reported_tokens"
            assert run["metadata"]["configuration"]["model_identity"] == "wire-fixture-no-model"
            assert run["metadata"]["server_termination_confirmed"] is False
            anchor = store.anchor(trial)
            collector.verify(trial, anchor)
            if row["outcome"] == "simulation_only":
                assert anchor.complete and run["state"] == "complete"
                assert run["model_calls"] == 3 and run["tool_calls"] == 2
            else:
                assert not anchor.complete
                assert run["model_calls"] == 1 and run["tool_calls"] == 0
                assert run["tokens"] == 2304
                assert run["actions"][0]["state"] == "uncertain"
                assert run["actions"][0]["result"] is None
        assert not list((ROOT / "resources").glob("*.json"))
        print(
            json.dumps(
                {
                    "verified_trials": 3,
                    "real_inference": False,
                    "transport_tool_loop_and_recovery": "passed",
                }
            )
        )
    finally:
        collector.close()
        store.close()


if __name__ == "__main__":
    check_runtime()
    mode = sys.argv[1]
    if mode == "verify":
        verify()
    elif mode in {"success", "timeout", "crash"}:
        run(mode)
    else:
        raise SystemExit("Expected success, timeout, crash, or verify")
