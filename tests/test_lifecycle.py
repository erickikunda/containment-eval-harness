from uuid import UUID, uuid4

import pytest

from containment.admission import development_policy, resolve
from containment.backend import FakeBackend
from containment.lifecycle import SimulationController, controller_lock
from containment.models import Outcome, State
from containment.store import Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "state.sqlite3")
    yield result
    result.close()


@pytest.fixture
def backend(tmp_path):
    return FakeBackend(tmp_path / "resources")


def test_complete_simulation_cleans_resources(store, backend, scenario):
    trial_id = SimulationController(store, backend).run(scenario, development_policy())
    assert store.get(trial_id)["state"] == State.COMPLETE
    assert store.get(trial_id)["outcome"] == Outcome.SIMULATED
    assert backend.verify_cleanup(trial_id)
    assert store.summary(trial_id)["simulation_only"] is True


def test_illegal_state_transition_is_not_journaled(store, scenario):
    trial_id = uuid4()
    store.create(trial_id, resolve(scenario, development_policy()))
    with pytest.raises(ValueError, match="Invalid transition"):
        store.transition(trial_id, State.COMPLETE)
    assert store.get(trial_id)["state"] == State.CREATED
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_resource_lifecycle_is_idempotent(backend, scenario):
    trial_id = uuid4()
    manifest = resolve(scenario, development_policy())
    backend.prepare(trial_id, manifest)
    backend.prepare(trial_id, manifest)
    backend.run(trial_id)
    with pytest.raises(RuntimeError, match="running"):
        backend.destroy(trial_id)
    for _ in range(2):
        backend.terminate(trial_id)
        backend.destroy(trial_id)
    assert backend.verify_cleanup(trial_id)


def test_execution_failure_still_cleans_up(store, backend, scenario, monkeypatch):
    def fail(trial_id):
        raise RuntimeError("execution failed")

    monkeypatch.setattr(backend, "run", fail)
    trial_id = SimulationController(store, backend).run(scenario, development_policy())
    assert store.get(trial_id)["state"] == State.COMPLETE
    assert store.get(trial_id)["outcome"] == Outcome.ERROR
    assert backend.verify_cleanup(trial_id)


def test_uncertain_stop_quarantines_without_destroy(store, backend, scenario, monkeypatch):
    monkeypatch.setattr(backend, "is_stopped", lambda trial_id: False)
    trial_id = SimulationController(store, backend).run(scenario, development_policy())
    assert store.get(trial_id)["state"] == State.QUARANTINED
    assert not backend.verify_cleanup(trial_id)
    assert "stop" in store.get(trial_id)["error"]
    assert SimulationController(store, backend).reconcile() == []


def test_uncertain_cleanup_quarantines(store, backend, scenario, monkeypatch):
    monkeypatch.setattr(backend, "verify_cleanup", lambda trial_id: False)
    trial_id = SimulationController(store, backend).run(scenario, development_policy())
    assert store.get(trial_id)["state"] == State.QUARANTINED
    assert "cleanup" in store.get(trial_id)["error"]


@pytest.mark.parametrize(
    "last_state",
    [
        State.CREATED,
        State.PREPARING,
        State.VERIFYING,
        State.RUNNING,
        State.STOPPING,
        State.CLEANING,
    ],
)
def test_recover_each_interrupted_state(store, backend, scenario, last_state):
    trial_id = uuid4()
    manifest = resolve(scenario, development_policy())
    store.create(trial_id, manifest)
    states = [State.PREPARING, State.VERIFYING, State.RUNNING, State.STOPPING, State.CLEANING]
    if last_state != State.CREATED:
        backend.prepare(trial_id, manifest)
        for state in states:
            store.transition(trial_id, state)
            if state == State.RUNNING:
                backend.run(trial_id)
            if state == last_state:
                break
    controller = SimulationController(store, backend)
    assert controller.reconcile() == [trial_id]
    assert store.get(trial_id)["outcome"] == Outcome.INTERRUPTED
    assert store.get(trial_id)["state"] == State.COMPLETE
    assert backend.verify_cleanup(trial_id)
    assert controller.reconcile() == []


def test_controller_restart_does_not_replay_workload(tmp_path, scenario):
    class InterruptingBackend(FakeBackend):
        def run(self, trial_id):
            super().run(trial_id)
            raise KeyboardInterrupt

    path = tmp_path / "trials.sqlite3"
    resources = tmp_path / "resources"
    store = Store(path)
    try:
        with pytest.raises(KeyboardInterrupt):
            SimulationController(store, InterruptingBackend(resources)).run(
                scenario, development_policy()
            )
        trial_id = UUID(store.records()[0]["id"])
    finally:
        store.close()

    class RecoveryBackend(FakeBackend):
        def run(self, trial_id):
            pytest.fail("Recovery must never rerun a workload")

    reopened = Store(path)
    try:
        backend = RecoveryBackend(resources)
        SimulationController(reopened, backend).reconcile()
        assert reopened.get(trial_id)["state"] == State.COMPLETE
        assert reopened.get(trial_id)["outcome"] == Outcome.INTERRUPTED
        assert backend.verify_cleanup(trial_id)
    finally:
        reopened.close()


def test_corrupted_resource_quarantines(store, backend, scenario):
    trial_id = uuid4()
    manifest = resolve(scenario, development_policy())
    store.create(trial_id, manifest)
    store.transition(trial_id, State.PREPARING)
    backend.prepare(trial_id, manifest)
    (backend.root / f"{trial_id}.json").write_text("not json")
    SimulationController(store, backend).reconcile()
    assert store.get(trial_id)["state"] == State.QUARANTINED
    assert not backend.verify_cleanup(trial_id)


def test_local_controller_lock_rejects_second_writer(tmp_path):
    path = tmp_path / "controller.lock"
    with controller_lock(path):
        with pytest.raises(RuntimeError, match="Another controller"):
            with controller_lock(path):
                pytest.fail("Second writer acquired lock")
