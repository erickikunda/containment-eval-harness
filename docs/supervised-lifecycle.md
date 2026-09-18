# Supervised simulation lifecycle

New `containment simulate` trials connect the fake backend, evidence collector, evidence supervisor,
and watchdog lease core. They exercise orchestration using marker files and synthetic `note`/`end`
events only. No agent, command, model, container, VM, or AWS operation runs. These notes are not
escape evidence, and this controller does not invoke the escape oracle.

```text
CREATED → PREPARING → VERIFYING → RUNNING → STOPPING
       → COLLECTING → SEALED → CLEANING → COMPLETE
Unconfirmed stop, seal/verification failure, or cleanup failure → QUARANTINED
```

## Execution and finalization

The controller first reconciles prior unfinished trials. For each new trial, it atomically records
the manifest and requirement for evidence in the lifecycle journal, creates an observer/guest source
registry, and issues a watchdog lease bound to the trial, manifest, and absolute marker path. It
then prepares/verifies the fake backend and checks evidence health before marking it running.
Another supervised heartbeat follows synthetic evidence ingestion. The short health lease is at
most five seconds; the hard runtime is bounded by the scenario budget and watchdog's 24-hour cap.
The collector byte quota is bounded by the scenario budget and its 16-MiB limit.

To finish a trial, the controller:

1. Persists STOPPING, revokes the lease, processes the watchdog stop, and requires confirmation.
2. Rechecks the marker's manifest identity and stopped state, including during recovery.
3. Persists COLLECTING. On an uninterrupted successful simulation, it sends final source events.
4. Seals the collection, retains the seal in the controller journal, and verifies the collector
   against that retained seal before persisting SEALED.
5. Verifies the retained seal again before entering CLEANING, deletes the fake marker, verifies
   its absence, and records COMPLETE.

The watchdog's quarantine status blocks finalization even if a later retry confirms termination.
Errors in sealing, seal retention, evidence verification, or cleanup quarantine the lifecycle
record. No cleanup starts on an evidence/stop failure. After cleanup has started, an interruption
or failed absence check can leave the marker already deleted; the journals and evidence remain.
Ordinary errors are reported by stage and exception type, without producer/provider error text.

## Persistence and recovery

The state directory contains three local journals:

| File | Contents |
| --- | --- |
| `trials.sqlite3` | Immutable trial manifest, lifecycle events, evidence-required flag, retained seal |
| `evidence.sqlite3` | Source registry, receipts, collector seal; also used by separate evidence fixtures |
| `watchdog.sqlite3` | Resource binding, lease/revocation state, stop attempts and confirmation |

The controller lock covers mutations; watchdog ownership has its own lock. Direct library callers
must provide the same exclusive controller ownership. There is no distributed transaction across
these journals. Process death leaves intent for the next invocation to reconcile. Opening the
watchdog revokes active leases; `list` does not open it or change leases.

Recovery never calls `backend.run`, reissues a lease, or reconstructs producer credentials.
Consequently, interrupted evidence may lack final source events. It is sealed as incomplete rather
than filled in with fabricated observations. COMPLETE means lifecycle processing and cleanup
finished, not that evidence is complete or a containment result is valid. The CLI includes
`evidence_required` and the full retained `evidence_seal`, whose `complete` and `collection_fault`
fields make this distinction explicit. Interrupted trials retain an interrupted outcome; a run
previously marked successful but finalized with incomplete evidence becomes an error.

The recovery rules include:

- A CREATED record can precede collector/lease setup. After confirming marker absence/stop, the
  controller can create an empty collection for that never-prepared trial and seal it incomplete.
- A missing watchdog record after preparation stops the known marker but quarantines the trial.
  Missing evidence after preparation is not replaced with a new history.
- If the collector committed its seal before the controller retained it, COLLECTING recovery can
  obtain the same idempotent seal and retain it. If a seal was already retained, it is never replaced.
- SEALED and CLEANING recovery require the existing retained seal and verify it before cleanup.
  Missing or mismatched seals quarantine the trial. Marker cleanup is idempotent if already done.
- COMPLETE and QUARANTINED lifecycle records are not automatically resumed or released. Inspection
  and any eventual manual resolution workflow remain separate work.

Some interrupted setup states are deliberately conservative. For example, a second crash after a
never-prepared trial enters STOPPING but before collector/lease recovery finishes can leave an
ambiguous missing watchdog record; it quarantines rather than guessing that setup was harmless.

## Compatibility and trust limits

An additive `trial_evidence` table marks new supervised trials. Existing records have no such row
and retain the original STOPPING → CLEANING recovery path; no evidence is invented for legacy runs.
The old `SimulationController` library remains for legacy simulations and refuses to finalize an
evidence-enabled trial. Store transition checks prevent new trials from skipping collection/sealing
or advancing without a retained seal. This protects application flow, not against a database owner.

The retained seal is in a different database from the collector, but both are local files under
the same owner. They are not independent trust domains or immutable storage. Recovery between
collector sealing and first retention still trusts that local collector. Production requires a
protected external anchor, independent collection/termination services, authenticated transport,
observer freshness checks, bounded calls and scheduling, and live fault tests on dedicated AWS
infrastructure. Marker absence proves only this simulation's stop; it is never infrastructure
termination evidence. Real execution remains disabled.

## Validation

```sh
uv run pytest tests/test_supervised_lifecycle.py tests/test_cli.py -q
```

Tests interrupt every new lifecycle transition and setup/stop/seal/retain/cleanup effect, reopen
the journals, and forbid workload replay. They also cover legacy schema upgrade, retained-seal
replacement, missing evidence, tampered receipts, lost watchdog records, unknown termination,
quota faults, seal retention failure, and listing without opening the watchdog.
