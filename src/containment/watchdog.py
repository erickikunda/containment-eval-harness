"""Local watchdog lease core; no scheduler, network service, or production stop adapter."""

import hashlib
import math
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import Field

from containment.lifecycle import controller_lock
from containment.models import StrictModel


class ResourceBinding(StrictModel):
    trial_id: UUID
    resource_id: str = Field(min_length=1, max_length=512)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class StopAdapter(Protocol):
    """Trusted adapter must verify immutable resource identity and bound every call."""

    def terminate(self, binding: ResourceBinding) -> None: ...
    def is_stopped(self, binding: ResourceBinding) -> bool: ...


def _duration(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 86400:
        raise ValueError("Duration must be finite and between 0 and 86400 seconds")
    return float(value)


class Watchdog:
    """One synchronous owner per local journal. Reopening revokes every active lease.

    Call tick regularly from a trusted supervisor. This library cannot enforce deadlines while
    its process is absent, stalled, or blocked inside a stop adapter.
    """

    def __init__(
        self, path: Path, adapter: StopAdapter, *, clock: Callable[[], float] = time.monotonic
    ):
        self.path = path
        self.adapter = adapter
        self.clock = clock
        self._stack: ExitStack | None = None
        self._db: sqlite3.Connection | None = None
        self._deadlines: dict[str, tuple[float, float, float]] = {}
        self._last_time: float | None = None
        self._clock_failed = False

    def __enter__(self) -> "Watchdog":
        if self._stack is not None:
            raise RuntimeError("Watchdog is already open")
        stack = ExitStack()
        try:
            stack.enter_context(controller_lock(self.path.with_suffix(self.path.suffix + ".lock")))
            db = sqlite3.connect(self.path)
            stack.callback(db.close)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous = FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS leases (
                trial_id TEXT PRIMARY KEY, resource_id TEXT UNIQUE NOT NULL,
                binding TEXT NOT NULL, token_hash TEXT NOT NULL,
                sequence INTEGER NOT NULL, state TEXT NOT NULL,
                reason TEXT, stop_confirmed INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT
            )""")
            with db:
                db.execute("""UPDATE leases SET state = 'stopping', reason = 'watchdog_restart'
                              WHERE state = 'active'""")
            self._db, self._stack = db, stack
            self._deadlines.clear()
            self._last_time, self._clock_failed = None, False
            return self
        except BaseException:
            stack.close()
            raise

    def __exit__(self, *_args) -> None:
        if self._stack:
            self._stack.close()
        self._stack, self._db = None, None
        self._deadlines.clear()

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Use Watchdog as a context manager")
        return self._db

    def _now(self) -> float | None:
        if self._clock_failed:
            self._revoke_for_clock_failure()
            return None
        try:
            now = self.clock()
            if (
                isinstance(now, bool)
                or not math.isfinite(now)
                or (self._last_time is not None and now < self._last_time)
            ):
                raise ValueError("Invalid monotonic clock")
        except Exception:
            self._clock_failed = True
            self._revoke_for_clock_failure()
            return None
        self._last_time = now
        return now

    def _revoke_for_clock_failure(self) -> None:
        # Retry persistence on later ticks if a previous journal write failed.
        with self.db:
            self.db.execute("""UPDATE leases SET state = 'stopping', reason = 'clock_failure'
                               WHERE state = 'active'""")

    def _row(self, trial_id: UUID) -> dict:
        row = self.db.execute(
            "SELECT * FROM leases WHERE trial_id = ?", (str(trial_id),)
        ).fetchone()
        if row is None:
            raise KeyError(str(trial_id))
        return dict(row)

    def status(self, trial_id: UUID) -> dict:
        """Last recorded state, not a fresh liveness or admission decision."""
        row = self._row(trial_id)
        del row["token_hash"]
        row["binding"] = ResourceBinding.model_validate_json(row["binding"]).model_dump(mode="json")
        row["stop_confirmed"] = bool(row["stop_confirmed"])
        return row

    def issue(self, binding: ResourceBinding, *, hard_seconds: float, lease_seconds: float) -> str:
        """Trusted controller operation. Returns the sole renewal bearer credential."""
        hard, ttl = _duration(hard_seconds), _duration(lease_seconds)
        if ttl > hard:
            raise ValueError("Health lease cannot exceed hard runtime")
        now = self._now()
        if now is None:
            raise RuntimeError("Clock failed; no new leases permitted")
        token = secrets.token_hex(32)
        with self.db:
            self.db.execute(
                """INSERT INTO leases
                (trial_id, resource_id, binding, token_hash, sequence, state)
                VALUES (?, ?, ?, ?, 0, 'active')""",
                (
                    str(binding.trial_id),
                    binding.resource_id,
                    binding.model_dump_json(),
                    hashlib.sha256(token.encode()).hexdigest(),
                ),
            )
        self._deadlines[str(binding.trial_id)] = (now + hard, now + ttl, ttl)
        return token

    def _expire(self, trial_id: UUID, now: float) -> bool:
        hard, health, _ = self._deadlines[str(trial_id)]
        if now >= min(hard, health):
            self.revoke(trial_id, reason="hard_deadline" if now >= hard else "lease_expired")
            return True
        return False

    def renew(self, trial_id: UUID, token: str, *, sequence: int) -> None:
        row = self._row(trial_id)
        if not secrets.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), row["token_hash"]
        ):
            raise ValueError("Invalid lease credential")
        if type(sequence) is not int or sequence != row["sequence"] + 1:
            raise ValueError("Renewals require the next sequence number")
        now = self._now()
        if row["state"] != "active" or now is None or self._expire(trial_id, now):
            raise RuntimeError("Lease is not renewable")
        hard, _, ttl = self._deadlines[str(trial_id)]
        with self.db:
            self.db.execute(
                "UPDATE leases SET sequence = ? WHERE trial_id = ?", (sequence, str(trial_id))
            )
        self._deadlines[str(trial_id)] = (hard, min(hard, now + ttl), ttl)

    def revoke(self, trial_id: UUID, *, reason: str) -> None:
        """Trusted control path for completion, evidence loss, budgets, or health faults."""
        if reason not in {
            "completed",
            "evidence_loss",
            "budget_exhausted",
            "health_failure",
            "lease_expired",
            "hard_deadline",
            "operator_stop",
        }:
            raise ValueError("Unknown revocation reason")
        self._row(trial_id)
        with self.db:
            self.db.execute(
                "UPDATE leases SET state = 'stopping', reason = ? "
                "WHERE trial_id = ? AND state = 'active'",
                (reason, str(trial_id)),
            )

    def tick(self) -> list[dict]:
        """Persist intent before stopping. Retry unresolved stops, never clean up resources."""
        now = self._now()
        rows = self.db.execute("SELECT trial_id FROM leases WHERE state = 'active'").fetchall()
        if now is not None:
            for row in rows:
                self._expire(UUID(row["trial_id"]), now)
        pending = self.db.execute(
            "SELECT * FROM leases WHERE state IN ('stopping', 'quarantined') "
            "AND stop_confirmed = 0 ORDER BY rowid"
        ).fetchall()
        for row in pending:
            trial_id = row["trial_id"]
            binding = ResourceBinding.model_validate_json(row["binding"])
            # Commit before the external effect: a crash can repeat termination, never renew it.
            with self.db:
                self.db.execute(
                    "UPDATE leases SET attempts = attempts + 1 WHERE trial_id = ?", (trial_id,)
                )
            error = None
            confirmed = False
            try:
                self.adapter.terminate(binding)
                confirmed = self.adapter.is_stopped(binding) is True
                if not confirmed:
                    error = "StopUnconfirmed"
            except Exception as exc:
                error = type(exc).__name__
            state = "stopped" if confirmed and row["state"] != "quarantined" else "quarantined"
            with self.db:
                self.db.execute(
                    "UPDATE leases SET state = ?, stop_confirmed = ?, error = ? WHERE trial_id = ?",
                    (state, int(confirmed), error or row["error"], trial_id),
                )
        return [
            self.status(UUID(row["trial_id"]))
            for row in self.db.execute("SELECT trial_id FROM leases ORDER BY rowid").fetchall()
        ]
