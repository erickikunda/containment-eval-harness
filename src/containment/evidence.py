"""Bounded local evidence collector. No network listener or external trust boundary yet.

Only the ingress methods receive producer-controlled bytes. Administrative methods must
remain outside that interface. A trusted caller must retain the returned seal independently.
"""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from containment.models import StrictModel

ZERO_HASH = "0" * 64
Role = Literal["guest", "observer"]
Kind = Literal["note", "submission", "setup_valid", "setup_leak", "protected_read", "end"]


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class ProducerEvent(StrictModel):
    sequence: int = Field(gt=0, le=2**31 - 1)
    kind: Kind
    text: str = Field(default="", max_length=8192)


class Limits(StrictModel):
    event_bytes: int = Field(default=16384, gt=0, le=65536)
    total_bytes: int = Field(default=1_048_576, gt=0, le=16_777_216)
    event_count: int = Field(default=1000, gt=0, le=10000)


@dataclass(frozen=True)
class Grant:
    source_id: str
    token: str = field(repr=False)


class Seal(StrictModel):
    trial_id: str
    manifest_digest: str
    event_count: int
    head_digest: str
    sources_digest: str
    collection_fault: bool
    complete: bool
    sealed_at: str
    seal_digest: str


@dataclass(frozen=True)
class VerifiedEvidence:
    seal: Seal
    # Returned objects are detached from storage; consumers must not treat mutable
    # in-process Python objects as a security boundary.
    receipts: tuple[dict, ...]


class CollectionHealth(StrictModel):
    trial_id: UUID
    manifest_digest: str
    collection_fault: bool
    has_gap: bool
    sealed: bool
    source_count: int
    observer_count: int
    ended_sources: int


class EvidenceError(ValueError):
    pass


