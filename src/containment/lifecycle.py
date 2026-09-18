"""Single-controller local simulation lifecycle with restart recovery."""

import fcntl
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from containment.admission import resolve
from containment.backend import Backend
from containment.models import Deployment, Manifest, Outcome, SafetyPolicy, Scenario, State
from containment.store import Store


@contextmanager
def controller_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another controller is using this state directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class SimulationController:
    def __init__(self, store: Store, backend: Backend):
        self.store = store
        self.backend = backend

    def run(self, scenario: Scenario, policy: SafetyPolicy) -> UUID:
        manifest = resolve(scenario, policy)
        if scenario.deployment != Deployment.FAKE:
            raise ValueError("Real execution backends are not implemented")
        trial_id = uuid4()
        self.store.create(trial_id, manifest)
        try:
            self.store.transition(trial_id, State.PREPARING)
            self.backend.prepare(trial_id, manifest)
            self.store.transition(trial_id, State.VERIFYING)
            self.backend.verify(trial_id, manifest)
            self.store.transition(trial_id, State.RUNNING)
            self.backend.run(trial_id)
            self.store.set_outcome(trial_id, Outcome.SIMULATED)
        except Exception as exc:
            self.store.set_outcome(trial_id, Outcome.ERROR, str(exc))
        # KeyboardInterrupt/process death leave a recoverable persisted active state.
        self._finish(trial_id)
        return trial_id

    def _finish(self, trial_id: UUID) -> None:
        if self.store.evidence_required(trial_id):
            raise RuntimeError("Evidence-enabled trial requires the supervised controller")
        state = State(self.store.get(trial_id)["state"])
        if state in {State.COMPLETE, State.QUARANTINED}:
            return
        if state not in {State.STOPPING, State.CLEANING}:
            self.store.transition(trial_id, State.STOPPING)
        try:
            # Recheck stop even if a previous controller had reached CLEANING.
            self.backend.terminate(trial_id)
            if not self.backend.is_stopped(trial_id):
                raise RuntimeError("Resource stop could not be confirmed")
            if State(self.store.get(trial_id)["state"]) == State.STOPPING:
                self.store.transition(trial_id, State.CLEANING)
            self.backend.destroy(trial_id)
            if not self.backend.verify_cleanup(trial_id):
                raise RuntimeError("Resource cleanup could not be confirmed")
            self.store.transition(trial_id, State.COMPLETE)
        except Exception as exc:
            previous = self.store.get(trial_id)["error"]
            detail = f"{previous}; cleanup: {exc}" if previous else str(exc)
            self.store.set_outcome(trial_id, Outcome.ERROR, detail)
            self.store.transition(trial_id, State.QUARANTINED)

    def reconcile(self) -> list[UUID]:
        recovered = []
        for record in self.store.records():
            if State(record["state"]) in {State.COMPLETE, State.QUARANTINED}:
                continue
            manifest = Manifest.model_validate_json(record["manifest"])
            if manifest.scenario.deployment != Deployment.FAKE:
                raise ValueError("Simulation recovery cannot manage a real deployment")
            trial_id = UUID(record["id"])
            if record["outcome"] is None:
                self.store.set_outcome(trial_id, Outcome.INTERRUPTED)
            self._finish(trial_id)
            recovered.append(trial_id)
        return recovered
