import hashlib
import sqlite3
from uuid import uuid4

import pytest

from containment.watchdog import ResourceBinding, Watchdog


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Adapter:
    def __init__(self):
        self.stops = []
        self.verified = []
        self.confirmed = True
        self.stop_error = None
        self.verify_error = None
        self.before_stop = None

    def terminate(self, binding):
        if self.before_stop:
            self.before_stop(binding)
        self.stops.append(binding)
        if self.stop_error:
            raise self.stop_error

    def is_stopped(self, binding):
        self.verified.append(binding)
        if self.verify_error:
            raise self.verify_error
        return self.confirmed


@pytest.fixture
def setup(tmp_path):
    binding = ResourceBinding(
        trial_id=uuid4(), resource_id="simulation-worker-1", manifest_digest="a" * 64
    )
    return tmp_path / "watchdog.sqlite3", Clock(), Adapter(), binding


def test_renewal_cannot_extend_hard_deadline(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=10, lease_seconds=4)
        for sequence, elapsed in enumerate((3, 6, 9), start=1):
            clock.now = 100 + elapsed
            watchdog.renew(binding.trial_id, token, sequence=sequence)
            assert watchdog.tick()[0]["state"] == "active"
        clock.now = 110
        report = watchdog.tick()[0]
        assert report["state"] == "stopped"
        assert report["reason"] == "hard_deadline"
        assert report["stop_confirmed"] is True
        assert adapter.stops == adapter.verified == [binding]
        watchdog.tick()
        assert adapter.stops == [binding]


def test_expired_lease_cannot_be_resurrected_before_tick(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        clock.now += 5
        with pytest.raises(RuntimeError, match="not renewable"):
            watchdog.renew(binding.trial_id, token, sequence=1)
        assert watchdog.status(binding.trial_id)["state"] == "stopping"
        watchdog.tick()
        assert watchdog.status(binding.trial_id)["reason"] == "lease_expired"


def test_health_lease_expiry_on_tick(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        clock.now += 5
        assert watchdog.tick()[0]["reason"] == "lease_expired"
        assert adapter.stops == [binding]


@pytest.mark.parametrize(
    "reason", ["completed", "evidence_loss", "budget_exhausted", "health_failure", "operator_stop"]
)
def test_revocation_is_persisted_before_external_stop(setup, reason):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.revoke(binding.trial_id, reason=reason)
        watchdog.revoke(binding.trial_id, reason="operator_stop")
        with pytest.raises(RuntimeError):
            watchdog.renew(binding.trial_id, token, sequence=1)

        def check_persisted(_binding):
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT state, reason, attempts FROM leases").fetchone() == (
                    "stopping",
                    reason,
                    1,
                )

        adapter.before_stop = check_persisted
        assert watchdog.tick()[0]["state"] == "stopped"


@pytest.mark.parametrize("mode", ["unconfirmed", "truthy", "stop_error", "verify_error"])
def test_unknown_stop_quarantines_and_retries_without_clearing_quarantine(setup, mode):
    path, clock, adapter, binding = setup
    if mode == "unconfirmed":
        adapter.confirmed = False
    elif mode == "truthy":
        adapter.confirmed = "true"
    elif mode == "stop_error":
        adapter.stop_error = RuntimeError("sensitive provider error")
    else:
        adapter.verify_error = RuntimeError("sensitive provider error")
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.revoke(binding.trial_id, reason="health_failure")
        report = watchdog.tick()[0]
        assert report["state"] == "quarantined"
        assert report["stop_confirmed"] is False
        assert "sensitive" not in str(report)
    adapter.stop_error = adapter.verify_error = None
    adapter.confirmed = True
    with Watchdog(path, adapter, clock=clock) as watchdog:
        report = watchdog.tick()[0]
        assert report["state"] == "quarantined"
        assert report["stop_confirmed"] is True
        assert report["attempts"] == 2
        watchdog.tick()
        assert len(adapter.stops) == 2


def test_restart_revokes_instead_of_reusing_monotonic_deadline(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
    clock.now = 0  # A different clock epoch cannot resurrect the previous run.
    with Watchdog(path, adapter, clock=clock) as watchdog:
        with pytest.raises(RuntimeError):
            watchdog.renew(binding.trial_id, token, sequence=1)
        report = watchdog.tick()[0]
        assert report["reason"] == "watchdog_restart"
        assert report["state"] == "stopped"


@pytest.mark.parametrize("crash_point", ["stop_error", "verify_error"])
def test_crash_after_stop_request_retries_on_restart(setup, crash_point):
    path, clock, adapter, binding = setup
    setattr(adapter, crash_point, KeyboardInterrupt())
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.revoke(binding.trial_id, reason="evidence_loss")
        with pytest.raises(KeyboardInterrupt):
            watchdog.tick()
        assert watchdog.status(binding.trial_id)["state"] == "stopping"
    setattr(adapter, crash_point, None)
    with Watchdog(path, adapter, clock=clock) as watchdog:
        report = watchdog.tick()[0]
        assert report["state"] == "stopped"
        assert report["reason"] == "evidence_loss"
        assert report["attempts"] == 2


@pytest.mark.parametrize("bad_time", [99, float("nan"), float("inf"), True, None])
def test_clock_fault_stops_all_active_leases_and_latches(setup, bad_time):
    path, clock, adapter, binding = setup
    other = binding.model_copy(update={"trial_id": uuid4(), "resource_id": "simulation-worker-2"})
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.issue(other, hard_seconds=30, lease_seconds=5)
        clock.now = bad_time
        reports = watchdog.tick()
        assert all(
            row["reason"] == "clock_failure" and row["state"] == "stopped" for row in reports
        )
        clock.now = 105
        with pytest.raises(RuntimeError, match="Clock failed"):
            watchdog.issue(other, hard_seconds=30, lease_seconds=5)


def test_renewal_credentials_are_trial_bound_and_sequences_cannot_replay(setup):
    path, clock, adapter, binding = setup
    other = binding.model_copy(update={"trial_id": uuid4(), "resource_id": "simulation-worker-2"})
    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        other_token = watchdog.issue(other, hard_seconds=30, lease_seconds=5)
        with pytest.raises(ValueError, match="credential"):
            watchdog.renew(binding.trial_id, other_token, sequence=1)
        watchdog.renew(binding.trial_id, token, sequence=1)
        for sequence in (1, 0, 3, True, 2.0):
            with pytest.raises(ValueError, match="sequence"):
                watchdog.renew(binding.trial_id, token, sequence=sequence)
        clock.now += 5
        assert all(row["state"] == "stopped" for row in watchdog.tick())
        with sqlite3.connect(path) as db:
            stored = db.execute(
                "SELECT token_hash FROM leases WHERE trial_id = ?", (str(binding.trial_id),)
            ).fetchone()[0]
        assert stored == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(watchdog.status(binding.trial_id))
        assert "token_hash" not in watchdog.status(binding.trial_id)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True, 86401])
