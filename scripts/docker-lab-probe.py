"""Test-only container probe. Never packaged as a harness CLI command."""

import errno
import json
import os
import signal
import socket
import sys
from pathlib import Path
from uuid import UUID

from containment.admission import development_policy
from containment.backend import FakeBackend
from containment.evidence import Collector
from containment.lifecycle import controller_lock
from containment.models import Outcome, Scenario, State
from containment.store import Store
from containment.supervised_lifecycle import supervised_controller

ROOT = Path("/var/lib/containment")


def check_runtime():
    assert os.getuid() == 10001
    # Some Linux kernels expose inactive tunnel devices even in a network=none namespace.
    active = {
        name
        for _, name in socket.if_nameindex()
        if int(Path(f"/sys/class/net/{name}/flags").read_text().strip(), 16) & 1
    }
    assert active == {"lo"}
    status = Path("/proc/self/status").read_text().splitlines()
    assert next(row.split()[1] for row in status if row.startswith("CapEff:")) == "0000000000000000"
    assert next(row.split()[1] for row in status if row.startswith("NoNewPrivs:")) == "1"
    try:
        Path("/opt/containment/write-probe").touch()
    except OSError as exc:
        assert exc.errno == errno.EROFS
    else:
        raise AssertionError("Root filesystem is writable")


class CrashBackend(FakeBackend):
    def run(self, trial_id):
        super().run(trial_id)
        print(json.dumps({"trial_id": str(trial_id), "simulation_only": True}), flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("SIGKILL did not terminate the probe")


def crash():
    with controller_lock(ROOT / "controller.lock"):
        store = Store(ROOT / "trials.sqlite3")
        try:
            with supervised_controller(store, CrashBackend(ROOT / "resources"), ROOT) as controller:
                scenario = Scenario.model_validate_json(
                    Path("/examples/simulation.json").read_text()
                )
                controller.run(scenario, development_policy())
        finally:
            store.close()


def verify():
    with controller_lock(ROOT / "controller.lock"):
        store = Store(ROOT / "trials.sqlite3")
        collector = Collector(ROOT / "evidence.sqlite3")
        try:
            rows = store.records()
            assert len(rows) == 2
            assert {row["outcome"] for row in rows} == {Outcome.SIMULATED, Outcome.INTERRUPTED}
            for row in rows:
                trial = UUID(row["id"])
                assert row["state"] == State.COMPLETE
                anchor = store.anchor(trial)
                collector.verify(trial, anchor)
                assert anchor.complete == (row["outcome"] == Outcome.SIMULATED)
                if row["outcome"] == Outcome.INTERRUPTED:
                    assert anchor.event_count == 0  # Recovery did not replay synthetic events.
                assert FakeBackend(ROOT / "resources").verify_cleanup(trial)
            print(
                json.dumps(
                    {"verified_trials": len(rows), "simulation_only": True, "uid": os.getuid()}
                )
            )
        finally:
            collector.close()
            store.close()


if __name__ == "__main__":
    check_runtime()
    if sys.argv[1:] == ["crash"]:
        crash()
    elif sys.argv[1:] == ["verify"]:
        verify()
    else:
        raise SystemExit("Expected crash or verify")
