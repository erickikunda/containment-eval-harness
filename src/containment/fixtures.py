"""Harmless evidence fixtures, not live agent evaluations or actual boundary escapes."""

import secrets
from pathlib import Path
from typing import Literal
from uuid import uuid4

from containment.evidence import Collector, ProducerEvent, digest
from containment.oracle import adjudicate

Fixture = Literal["positive", "negative", "leaked", "claim_only", "gap"]


def run_fixture(path: Path, mode: Fixture) -> dict:
    if mode not in {"positive", "negative", "leaked", "claim_only", "gap"}:
        raise ValueError("Unknown fixture")
    collector = Collector(path)
    trial_id = uuid4()
    canary = secrets.token_hex(32)
    try:
        observer, guest = collector.create(
            trial_id, digest(f"canary-fixture-v1:{mode}"), ("observer", "guest")
        )

        def send(grant, sequence, kind, text=""):
            event = ProducerEvent(sequence=sequence, kind=kind, text=text)
            return collector.ingest(trial_id, grant.token, event.model_dump_json().encode())

        send(observer, 1, "setup_valid")
        observer_sequence = 2
        if mode in {"positive", "leaked", "gap"}:
            send(observer, observer_sequence, "protected_read")
            observer_sequence += 1
        if mode == "leaked":
            send(observer, observer_sequence, "setup_leak")
            observer_sequence += 1
        send(
            guest,
            2 if mode == "gap" else 1,
            "submission",
            "wrong" if mode == "negative" else canary,
        )
        send(guest, 3 if mode == "gap" else 2, "end")
        send(observer, observer_sequence, "end")
        anchor = collector.seal(trial_id)
        evidence = collector.verify(trial_id, anchor)
        verdict = adjudicate(evidence, canary)
        return {
            "trial_id": str(trial_id),
            "fixture": mode,
            "seal": anchor.model_dump(mode="json"),
            "verdict": verdict.model_dump(mode="json"),
        }
    finally:
        collector.close()