def test_invalid_deadlines_rejected_without_creating_record(setup, duration):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        with pytest.raises(ValueError):
            watchdog.issue(binding, hard_seconds=duration, lease_seconds=1)
        with pytest.raises(ValueError):
            watchdog.issue(binding, hard_seconds=30, lease_seconds=duration)
        assert watchdog.tick() == []


def test_resource_and_trial_cannot_be_reassigned_even_after_stop(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        with pytest.raises(ValueError):
            watchdog.issue(binding, hard_seconds=1, lease_seconds=2)
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.revoke(binding.trial_id, reason="completed")
        watchdog.tick()
        for replacement in (
            binding,
            binding.model_copy(update={"trial_id": uuid4()}),
            binding.model_copy(update={"resource_id": "different"}),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                watchdog.issue(replacement, hard_seconds=30, lease_seconds=5)
        assert watchdog.status(binding.trial_id)["binding"] == binding.model_dump(mode="json")


def test_lock_excludes_second_owner_and_is_released(setup):
    path, clock, adapter, _ = setup
    with Watchdog(path, adapter, clock=clock):
        with pytest.raises(RuntimeError, match="Another controller"):
            with Watchdog(path, adapter, clock=clock):
                pytest.fail("Second owner acquired watchdog")
    with Watchdog(path, adapter, clock=clock) as watchdog:
        assert watchdog.tick() == []


def test_journal_failure_prevents_external_action(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.revoke(binding.trial_id, reason="evidence_loss")
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TRIGGER fail_attempt BEFORE UPDATE OF attempts ON leases
                          BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
        with pytest.raises(sqlite3.DatabaseError):
            watchdog.tick()
        assert adapter.stops == []
        assert watchdog.status(binding.trial_id)["state"] == "stopping"
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_attempt")
        assert watchdog.tick()[0]["state"] == "stopped"


def test_one_adapter_failure_does_not_skip_other_stops(setup):
    path, clock, adapter, binding = setup
    other = binding.model_copy(update={"trial_id": uuid4(), "resource_id": "simulation-worker-2"})

    def fail_first(selected):
        if selected == binding:
            raise RuntimeError("first stop failed")

    adapter.before_stop = fail_first
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.issue(other, hard_seconds=30, lease_seconds=5)
        clock.now += 5
        reports = watchdog.tick()
        assert [row["state"] for row in reports] == ["quarantined", "stopped"]
        assert adapter.stops == [other]


def test_clock_exception_also_revokes_on_renewal(setup):
    path, clock, adapter, binding = setup

    def failed_clock():
        raise OSError("unavailable")

    with Watchdog(path, adapter, clock=clock) as watchdog:
        token = watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        watchdog.clock = failed_clock
        with pytest.raises(RuntimeError):
            watchdog.renew(binding.trial_id, token, sequence=1)
        assert watchdog.tick()[0]["reason"] == "clock_failure"


def test_clock_fault_revocation_retries_after_journal_recovers(setup):
    path, clock, adapter, binding = setup
    with Watchdog(path, adapter, clock=clock) as watchdog:
        watchdog.issue(binding, hard_seconds=30, lease_seconds=5)
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TRIGGER fail_state BEFORE UPDATE OF state ON leases
                          BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
        clock.now = float("nan")
        with pytest.raises(sqlite3.DatabaseError):
            watchdog.tick()
        assert adapter.stops == []
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_state")
        clock.now = 100
        report = watchdog.tick()[0]
        assert report["state"] == "stopped"
        assert report["reason"] == "clock_failure"
