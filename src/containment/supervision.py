"""Trusted local bridge from evidence collection health to watchdog revocation.

No network transport, background polling, execution admission, or production stop adapter.
"""

from uuid import UUID

from containment.evidence import Collector, EvidenceError
from containment.watchdog import Watchdog


class EvidenceSupervisor:
    def __init__(self, collector: Collector, watchdog: Watchdog):
        self.collector = collector
        self.watchdog = watchdog
        self._failed_trials: set[UUID] = set()

    def _fail(self, trial_id: UUID) -> None:
        # Keep retry intent even when the watchdog journal cannot currently commit it.
        self._failed_trials.add(trial_id)
        self.watchdog.revoke(trial_id, reason="evidence_loss")

    def _check(self, trial_id: UUID) -> bool:
        binding = self.watchdog.status(trial_id)["binding"]
        if trial_id in self._failed_trials:
            self._fail(trial_id)
            return False
        try:
            health = self.collector.health(trial_id)
            healthy = (
                health.trial_id == trial_id
                and health.manifest_digest == binding["manifest_digest"]
                and not health.collection_fault
                and not health.has_gap
                and not health.sealed
                and health.source_count > 0
                and health.observer_count > 0
                and health.ended_sources == 0
            )
        except Exception:
            # Unavailable or malformed evidence cannot justify continued execution.
            healthy = False
        if not healthy:
            self._fail(trial_id)
        return healthy

    def heartbeat(self, trial_id: UUID, token: str, *, sequence: int) -> dict:
        """Freshly check evidence before renewal; always process pending stops afterward.

        Call only from a trusted controller. A healthy result is not execution admission.
        """
        try:
            if self.watchdog.status(trial_id)["state"] == "active" and self._check(trial_id):
                self.watchdog.renew(trial_id, token, sequence=sequence)
        finally:
            self.watchdog.tick()
        return self.watchdog.status(trial_id)

    def ingest(self, trial_id: UUID, token: str, raw: bytes) -> dict:
        """Route ingress through this bridge to stop on write failures as well as latched faults.

        Invalid producer credentials alone cannot revoke a healthy trial. The caller must route
        the trial identity through this trusted bridge; producers never receive watchdog access.
        """
        self.watchdog.status(trial_id)  # Reject unknown watchdog scope before accepting evidence.
        try:
            return self.collector.ingest(trial_id, token, raw)
        except EvidenceError:
            # Role/quota/parse faults latch, whereas an authentication failure does not.
            raise
        except Exception:
            self._fail(trial_id)
            raise EvidenceError("Evidence ingestion unavailable; lease revoked") from None
        finally:
            try:
                self._check(trial_id)
            finally:
                self.watchdog.tick()
