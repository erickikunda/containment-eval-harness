"""Durable replay reservations. Commit before effects; never retry uncertain actions."""

import json
from contextlib import contextmanager
from uuid import UUID

from containment.models import Budget


class BudgetExceeded(ValueError):
    pass


class ReplayJournal:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS replay_runs (
                trial_id TEXT PRIMARY KEY REFERENCES trials(id),
                script_digest TEXT NOT NULL, limits_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0, output_bytes INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS replay_actions (
                trial_id TEXT NOT NULL REFERENCES replay_runs(trial_id),
                sequence INTEGER NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                tokens INTEGER NOT NULL, output_bytes INTEGER NOT NULL, result TEXT,
                PRIMARY KEY (trial_id, sequence)
            );
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def register(self, trial: UUID, script_digest: str, budget: Budget, output_bytes: int):
        limits = dict(
            model_calls=budget.model_calls,
            tool_calls=budget.tool_calls,
            tokens=budget.model_tokens,
            output_bytes=output_bytes,
        )
        with self.transaction():
            self.db.execute(
                "INSERT INTO replay_runs(trial_id, script_digest, limits_json) VALUES (?, ?, ?)",
                (str(trial), script_digest, json.dumps(limits)),
            )

    def _run(self, trial):
        row = self.db.execute(
            "SELECT * FROM replay_runs WHERE trial_id = ?", (str(trial),)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown replay trial")
        return dict(row)

    def reserve(self, trial: UUID, kind: str, *, tokens: int = 0, output_bytes: int) -> int:
        if kind not in {"model", "tool"}:
            raise ValueError("Unknown action kind")
        if any(type(value) is not int or value < 0 for value in (tokens, output_bytes)):
            raise ValueError("Invalid reservation")
        with self.transaction():
            run = self._run(trial)
            if run["state"] != "active":
                raise RuntimeError("Replay is closed")
            if self.db.execute(
                "SELECT 1 FROM replay_actions WHERE trial_id = ? AND state = 'reserved'",
                (str(trial),),
            ).fetchone():
                raise RuntimeError("Uncertain action cannot be retried")
            deltas = dict(
                model_calls=int(kind == "model"),
                tool_calls=int(kind == "tool"),
                tokens=tokens,
                output_bytes=output_bytes,
            )
            limits = json.loads(run["limits_json"])
            if any(run[key] + delta > limits[key] for key, delta in deltas.items()):
                raise BudgetExceeded("Replay budget exhausted")
            sequence = self.db.execute(
                "SELECT COUNT(*) + 1 FROM replay_actions WHERE trial_id = ?", (str(trial),)
            ).fetchone()[0]
            self.db.execute(
                """INSERT INTO replay_actions VALUES (?, ?, ?, 'reserved', ?, ?, NULL)""",
                (str(trial), sequence, kind, tokens, output_bytes),
            )
            self.db.execute(
                """UPDATE replay_runs SET model_calls = model_calls + ?,
                tool_calls = tool_calls + ?, tokens = tokens + ?, output_bytes = output_bytes + ?
                WHERE trial_id = ?""",
                (deltas["model_calls"], deltas["tool_calls"], tokens, output_bytes, str(trial)),
            )
        return sequence

    def settle(self, trial: UUID, sequence: int, result: str, *, tokens: int = 0):
        size = len(result.encode("utf-8"))
        with self.transaction():
            run = self._run(trial)
            row = self.db.execute(
                "SELECT * FROM replay_actions WHERE trial_id = ? AND sequence = ?",
                (str(trial), sequence),
            ).fetchone()
            if run["state"] != "active" or row is None or row["state"] != "reserved":
                raise RuntimeError("Action is not settleable")
            if (
                type(tokens) is not int
                or not 0 <= tokens <= row["tokens"]
                or size > row["output_bytes"]
            ):
                raise BudgetExceeded("Response exceeds reserved budget")
            self.db.execute(
                "UPDATE replay_runs SET tokens = tokens - ?, output_bytes = output_bytes - ? "
                "WHERE trial_id = ?",
                (row["tokens"] - tokens, row["output_bytes"] - size, str(trial)),
            )
            self.db.execute(
                "UPDATE replay_actions SET state = 'done', tokens = ?, "
                "output_bytes = ?, result = ? "
                "WHERE trial_id = ? AND sequence = ?",
                (tokens, size, result, str(trial), sequence),
            )

    def complete(self, trial: UUID):
        with self.transaction():
            if self._run(trial)["state"] != "active":
                raise RuntimeError("Replay is closed")
            if self.db.execute(
                "SELECT 1 FROM replay_actions WHERE trial_id = ? AND state != 'done'", (str(trial),)
            ).fetchone():
                raise RuntimeError("Replay has unresolved actions")
            self.db.execute(
                "UPDATE replay_runs SET state = 'complete' WHERE trial_id = ?", (str(trial),)
            )

    def abort(self, trial: UUID, *, state: str = "interrupted"):
        if state not in {"failed", "interrupted"}:
            raise ValueError("Invalid abort state")
        with self.transaction():
            self.db.execute(
                "UPDATE replay_runs SET state = ? WHERE trial_id = ? AND state = 'active'",
                (state, str(trial)),
            )
            self.db.execute(
                "UPDATE replay_actions SET state = 'uncertain' "
                "WHERE trial_id = ? AND state = 'reserved'",
                (str(trial),),
            )

    def summary(self, trial: UUID) -> dict | None:
        try:
            row = self._run(trial)
        except KeyError:
            return None
        row["limits"] = json.loads(row.pop("limits_json"))
        row["actions"] = [
            dict(action)
            for action in self.db.execute(
                "SELECT sequence, kind, state, tokens, output_bytes, result FROM replay_actions "
                "WHERE trial_id = ? ORDER BY sequence",
                (str(trial),),
            )
        ]
        row["accounting"] = "utf8_byte_units_not_provider_tokens"
        row["model_cost_microusd"] = 0
        return row
