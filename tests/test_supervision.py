import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from containment.evidence import Collector, EvidenceError, Limits, ProducerEvent
from containment.supervision import EvidenceSupervisor
from containment.watchdog import ResourceBinding, Watchdog


def event(sequence=1, kind="note", text=""):
    return ProducerEvent(sequence=sequence, kind=kind, text=text).model_dump_json().encode()


@pytest.fixture
def system(tmp_path):
    state = SimpleNamespace(now=100.0, stops=[], confirmed=True)
    binding = ResourceBinding(trial_id=uuid4(), resource_id="fake-worker", manifest_digest="a" * 64)
    collector = Collector(tmp_path / "evidence.sqlite3")
    observer, guest = collector.create(
        binding.trial_id, binding.manifest_digest, ("observer", "guest")
    )
    adapter = SimpleNamespace(
        terminate=lambda bound: state.stops.append(bound),
        is_stopped=lambda _: state.confirmed,
    )
    with Watchdog(tmp_path / "watchdog.sqlite3", adapter, clock=lambda: state.now) as watchdog:
        token = watchdog.issue(binding, hard_seconds=20, lease_seconds=5)
        yield SimpleNamespace(
            collector=collector,
            watchdog=watchdog,
            state=state,
            binding=binding,
            trial=binding.trial_id,
            observer=observer,
            guest=guest,
            token=token,
            supervisor=EvidenceSupervisor(collector, watchdog),
        )
    collector.close()


def test_health_does_not_seal_or_export_payloads(system):
    s = system
    s.supervisor.ingest(s.trial, s.guest.token, event(text="private payload"))
    health = s.collector.health(s.trial)
    assert health.source_count == 2 and health.observer_count == 1
    assert not health.sealed and not health.has_gap and not health.collection_fault
    assert "private payload" not in health.model_dump_json()
    assert s.collector.db.execute("SELECT seal_json FROM collections").fetchone()[0] is None
    s.supervisor.ingest(s.trial, s.guest.token, event(2))


def test_fresh_health_check_renews_but_hard_deadline_still_wins(system):
    s = system
    for sequence, elapsed in enumerate((4, 8, 12, 16), start=1):
        s.state.now = 100 + elapsed
        assert s.supervisor.heartbeat(s.trial, s.token, sequence=sequence)["state"] == "active"
    s.state.now = 120
    with pytest.raises(RuntimeError, match="not renewable"):
        s.supervisor.heartbeat(s.trial, s.token, sequence=5)
    assert s.watchdog.status(s.trial)["reason"] == "hard_deadline"
    assert s.state.stops == [s.binding]


@pytest.mark.parametrize("payload", [b"{", event(kind="protected_read"), event(text="x" * 100)])
def test_authenticated_ingress_fault_stops_without_waiting_for_heartbeat(system, payload):
    s = system
    s.collector.db.execute(
        "UPDATE collections SET limits_json = ?", (Limits(event_bytes=80).model_dump_json(),)
    )
    with pytest.raises(EvidenceError):
        s.supervisor.ingest(s.trial, s.guest.token, payload)
    report = s.watchdog.status(s.trial)
    assert report["state"] == "stopped" and report["reason"] == "evidence_loss"
    assert report["sequence"] == 0


def test_gap_is_accepted_as_evidence_but_revokes_execution_lease(system):
    s = system
    receipt = s.supervisor.ingest(s.trial, s.observer.token, event(3))
    assert receipt["missing_before"] == 2
    assert s.collector.health(s.trial).has_gap
    assert s.watchdog.status(s.trial)["reason"] == "evidence_loss"
    assert s.state.stops == [s.binding]


def test_invalid_producer_credentials_cannot_revoke_healthy_trial(system):
    s = system
    other = uuid4()
    (grant,) = s.collector.create(other, "b" * 64, ("guest",))
    for token in ("wrong", grant.token):
        with pytest.raises(EvidenceError, match="Unauthenticated"):
            s.supervisor.ingest(s.trial, token, event())
    assert s.watchdog.status(s.trial)["state"] == "active"
    assert not s.collector.health(s.trial).collection_fault
    assert s.state.stops == []


