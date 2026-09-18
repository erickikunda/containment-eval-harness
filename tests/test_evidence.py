import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from containment.evidence import Collector, EvidenceError, Limits, ProducerEvent, digest


def raw(sequence=1, kind="note", text=""):
    return ProducerEvent(sequence=sequence, kind=kind, text=text).model_dump_json().encode()


@pytest.fixture
def collector(tmp_path):
    result = Collector(tmp_path / "evidence.sqlite3")
    yield result
    result.close()


@pytest.fixture
def collection(collector):
    trial_id = uuid4()
    observer, guest = collector.create(trial_id, digest("manifest"), ("observer", "guest"))
    return trial_id, observer, guest


def test_identity_comes_from_credential_and_extra_claim_rejected(collector, collection):
    trial_id, observer, guest = collection
    receipt = collector.ingest(trial_id, guest.token, raw())
    assert receipt["source_id"] == guest.source_id
    assert receipt["source_role"] == "guest"
    claimed = json.loads(raw()) | {"source_role": "observer", "source_id": observer.source_id}
    with pytest.raises(EvidenceError):
        collector.ingest(trial_id, guest.token, json.dumps(claimed).encode())
    with pytest.raises(EvidenceError, match="must stop"):
        collector.ingest(trial_id, guest.token, raw(sequence=2))
    assert collector.seal(trial_id).collection_fault is True


@pytest.mark.parametrize("kind", ["setup_valid", "setup_leak", "protected_read"])
def test_guest_cannot_assert_observer_events(collector, collection, kind):
    trial_id, _, guest = collection
    with pytest.raises(EvidenceError, match="observer"):
        collector.ingest(trial_id, guest.token, raw(kind=kind))


def test_observer_cannot_impersonate_submission(collector, collection):
    trial_id, observer, _ = collection
    with pytest.raises(EvidenceError, match="impersonate"):
        collector.ingest(trial_id, observer.token, raw(kind="submission"))


def test_wrong_trial_and_bad_tokens_cannot_taint_collection(collector, collection):
    trial_id, _, guest = collection
    other = uuid4()
    collector.create(other, digest("other"), ("guest",))
    with pytest.raises(EvidenceError, match="Unauthenticated"):
        collector.ingest(other, guest.token, raw())
    with pytest.raises(EvidenceError, match="Unauthenticated"):
        collector.ingest(trial_id, "invalid", raw())
    assert collector.seal(other).collection_fault is False
    assert collector.seal(trial_id).collection_fault is False


def test_token_plaintext_is_not_stored_or_repr_exposed(collector, collection):
    _, _, guest = collection
    row = collector.db.execute("SELECT * FROM sources WHERE id = ?", (guest.source_id,)).fetchone()
    assert guest.token not in repr(tuple(row))
    assert guest.token not in repr(guest)


def test_idempotent_retry_and_conflicting_replay(collector, collection):
    trial_id, _, guest = collection
    first = collector.ingest(trial_id, guest.token, raw())
    assert collector.ingest(trial_id, guest.token, raw()) == first
    with pytest.raises(EvidenceError, match="Conflicting"):
        collector.ingest(trial_id, guest.token, raw(text="changed"))
    seal = collector.seal(trial_id)
    assert seal.event_count == 1
    assert seal.collection_fault is True


@pytest.mark.parametrize("kind", ["gap", "no_end", "missing_source"])
def test_incomplete_streams_are_explicit(collector, collection, kind):
    trial_id, observer, guest = collection
    collector.ingest(trial_id, observer.token, raw(kind="end"))
    if kind != "missing_source":
        collector.ingest(
            trial_id,
            guest.token,
            raw(sequence=2 if kind == "gap" else 1, kind="end" if kind == "gap" else "note"),
        )
    anchor = collector.seal(trial_id)
    assert anchor.complete is False
    assert collector.verify(trial_id, anchor).seal == anchor


def test_complete_seal_idempotence_and_ingestion_revocation(collector, collection):
    trial_id, observer, guest = collection
    for grant in (observer, guest):
        collector.ingest(trial_id, grant.token, raw(kind="end"))
    anchor = collector.seal(trial_id)
    assert anchor.complete is True
    assert collector.seal(trial_id) == anchor
    assert collector.verify(trial_id, anchor).seal == anchor
    with pytest.raises(EvidenceError, match="sealed"):
        collector.ingest(trial_id, guest.token, raw(kind="end"))


def test_source_cannot_emit_after_end(collector, collection):
    trial_id, _, guest = collection
    collector.ingest(trial_id, guest.token, raw(kind="end"))
    with pytest.raises(EvidenceError, match="ended"):
        collector.ingest(trial_id, guest.token, raw(sequence=2))


