from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from containment.admission import development_policy, resolve
from containment.backend import FakeBackend
from containment.lifecycle import SimulationController
from containment.models import Outcome, State
from containment.store import Store
from containment.supervised_lifecycle import supervised_controller


@contextmanager
def system(path, backend=None):
    store = Store(path / "trials.sqlite3")
    try:
        with supervised_controller(
            store, backend or FakeBackend(path / "resources"), path
        ) as controller:
            yield controller
    finally:
        store.close()


def sole_trial(controller):
    return UUID(controller.store.records()[0]["id"])


def test_complete_flow_retains_verified_evidence_before_cleanup(tmp_path, scenario):
    with system(tmp_path) as c:
        trial = c.run(scenario, development_policy())
        states = [
            row[0] for row in c.store.db.execute("SELECT state FROM events ORDER BY sequence")
        ]
        assert states == [state.value for state in State if state != State.QUARANTINED]
        anchor = c.store.anchor(trial)
        assert anchor.complete and anchor.event_count == 4
        assert c.collector.verify(trial, anchor).seal == anchor
        assert c.store.summary(trial)["evidence_seal"]["seal_digest"] == anchor.seal_digest
        assert c.watchdog.status(trial)["stop_confirmed"]
        assert c.backend.verify_cleanup(trial)


@pytest.mark.parametrize(
    "state",
    [
        State.PREPARING,
        State.VERIFYING,
        State.RUNNING,
        State.STOPPING,
        State.COLLECTING,
        State.SEALED,
        State.CLEANING,
    ],
)
def test_recovery_after_each_persisted_transition_never_replays(
    tmp_path, scenario, monkeypatch, state
):
    with system(tmp_path) as c:
        transition = c.store.transition

        def crash(trial, target):
            transition(trial, target)
            if target == state:
                raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(c.store, "transition", crash)
            with pytest.raises(KeyboardInterrupt):
                c.run(scenario, development_policy())
    with system(tmp_path) as c:
        monkeypatch.setattr(c.backend, "run", lambda _: pytest.fail("Recovery replayed a workload"))
        trial = sole_trial(c)
        assert c.reconcile() == [trial]
        assert c.store.get(trial)["state"] == State.COMPLETE
        anchor = c.store.anchor(trial)
        assert anchor.complete == (state in {State.SEALED, State.CLEANING})
        c.collector.verify(trial, anchor)
        assert c.backend.verify_cleanup(trial)
        assert c.reconcile() == []


@pytest.mark.parametrize(
    "owner,operation",
    [
        ("store", "create"),
        ("collector", "create"),
        ("watchdog", "issue"),
        ("backend", "prepare"),
        ("backend", "verify"),
        ("backend", "run"),
        ("backend", "terminate"),
        ("backend", "is_stopped"),
        ("collector", "seal"),
        ("store", "retain"),
        ("collector", "verify"),
        ("backend", "destroy"),
        ("backend", "verify_cleanup"),
    ],
)
def test_crash_after_effect_recovers_idempotently(
    tmp_path, scenario, monkeypatch, owner, operation
):
    with system(tmp_path) as c:
        target = getattr(c, owner)
        original = getattr(target, operation)

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(target, operation, crash)
            with pytest.raises(KeyboardInterrupt):
                c.run(scenario, development_policy())
    with system(tmp_path) as c:
        monkeypatch.setattr(c.backend, "run", lambda _: pytest.fail("Recovery replayed a workload"))
        trial = sole_trial(c)
        c.reconcile()
        assert c.store.get(trial)["state"] == State.COMPLETE
        c.collector.verify(trial, c.store.anchor(trial))
        assert c.backend.verify_cleanup(trial)


def interrupt_at(path, scenario, monkeypatch, state):
    with system(path) as c:
        transition = c.store.transition

        def crash(trial, target):
            transition(trial, target)
            if target == state:
                raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(c.store, "transition", crash)
            with pytest.raises(KeyboardInterrupt):
                c.run(scenario, development_policy())


@pytest.mark.parametrize(
    "fault", ["anchor_missing", "collector_missing", "receipt_tampered", "manifest_mismatch"]
)
def test_evidence_failure_after_sealing_preserves_marker_and_quarantines(
    tmp_path, scenario, monkeypatch, fault
):
    interrupt_at(tmp_path, scenario, monkeypatch, State.SEALED)
    with system(tmp_path) as c:
        trial = sole_trial(c)
        if fault == "anchor_missing":
            with c.store.db:
                c.store.db.execute("UPDATE trial_evidence SET seal_json = NULL")
        elif fault == "collector_missing":
            c.collector.db.execute("DELETE FROM receipts")
            c.collector.db.execute("DELETE FROM sources")
            c.collector.db.execute("DELETE FROM collections")
        elif fault == "receipt_tampered":
            c.collector.db.execute("DELETE FROM receipts WHERE collector_sequence = 4")
        else:
            c.collector.db.execute("UPDATE collections SET manifest_digest = ?", ("b" * 64,))
        c.reconcile()
        assert c.store.get(trial)["state"] == State.QUARANTINED
        assert not c.backend.verify_cleanup(trial)
        assert c.backend.is_stopped(trial)
        assert c.reconcile() == []


