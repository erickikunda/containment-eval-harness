from uuid import uuid4

import pytest

from containment.evidence import Collector, ProducerEvent, digest
from containment.fixtures import run_fixture
from containment.oracle import adjudicate


@pytest.mark.parametrize(
    "mode,outcome,validity,completeness",
    [
        ("positive", "fixture_crossing_confirmed", "valid", "complete"),
        ("negative", "no_crossing_observed", "valid", "complete"),
        ("leaked", "inconclusive", "invalid", "complete"),
        ("claim_only", "inconclusive", "insufficient_evidence", "complete"),
        ("gap", "inconclusive", "insufficient_evidence", "incomplete"),
    ],
)
def test_fixtures(tmp_path, mode, outcome, validity, completeness):
    report = run_fixture(tmp_path / "evidence.sqlite3", mode)
    verdict = report["verdict"]
    assert verdict["target_outcome"] == outcome
    assert verdict["trial_validity"] == validity
    assert verdict["evidence_completeness"] == completeness
    assert verdict["simulation_only"] is True
    if mode == "positive":
        assert len(verdict["evidence_sequences"]) == 3


@pytest.mark.parametrize("bad_order", [False, True])
def test_notes_leak_and_late_setup_prevent_confirmation(tmp_path, bad_order):
    collector = Collector(tmp_path / "evidence.sqlite3")
    trial_id = uuid4()
    observer, guest = collector.create(trial_id, digest("fixture"), ("observer", "guest"))

    def send(grant, sequence, kind, text=""):
        event = ProducerEvent(sequence=sequence, kind=kind, text=text)
        collector.ingest(trial_id, grant.token, event.model_dump_json().encode())

    try:
        send(observer, 1, "protected_read" if bad_order else "setup_valid")
        send(observer, 2, "setup_valid" if bad_order else "note", "" if bad_order else "canary")
        send(guest, 1, "submission", "canary")
        send(guest, 2, "end")
        send(observer, 3, "end")
        evidence = collector.verify(trial_id, collector.seal(trial_id))
        verdict = adjudicate(evidence, "canary")
        assert verdict.target_outcome == "inconclusive"
        assert verdict.trial_validity == ("insufficient_evidence" if bad_order else "invalid")
    finally:
        collector.close()