@pytest.mark.parametrize(
    "limits,first,second",
    [
        (Limits(event_bytes=50), None, raw(text="a" * 60)),
        (Limits(total_bytes=50), None, raw()),
        (Limits(event_count=1), raw(), raw(sequence=2)),
    ],
)
def test_bounds_latch_fault_without_accepting_event(collector, limits, first, second):
    trial_id = uuid4()
    (grant,) = collector.create(trial_id, digest("manifest"), ("guest",), limits)
    if first:
        collector.ingest(trial_id, grant.token, first)
    with pytest.raises(EvidenceError, match="limit"):
        collector.ingest(trial_id, grant.token, second)
    anchor = collector.seal(trial_id)
    assert anchor.collection_fault is True
    assert anchor.complete is False
    assert anchor.event_count == (1 if first else 0)


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"sequence":true,"kind":"end"}'])
def test_malformed_input_latches_fault(collector, collection, payload):
    trial_id, _, guest = collection
    with pytest.raises(EvidenceError):
        collector.ingest(trial_id, guest.token, payload)
    assert collector.seal(trial_id).collection_fault


def test_validation_error_does_not_echo_producer_content(collector, collection):
    trial_id, _, guest = collection
    payload = b'{"sequence": "secret-canary", "kind": "note"}'
    with pytest.raises(EvidenceError) as raised:
        collector.ingest(trial_id, guest.token, payload)
    assert "secret-canary" not in str(raised.value)


def test_receipt_survives_reopen(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    trial_id = uuid4()
    collector = Collector(path)
    (grant,) = collector.create(trial_id, digest("manifest"), ("guest",))
    receipt = collector.ingest(trial_id, grant.token, raw(kind="end"))
    collector.close()
    reopened = Collector(path)
    try:
        assert reopened.ingest(trial_id, grant.token, raw(kind="end")) == receipt
        anchor = reopened.seal(trial_id)
        assert reopened.verify(trial_id, anchor).receipts == (receipt,)
    finally:
        reopened.close()


@pytest.mark.parametrize("tamper", ["payload", "truncate", "source", "fault", "manifest", "index"])
def test_retained_seal_detects_tampering(collector, collection, tamper):
    trial_id, observer, guest = collection
    for grant in (observer, guest):
        collector.ingest(trial_id, grant.token, raw(kind="end"))
    anchor = collector.seal(trial_id)
    if tamper == "payload":
        row = collector.db.execute("SELECT receipt_json FROM receipts LIMIT 1").fetchone()
        receipt = json.loads(row[0])
        receipt["event"]["text"] = "tampered"
        collector.db.execute(
            "UPDATE receipts SET receipt_json = ? WHERE collector_sequence = 1",
            (json.dumps(receipt),),
        )
    elif tamper == "truncate":
        collector.db.execute("DELETE FROM receipts WHERE collector_sequence = 2")
    elif tamper == "source":
        collector.db.execute(
            "UPDATE sources SET role = 'observer' WHERE id = ?", (guest.source_id,)
        )
    elif tamper == "fault":
        collector.db.execute("UPDATE collections SET fault = 1")
    elif tamper == "manifest":
        collector.db.execute("UPDATE collections SET manifest_digest = ?", (digest("other"),))
    else:
        collector.db.execute(
            "UPDATE receipts SET producer_sequence = 50 WHERE collector_sequence = 1"
        )
    with pytest.raises(EvidenceError):
        collector.verify(trial_id, anchor)


def test_failed_sqlite_write_is_not_acknowledged(collector, collection):
    trial_id, _, guest = collection
    collector.db.execute("""
        CREATE TRIGGER fail_insert BEFORE INSERT ON receipts
        BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END
    """)
    with pytest.raises(sqlite3.Error, match="injected disk failure"):
        collector.ingest(trial_id, guest.token, raw())
    assert collector.seal(trial_id).event_count == 0


def test_two_connections_cannot_overspend_event_quota(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    trial_id = uuid4()
    collector = Collector(path)
    grants = collector.create(
        trial_id, digest("manifest"), ("guest", "guest"), Limits(event_count=1)
    )

    def attempt(grant):
        connection = Collector(path)
        try:
            connection.ingest(trial_id, grant.token, raw(kind="end"))
            return True
        except EvidenceError:
            return False
        finally:
            connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(attempt, grants)) == [False, True]
        assert collector.seal(trial_id).event_count == 1
    finally:
        collector.close()
