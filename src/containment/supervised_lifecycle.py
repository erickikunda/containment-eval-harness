"""Supervised lab lifecycle with pure tools; no real backend or independent watchdog."""

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from containment.admission import manifest_digest, resolve
from containment.backend import FakeBackend
from containment.evidence import Collector, Grant, Limits, ProducerEvent
from containment.lifecycle import SimulationController
from containment.local_model import LocalModelScript, run_model
from containment.models import Deployment, Manifest, Outcome, SafetyPolicy, Scenario, State
from containment.replay import ReplayScript, run_replay
from containment.replay_journal import BudgetExceeded
from containment.store import Store
from containment.supervision import EvidenceSupervisor
from containment.watchdog import ResourceBinding, Watchdog


class SimulationStopAdapter:
    def __init__(self, store: Store, backend: FakeBackend):
        if not isinstance(backend, FakeBackend):
            raise ValueError("Supervised simulation requires a fake backend")
        self.store, self.backend = store, backend

    def binding(self, trial_id: UUID) -> ResourceBinding:
        row = self.store.get(trial_id)
        manifest = Manifest.model_validate_json(row["manifest"])
        if (
            manifest.scenario.deployment != Deployment.FAKE
            or manifest_digest(manifest) != row["manifest_digest"]
        ):
            raise ValueError("Simulation manifest identity mismatch")
        return ResourceBinding(
            trial_id=trial_id,
            resource_id=str(self.backend.root.resolve() / f"{trial_id}.json"),
            manifest_digest=row["manifest_digest"],
        )

    def _verify(self, binding: ResourceBinding) -> None:
        if binding != self.binding(binding.trial_id):
            raise ValueError("Simulation resource binding mismatch")
        manifest = Manifest.model_validate_json(self.store.get(binding.trial_id)["manifest"])
        try:
            self.backend.verify(binding.trial_id, manifest)
        except FileNotFoundError:
            # Marker absence is meaningful only for this simulation backend.
            if self.backend.is_stopped(binding.trial_id) is not True:
                raise RuntimeError("Simulation stop unconfirmed") from None

    def terminate(self, binding: ResourceBinding) -> None:
        self._verify(binding)
        self.backend.terminate(binding.trial_id)

    def is_stopped(self, binding: ResourceBinding) -> bool:
        self._verify(binding)
        return self.backend.is_stopped(binding.trial_id) is True