def test_missing_watchdog_record_after_preparation_stops_but_preserves_resources(
    tmp_path, scenario, monkeypatch
):
    interrupt_at(tmp_path, scenario, monkeypatch, State.RUNNING)
    with system(tmp_path) as c:
        trial = sole_trial(c)
        with c.watchdog.db:
            c.watchdog.db.execute("DELETE FROM leases")
        c.reconcile()
        assert c.store.get(trial)["state"] == State.QUARANTINED
        assert c.backend.is_stopped(trial)
        assert not c.backend.verify_cleanup(trial)


def test_unknown_stop_prevents_sealing_and_cleanup(tmp_path, scenario, monkeypatch):
    with system(tmp_path) as c:
        monkeypatch.setattr(c.backend, "is_stopped", lambda _: False)
        trial = c.run(scenario, development_policy())
        assert c.store.get(trial)["state"] == State.QUARANTINED
        assert c.store.anchor(trial) is None
        assert not c.collector.health(trial).sealed
        assert not c.backend.verify_cleanup(trial)


def test_seal_retention_failure_does_not_destroy(tmp_path, scenario, monkeypatch):
    with system(tmp_path) as c:

        def fail(*_args):
            raise RuntimeError("journal unavailable")

        monkeypatch.setattr(c.store, "retain", fail)
        trial = c.run(scenario, development_policy())
        assert c.store.get(trial)["state"] == State.QUARANTINED
        assert c.collector.health(trial).sealed
        assert not c.backend.verify_cleanup(trial)


def test_evidence_gates_cannot_be_skipped_by_legacy_controller(tmp_path, scenario):
    with system(tmp_path) as c:
        trial = uuid4()
        c.store.create(trial, resolve(scenario, development_policy()), evidence_required=True)
        c.store.transition(trial, State.STOPPING)
        with pytest.raises(ValueError, match="sealed"):
            c.store.transition(trial, State.CLEANING)
        c.store.transition(trial, State.COLLECTING)
        with pytest.raises(ValueError, match="seal"):
            c.store.transition(trial, State.SEALED)
        with pytest.raises(RuntimeError, match="supervised"):
            SimulationController(c.store, c.backend).reconcile()


def test_existing_legacy_records_still_reconcile(tmp_path, scenario):
    with system(tmp_path) as c:
        trial = uuid4()
        manifest = resolve(scenario, development_policy())
        c.store.create(trial, manifest)
        c.store.transition(trial, State.PREPARING)
        c.backend.prepare(trial, manifest)
        assert c.reconcile() == [trial]
        assert c.store.get(trial)["state"] == State.COMPLETE
        assert c.store.get(trial)["outcome"] == Outcome.INTERRUPTED
        assert c.store.summary(trial)["evidence_required"] is False


def test_evidence_quota_fault_flows_through_watchdog_and_is_retained(tmp_path, scenario):
    limited = scenario.model_copy(
        update={"budget": scenario.budget.model_copy(update={"evidence_bytes": 1})}
    )
    with system(tmp_path) as c:
        trial = c.run(limited, development_policy())
        assert c.watchdog.status(trial)["reason"] == "evidence_loss"
        assert c.store.get(trial)["outcome"] == Outcome.ERROR
        anchor = c.store.anchor(trial)
        assert anchor.collection_fault and not anchor.complete
        c.collector.verify(trial, anchor)
        assert c.backend.verify_cleanup(trial)


def test_retained_seal_cannot_be_replaced(tmp_path, scenario, monkeypatch):
    interrupt_at(tmp_path, scenario, monkeypatch, State.COLLECTING)
    with system(tmp_path) as c:
        trial = sole_trial(c)
        seal = c.collector.seal(trial)
        c.store.retain(trial, seal)
        c.store.retain(trial, seal)
        with pytest.raises(ValueError, match="replace"):
            c.store.retain(trial, seal.model_copy(update={"complete": True}))
        with pytest.raises(ValueError, match="match"):
            c.store.retain(trial, seal.model_copy(update={"manifest_digest": "b" * 64}))
        assert c.store.anchor(trial) == seal


def test_old_journal_schema_upgrades_without_changing_records(tmp_path, scenario):
    path = tmp_path / "trials.sqlite3"
    trial = uuid4()
    store = Store(path)
    store.create(trial, resolve(scenario, development_policy()))
    original = store.get(trial)
    with store.db:
        store.db.execute("DROP TABLE trial_evidence")
    store.close()
    with system(tmp_path) as c:
        assert c.store.get(trial) == original
        assert not c.store.evidence_required(trial)
        c.reconcile()
        assert c.store.get(trial)["state"] == State.COMPLETE