@pytest.mark.parametrize("fault", ["manifest", "missing", "sealed", "end", "observer", "tamper"])
def test_heartbeat_fails_closed_for_unusable_collection(system, fault):
    s = system
    if fault == "manifest":
        s.collector.db.execute("UPDATE collections SET manifest_digest = ?", ("b" * 64,))
    elif fault == "missing":
        s.collector.db.execute("DELETE FROM sources")
        s.collector.db.execute("DELETE FROM collections")
    elif fault == "sealed":
        s.collector.seal(s.trial)
    elif fault == "end":
        s.collector.ingest(s.trial, s.observer.token, event(kind="end"))
    elif fault == "observer":
        s.collector.db.execute("UPDATE sources SET role = 'guest'")
    else:
        receipt = s.collector.ingest(s.trial, s.guest.token, event())
        receipt["event"]["text"] = "tampered"
        s.collector.db.execute("UPDATE receipts SET receipt_json = ?", (json.dumps(receipt),))
    report = s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert report["state"] == "stopped" and report["reason"] == "evidence_loss"
    assert report["sequence"] == 0


def test_collector_unavailable_stops_trial(system, monkeypatch):
    s = system

    def unavailable(_trial):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(s.collector, "health", unavailable)
    report = s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert report["state"] == "stopped"
    assert "private database detail" not in str(report)


def test_ingestion_write_failure_revokes_even_when_health_read_recovers(system):
    s = system
    s.collector.db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON receipts
                            BEGIN SELECT RAISE(ABORT, 'private storage error'); END""")
    with pytest.raises(EvidenceError, match="lease revoked") as error:
        s.supervisor.ingest(s.trial, s.guest.token, event())
    assert "private storage error" not in str(error.value)
    assert not s.collector.health(s.trial).collection_fault
    assert s.watchdog.status(s.trial)["reason"] == "evidence_loss"
    s.collector.db.execute("DROP TRIGGER fail_insert")
    assert s.supervisor.heartbeat(s.trial, s.token, sequence=1)["state"] == "stopped"


def test_stop_uncertainty_stays_quarantined_when_collector_recovers(system):
    s = system
    s.state.confirmed = False
    s.supervisor.ingest(s.trial, s.guest.token, event(2))
    assert s.watchdog.status(s.trial)["state"] == "quarantined"
    s.state.confirmed = True
    report = s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert report["state"] == "quarantined" and report["stop_confirmed"]
    assert report["sequence"] == 0


def test_slow_health_read_cannot_renew_an_expired_lease(system, monkeypatch):
    s = system
    original = s.collector.health

    def slow(trial):
        s.state.now += 5
        return original(trial)

    monkeypatch.setattr(s.collector, "health", slow)
    with pytest.raises(RuntimeError, match="not renewable"):
        s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert s.watchdog.status(s.trial)["reason"] == "lease_expired"
    assert s.state.stops == [s.binding]


def test_failed_renewal_auth_still_processes_expired_stops(system):
    s = system
    s.state.now += 5
    with pytest.raises(ValueError, match="credential"):
        s.supervisor.heartbeat(s.trial, "wrong", sequence=1)
    assert s.watchdog.status(s.trial)["reason"] == "lease_expired"


def test_normal_completion_reason_survives_end_and_seal(system):
    s = system
    s.watchdog.revoke(s.trial, reason="completed")
    s.watchdog.tick()
    for grant in (s.observer, s.guest):
        s.supervisor.ingest(s.trial, grant.token, event(kind="end"))
    seal = s.collector.seal(s.trial)
    assert seal.complete
    report = s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert report["reason"] == "completed" and report["state"] == "stopped"
    assert s.collector.verify(s.trial, seal).seal == seal


def test_evidence_failure_latches_when_watchdog_journal_temporarily_fails(system, monkeypatch):
    s = system
    original = s.collector.health

    def unavailable(_trial):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(s.collector, "health", unavailable)
    with s.watchdog.db:
        s.watchdog.db.execute("""CREATE TRIGGER fail_state BEFORE UPDATE OF state ON leases
                                BEGIN SELECT RAISE(ABORT, 'journal unavailable'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert s.watchdog.status(s.trial)["sequence"] == 0
    monkeypatch.setattr(s.collector, "health", original)
    with s.watchdog.db:
        s.watchdog.db.execute("DROP TRIGGER fail_state")
    report = s.supervisor.heartbeat(s.trial, s.token, sequence=1)
    assert report["state"] == "stopped" and report["reason"] == "evidence_loss"
    assert report["sequence"] == 0