class SupervisedSimulationController(SimulationController):
    def __init__(
        self, store: Store, backend: FakeBackend, collector: Collector, watchdog: Watchdog
    ):
        super().__init__(store, backend)
        self.collector, self.watchdog = collector, watchdog
        self.adapter = SimulationStopAdapter(store, backend)
        self.supervisor = EvidenceSupervisor(collector, watchdog)

    def run(
        self,
        scenario: Scenario,
        policy: SafetyPolicy,
        replay: ReplayScript | LocalModelScript | None = None,
    ) -> UUID:
        manifest = resolve(scenario, policy)
        if scenario.deployment != Deployment.FAKE:
            raise ValueError("Real execution backends are not implemented")
        if replay is not None:
            kind = LocalModelScript if isinstance(replay, LocalModelScript) else ReplayScript
            replay = kind.model_validate_json(replay.model_dump_json())
        expected = "local" if isinstance(replay, LocalModelScript) else "replay"
        if scenario.inference != expected:
            raise ValueError("Runner and scenario inference mode do not match")
        # Finish old work before creating a new marker. Never replay an interrupted workload.
        self.reconcile()
        trial_id = uuid4()
        self.store.create(trial_id, manifest, evidence_required=True)
        grants = ()
        end_sequences = (2, 2)
        try:
            if replay is not None:
                self.store.replay.register(
                    trial_id,
                    replay.digest(),
                    scenario.budget,
                    replay.total_output_bytes,
                    metadata=(
                        {
                            "runner": "local_ollama",
                            "accounting": "server_reported_tokens",
                            "configuration": replay.model_dump(mode="json"),
                            "server_identity_verified": False,
                            "server_termination_confirmed": False,
                        }
                        if isinstance(replay, LocalModelScript)
                        else None
                    ),
                )
            grants = self.collector.create(
                trial_id,
                manifest_digest(manifest),
                ("observer", "guest"),
                Limits(total_bytes=min(scenario.budget.evidence_bytes, 16_777_216)),
            )
            hard = min(scenario.budget.wall_seconds, 86400)
            token = self.watchdog.issue(
                self.adapter.binding(trial_id), hard_seconds=hard, lease_seconds=min(5, hard)
            )
            self.store.transition(trial_id, State.PREPARING)
            self.backend.prepare(trial_id, manifest)
            self.store.transition(trial_id, State.VERIFYING)
            self.backend.verify(trial_id, manifest)
            if self.supervisor.heartbeat(trial_id, token, sequence=1)["state"] != "active":
                raise RuntimeError("Simulation lease unavailable")
            self.store.transition(trial_id, State.RUNNING)
            self.backend.run(trial_id)
            if replay is None:
                for grant in grants:
                    raw = (
                        ProducerEvent(sequence=1, kind="note", text="Simulation marker only")
                        .model_dump_json()
                        .encode()
                    )
                    self.supervisor.ingest(trial_id, grant.token, raw)
                if self.supervisor.heartbeat(trial_id, token, sequence=2)["state"] != "active":
                    raise RuntimeError("Simulation lease revoked")
            else:
                end_sequences = self._replay(trial_id, replay, grants, token)
            self.store.set_outcome(trial_id, Outcome.SIMULATED)
        except Exception as exc:
            self.store.replay.abort(trial_id, state="failed")
            self.store.set_outcome(trial_id, Outcome.ERROR, f"simulation: {type(exc).__name__}")
            if replay is not None:
                # Preserve an earlier evidence/clock fault if it already revoked the lease.
                try:
                    self.watchdog.status(trial_id)
                except KeyError:
                    pass
                else:
                    reason = (
                        "budget_exhausted" if isinstance(exc, BudgetExceeded) else "health_failure"
                    )
                    self.watchdog.revoke(trial_id, reason=reason)
        self._finish(trial_id, grants, end_sequences)
        return trial_id

    def _replay(self, trial_id, script, grants, token):
        import json

        evidence_sequence, heartbeat_sequence = 1, 2

        def emit(event):
            nonlocal evidence_sequence
            raw = (
                ProducerEvent(
                    sequence=evidence_sequence, kind="note", text=json.dumps(event, sort_keys=True)
                )
                .model_dump_json()
                .encode()
            )
            self.supervisor.ingest(trial_id, grants[0].token, raw)
            evidence_sequence += 1

        def checkpoint():
            nonlocal heartbeat_sequence
            report = self.supervisor.heartbeat(trial_id, token, sequence=heartbeat_sequence)
            heartbeat_sequence += 1
            if report["state"] != "active":
                raise RuntimeError("Replay lease revoked")

        runner = run_model if isinstance(script, LocalModelScript) else run_replay
        result = runner(trial_id, script, self.store.replay, checkpoint, emit)
        raw = ProducerEvent(sequence=1, kind="note", text=result).model_dump_json().encode()
        self.supervisor.ingest(trial_id, grants[1].token, raw)
        checkpoint()
        return evidence_sequence, 2

    def _finish(self, trial_id: UUID, grants: tuple[Grant, ...] = (), end_sequences=(2, 2)) -> None:
        if not self.store.evidence_required(trial_id):
            return super()._finish(trial_id)
        initial = State(self.store.get(trial_id)["state"])
        if initial in {State.COMPLETE, State.QUARANTINED}:
            return
        self.store.replay.abort(trial_id)
        if initial not in {State.STOPPING, State.COLLECTING, State.SEALED, State.CLEANING}:
            self.store.transition(trial_id, State.STOPPING)
        try:
            binding = self.adapter.binding(trial_id)
            try:
                lease = self.watchdog.status(trial_id)
            except KeyError:
                lease = None
            if lease is not None:
                if lease["binding"] != binding.model_dump(mode="json"):
                    raise ValueError("Watchdog binding mismatch")
                reason = (
                    "completed"
                    if self.store.get(trial_id)["outcome"] == Outcome.SIMULATED
                    else "operator_stop"
                )
                self.watchdog.revoke(trial_id, reason=reason)
                self.watchdog.tick()
                lease = self.watchdog.status(trial_id)
                if lease["state"] != "stopped" or not lease["stop_confirmed"]:
                    raise RuntimeError("Watchdog stop unconfirmed or quarantined")
            # Recheck actual marker state even if a prior process recorded confirmed termination.
            self.adapter.terminate(binding)
            if not self.adapter.is_stopped(binding):
                raise RuntimeError("Simulation stop unconfirmed")
            if lease is None and initial != State.CREATED:
                raise RuntimeError("Watchdog record missing after preparation")
            if self.store.get(trial_id)["state"] == State.STOPPING:
                self.store.transition(trial_id, State.COLLECTING)
            if self.store.get(trial_id)["state"] == State.COLLECTING:
                if not self.collector.has_collection(trial_id) and initial == State.CREATED:
                    # The trial journal committed before collector setup; no workload was prepared.
                    self.collector.create(trial_id, binding.manifest_digest, ("observer", "guest"))
                if grants and self.store.get(trial_id)["outcome"] == Outcome.SIMULATED:
                    for grant, sequence in zip(grants, end_sequences, strict=True):
                        raw = (
                            ProducerEvent(sequence=sequence, kind="end").model_dump_json().encode()
                        )
                        self.supervisor.ingest(trial_id, grant.token, raw)
                health = self.collector.health(trial_id)
                if health.manifest_digest != binding.manifest_digest:
                    raise ValueError("Evidence manifest mismatch")
                anchor = self.store.anchor(trial_id)
                if anchor is None:
                    anchor = self.collector.seal(trial_id)
                    self.store.retain(trial_id, anchor)
                self.collector.verify(trial_id, anchor)
                self.store.transition(trial_id, State.SEALED)
            anchor = self.store.anchor(trial_id)
            if anchor is None:
                raise ValueError("Retained seal missing")
            self.collector.verify(trial_id, anchor)
            if not anchor.complete and self.store.get(trial_id)["outcome"] == Outcome.SIMULATED:
                self.store.set_outcome(trial_id, Outcome.ERROR, "Simulation evidence incomplete")
            if self.store.get(trial_id)["state"] == State.SEALED:
                self.store.transition(trial_id, State.CLEANING)
            self.backend.destroy(trial_id)
            if not self.backend.verify_cleanup(trial_id):
                raise RuntimeError("Simulation cleanup unconfirmed")
            self.store.transition(trial_id, State.COMPLETE)
        except Exception as exc:
            self.store.set_outcome(trial_id, Outcome.ERROR, f"finalization: {type(exc).__name__}")
            self.store.transition(trial_id, State.QUARANTINED)


@contextmanager
def supervised_controller(store: Store, backend: FakeBackend, state_dir: Path):
    collector = Collector(state_dir / "evidence.sqlite3")
    try:
        with Watchdog(
            state_dir / "watchdog.sqlite3", SimulationStopAdapter(store, backend)
        ) as watchdog:
            yield SupervisedSimulationController(store, backend, collector, watchdog)
    finally:
        collector.close()