class Collector:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                manifest_digest TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                fault INTEGER NOT NULL DEFAULT 0,
                seal_json TEXT
            );
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL REFERENCES collections(id),
                role TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS receipts (
                trial_id TEXT NOT NULL REFERENCES collections(id),
                collector_sequence INTEGER NOT NULL,
                source_id TEXT NOT NULL REFERENCES sources(id),
                producer_sequence INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY (trial_id, collector_sequence),
                UNIQUE (trial_id, source_id, producer_sequence)
            );
        """)

    def close(self) -> None:
        self.db.close()

    def health(self, trial_id: UUID) -> CollectionHealth:
        """Trusted live snapshot; validates receipts without sealing or exporting producer text.

        This checks local collection consistency, not observer liveness or independent integrity.
        """
        with self._transaction():
            collection = self._collection(trial_id)
            snapshot, receipts = self._snapshot(trial_id, datetime.now(UTC).isoformat())
            roles = [
                row["role"]
                for row in self.db.execute(
                    "SELECT role FROM sources WHERE trial_id = ?", (str(trial_id),)
                )
            ]
            return CollectionHealth(
                trial_id=trial_id,
                manifest_digest=snapshot.manifest_digest,
                collection_fault=snapshot.collection_fault,
                has_gap=any(receipt["missing_before"] > 0 for receipt in receipts),
                sealed=collection["seal_json"] is not None,
                source_count=len(roles),
                observer_count=roles.count("observer"),
                ended_sources=sum(receipt["event"]["kind"] == "end" for receipt in receipts),
            )

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create(
        self,
        trial_id: UUID,
        manifest_digest: str,
        roles: tuple[Role, ...],
        limits: Limits | None = None,
    ) -> tuple[Grant, ...]:
        """Trusted setup: source registry is fixed before the first event."""
        if not roles or len(roles) > 16 or any(role not in {"guest", "observer"} for role in roles):
            raise EvidenceError("Expected 1–16 supported source roles")
        if len(manifest_digest) != 64 or any(c not in "0123456789abcdef" for c in manifest_digest):
            raise EvidenceError("Invalid manifest digest")
        limits = limits or Limits()
        grants = tuple(Grant(str(uuid4()), secrets.token_urlsafe(32)) for _ in roles)
        with self._transaction():
            self.db.execute(
                "INSERT INTO collections(id, manifest_digest, limits_json) VALUES (?, ?, ?)",
                (str(trial_id), manifest_digest, limits.model_dump_json()),
            )
            self.db.executemany(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                [
                    (g.source_id, str(trial_id), role, digest(g.token))
                    for g, role in zip(grants, roles, strict=True)
                ],
            )
        return grants

    def _collection(self, trial_id: UUID) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM collections WHERE id = ?", (str(trial_id),)).fetchone()
        if row is None:
            raise EvidenceError("Unknown evidence collection")
        return row

    def _receipts(self, trial_id: UUID) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM receipts WHERE trial_id = ? ORDER BY collector_sequence",
            (str(trial_id),),
        )
        receipts = []
        for row in rows:
            receipt = json.loads(row["receipt_json"])
            if (
                receipt["trial_id"] != row["trial_id"]
                or receipt["collector_sequence"] != row["collector_sequence"]
                or receipt["source_id"] != row["source_id"]
                or receipt["event"]["sequence"] != row["producer_sequence"]
            ):
                raise EvidenceError("Evidence index does not match receipt")
            receipts.append(receipt)
        return receipts

    def ingest(self, trial_id: UUID, token: str, raw: bytes) -> dict:
        """Authenticate scope before parsing; acknowledge only after SQLite commits.

        Exact canonical retries are idempotent while the collection is open. A
        conflicting replay, malformed event, forbidden kind, or quota overflow from
        an authenticated source latches collection_fault and raises EvidenceError.
        Callers must stop their trial on that error. Invalid credentials cannot taint
        someone else's collection. Sealing revokes all ingestion.
        """
        if len(token) > 256:
            raise EvidenceError("Unauthenticated source or wrong trial")
        failure = None
        receipt = None
        with self._transaction():
            source = self.db.execute(
                "SELECT * FROM sources WHERE trial_id = ? AND token_hash = ?",
                (str(trial_id), digest(token)),
            ).fetchone()
            if source is None:
                raise EvidenceError("Unauthenticated source or wrong trial")
            collection = self._collection(trial_id)
            if collection["seal_json"] is not None:
                raise EvidenceError("Collection is sealed")
            if collection["fault"]:
                raise EvidenceError("Collection faulted; trial must stop")
            try:
                limits = Limits.model_validate_json(collection["limits_json"])
                if len(raw) > limits.event_bytes:
                    raise EvidenceError("Event byte limit exceeded")
                try:
                    event = ProducerEvent.model_validate_json(raw)
                except ValidationError as exc:
                    # Do not echo producer bytes into control-plane error logs.
                    raise EvidenceError("Invalid producer event") from exc
                if source["role"] == "guest" and event.kind not in {"note", "submission", "end"}:
                    raise EvidenceError("Source role cannot assert observer evidence")
                if source["role"] == "observer" and event.kind == "submission":
                    raise EvidenceError("Observer cannot impersonate a guest submission")
                receipt = self._append(trial_id, source, event, limits)
            except ValueError as exc:
                self.db.execute("UPDATE collections SET fault = 1 WHERE id = ?", (str(trial_id),))
                failure = EvidenceError(str(exc))
        if failure is not None:
            raise failure
        assert receipt is not None
        return receipt

    def _append(self, trial_id, source, event, limits) -> dict:
        receipts = self._receipts(trial_id)
        own = [r for r in receipts if r["source_id"] == source["id"]]
        event_data = event.model_dump(mode="json")
        for previous in own:
            if previous["event"]["sequence"] == event.sequence:
                if previous["event"] != event_data:
                    raise EvidenceError("Conflicting producer-sequence replay")
                return previous
        if own and own[-1]["event"]["kind"] == "end":
            raise EvidenceError("Source already ended")
        last_sequence = own[-1]["event"]["sequence"] if own else 0
        if event.sequence <= last_sequence:
            raise EvidenceError("Out-of-order producer sequence")
        if len(receipts) >= limits.event_count:
            raise EvidenceError("Event count limit exceeded")
        receipt = {
            "trial_id": str(trial_id),
            "collector_sequence": len(receipts) + 1,
            "source_id": source["id"],
            "source_role": source["role"],
            "received_at": datetime.now(UTC).isoformat(),
            "missing_before": event.sequence - last_sequence - 1,
            "event": event_data,
            "event_digest": digest(canonical(event_data)),
            "previous_digest": receipts[-1]["receipt_digest"] if receipts else ZERO_HASH,
        }
        receipt["receipt_digest"] = digest(canonical(receipt))
        encoded = canonical(receipt)
        if (
            sum(len(canonical(r).encode()) for r in receipts) + len(encoded.encode())
            > limits.total_bytes
        ):
            raise EvidenceError("Collection byte limit exceeded")
        self.db.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
            (str(trial_id), receipt["collector_sequence"], source["id"], event.sequence, encoded),
        )
        return receipt

    def _snapshot(self, trial_id: UUID, sealed_at: str) -> tuple[Seal, list[dict]]:
        collection = self._collection(trial_id)
        sources = [
            dict(row)
            for row in self.db.execute(
                "SELECT id, role FROM sources WHERE trial_id = ? ORDER BY id", (str(trial_id),)
            )
        ]
        roles = {source["id"]: source["role"] for source in sources}
        receipts = self._receipts(trial_id)
        previous = ZERO_HASH
        last_sequences: dict[str, int] = {}
        ended: set[str] = set()
        has_gap = False
        for sequence, receipt in enumerate(receipts, 1):
            body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
            source_id = receipt["source_id"]
            event = ProducerEvent.model_validate_json(canonical(receipt["event"]))
            gap = event.sequence - last_sequences.get(source_id, 0) - 1
            if (
                receipt["receipt_digest"] != digest(canonical(body))
                or receipt["trial_id"] != str(trial_id)
                or receipt["collector_sequence"] != sequence
                or receipt["previous_digest"] != previous
                or receipt["event_digest"] != digest(canonical(receipt["event"]))
                or source_id not in roles
                or receipt["source_role"] != roles[source_id]
                or source_id in ended
                or gap < 0
                or receipt["missing_before"] != gap
            ):
                raise EvidenceError("Evidence integrity check failed")
            last_sequences[source_id] = event.sequence
            if event.kind == "end":
                ended.add(source_id)
            has_gap |= gap > 0
            previous = receipt["receipt_digest"]
        body = {
            "trial_id": str(trial_id),
            "manifest_digest": collection["manifest_digest"],
            "event_count": len(receipts),
            "head_digest": previous,
            "sources_digest": digest(canonical({"sources": sources})),
            "collection_fault": bool(collection["fault"]),
            "complete": bool(sources)
            and ended == set(roles)
            and not has_gap
            and not collection["fault"],
            "sealed_at": sealed_at,
        }
        return Seal(**body, seal_digest=digest(canonical(body))), receipts

    def seal(self, trial_id: UUID) -> Seal:
        """Trusted operation; stops ingestion even when the stream is incomplete."""
        with self._transaction():
            collection = self._collection(trial_id)
            if collection["seal_json"] is not None:
                anchor = Seal.model_validate_json(collection["seal_json"])
                self._verify(trial_id, anchor)
                return anchor
            anchor, _ = self._snapshot(trial_id, datetime.now(UTC).isoformat())
            self.db.execute(
                "UPDATE collections SET seal_json = ? WHERE id = ?",
                (anchor.model_dump_json(), str(trial_id)),
            )
        return anchor

    def _verify(self, trial_id: UUID, anchor: Seal) -> VerifiedEvidence:
        collection = self._collection(trial_id)
        if collection["seal_json"] is None:
            raise EvidenceError("Collection is not sealed")
        actual, receipts = self._snapshot(trial_id, anchor.sealed_at)
        if actual != anchor or Seal.model_validate_json(collection["seal_json"]) != anchor:
            raise EvidenceError("Evidence does not match the retained seal")
        return VerifiedEvidence(actual, tuple(receipts))

    def verify(self, trial_id: UUID, anchor: Seal) -> VerifiedEvidence:
        """Compare to a seal retained by a trusted caller, not read back from this DB."""
        with self._transaction():
            return self._verify(trial_id, anchor)
