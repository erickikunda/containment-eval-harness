"""Persistent simulation backend: markers only, no subprocesses or network access."""

import json
from pathlib import Path
from typing import Protocol
from uuid import UUID

from containment.admission import manifest_digest
from containment.models import Deployment, Manifest


class Backend(Protocol):
    def prepare(self, trial_id: UUID, manifest: Manifest) -> None: ...
    def verify(self, trial_id: UUID, manifest: Manifest) -> None: ...
    def run(self, trial_id: UUID) -> None: ...
    def terminate(self, trial_id: UUID) -> None: ...
    def is_stopped(self, trial_id: UUID) -> bool: ...
    def destroy(self, trial_id: UUID) -> None: ...
    def verify_cleanup(self, trial_id: UUID) -> bool: ...


class FakeBackend:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, trial_id: UUID) -> Path:
        return self.root / f"{UUID(str(trial_id))}.json"

    def _read(self, trial_id: UUID) -> dict:
        return json.loads(self._path(trial_id).read_text())

    def _write(self, trial_id: UUID, data: dict) -> None:
        target = self._path(trial_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True))
        temporary.replace(target)

    def prepare(self, trial_id: UUID, manifest: Manifest) -> None:
        if manifest.scenario.deployment != Deployment.FAKE:
            raise ValueError("Fake backend cannot execute a real deployment")
        if self._path(trial_id).exists():
            self.verify(trial_id, manifest)
            return
        self._write(trial_id, {"manifest_digest": manifest_digest(manifest), "running": False})

    def verify(self, trial_id: UUID, manifest: Manifest) -> None:
        if self._read(trial_id)["manifest_digest"] != manifest_digest(manifest):
            raise ValueError("Resource manifest mismatch")

    def run(self, trial_id: UUID) -> None:
        marker = self._read(trial_id)
        marker["running"] = True
        self._write(trial_id, marker)

    def terminate(self, trial_id: UUID) -> None:
        if self._path(trial_id).exists():
            marker = self._read(trial_id)
            marker["running"] = False
            self._write(trial_id, marker)

    def is_stopped(self, trial_id: UUID) -> bool:
        return not self._path(trial_id).exists() or self._read(trial_id)["running"] is False

    def destroy(self, trial_id: UUID) -> None:
        if not self.is_stopped(trial_id):
            raise RuntimeError("Cannot destroy a running resource")
        self._path(trial_id).unlink(missing_ok=True)
        self._path(trial_id).with_suffix(".tmp").unlink(missing_ok=True)

    def verify_cleanup(self, trial_id: UUID) -> bool:
        return not any(
            path.exists()
            for path in (self._path(trial_id), self._path(trial_id).with_suffix(".tmp"))
        )
