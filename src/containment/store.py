"""Local lifecycle journal, not authoritative security evidence."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from containment.admission import canonical_manifest, manifest_digest
from containment.models import Manifest, Outcome, State

ALLOWED = {
    State.CREATED: {State.PREPARING, State.STOPPING},
    State.PREPARING: {State.VERIFYING, State.STOPPING},
    State.VERIFYING: {State.RUNNING, State.STOPPING},
    State.RUNNING: {State.STOPPING},
    State.STOPPING: {State.CLEANING, State.QUARANTINED},
    State.CLEANING: {State.COMPLETE, State.QUARANTINED},
    State.COMPLETE: set(),
    State.QUARANTINED: set(),
}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trials (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                manifest TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                outcome TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id TEXT NOT NULL REFERENCES trials(id),
                received_at TEXT NOT NULL,
                state TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.db.close()

    def _event(self, trial_id: str, state: State) -> None:
        self.db.execute(
            "INSERT INTO events(trial_id, received_at, state) VALUES (?, ?, ?)",
            (trial_id, datetime.now(UTC).isoformat(), state.value),
        )

    def create(self, trial_id: UUID, manifest: Manifest) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO trials(id, state, manifest, manifest_digest) VALUES (?, ?, ?, ?)",
                (
                    str(trial_id),
                    State.CREATED,
                    canonical_manifest(manifest),
                    manifest_digest(manifest),
                ),
            )
            self._event(str(trial_id), State.CREATED)

    def get(self, trial_id: UUID) -> dict:
        row = self.db.execute("SELECT * FROM trials WHERE id = ?", (str(trial_id),)).fetchone()
        if row is None:
            raise KeyError(str(trial_id))
        return dict(row)

    def records(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM trials ORDER BY rowid")]

    def transition(self, trial_id: UUID, target: State) -> None:
        current = State(self.get(trial_id)["state"])
        if target not in ALLOWED[current]:
            raise ValueError(f"Invalid transition: {current} -> {target}")
        with self.db:
            updated = self.db.execute(
                "UPDATE trials SET state = ? WHERE id = ? AND state = ?",
                (target.value, str(trial_id), current.value),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Concurrent state transition")
            self._event(str(trial_id), target)

    def set_outcome(self, trial_id: UUID, outcome: Outcome, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE trials SET outcome = ?, error = ? WHERE id = ?",
                (outcome.value, error, str(trial_id)),
            )

    def summary(self, trial_id: UUID) -> dict:
        record = self.get(trial_id)
        manifest = json.loads(record.pop("manifest"))
        record["scenario_id"] = manifest["scenario"]["scenario_id"]
        record["deployment"] = manifest["scenario"]["deployment"]
        record["simulation_only"] = True
        return record
