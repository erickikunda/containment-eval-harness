# Watchdog lease core

`containment.watchdog` implements a local, synchronous lease and stop-recovery state machine.
It is a preparatory slice for the independent watchdog described in the system design. It is
not deployed as an independent service or connected to AWS. The supervised simulation controller
uses this core locally. No real execution
gate has changed. Its tests use a fake clock and recording adapters without starting workloads.

## Lease contract

An operator-controlled `ResourceBinding` fixes a trial UUID, resource identity, and manifest
digest. `issue(binding, hard_seconds=..., lease_seconds=...)` records the binding and returns a
random 256-bit renewal token. Only its SHA-256 hash is stored. The resource and trial identifiers
cannot be reused in the same journal, including after completion. Durations must be finite,
positive, at most 24 hours, and the health lease cannot exceed the hard runtime.

The hard deadline is set once when the lease is issued. Each valid `renew(trial_id, token,
sequence=...)` moves the health deadline forward by the original lease duration, capped by the
hard deadline. Renewal requires the correct trial credential and the next integer sequence,
starting at one. Duplicates, skipped sequences, and another trial's token fail. A renewal at or
after either deadline revokes the lease; a delayed heartbeat cannot revive it. An ambiguous
renewal response does not justify a blind retry with a new sequence: query trusted state and
stop if synchronization cannot be established. Transport authentication is future work.

`revoke(trial_id, reason=...)` is a trusted controller operation for completion, evidence loss,
budget exhaustion, health failure, or an operator stop. It persists the first reason and is
idempotent for an already revoked record. It does not itself call the provider. Renewal does not
clear revocation. The trial must never receive access to issue/revoke operations or the journal;
the renewal token also belongs to the trusted controller, not the agent.

## Stop processing

Use `Watchdog(path, adapter)` as a context manager, owned by one supervisor. An advisory process
lock excludes another owner of the same journal path. SQLite uses full synchronous commits;
the database and lock must be on trusted local storage, accessed through one canonical path.
The journal is not an authoritative evidence store and offers no protection against its owner
rewriting or deleting it.

The supervisor must call `tick()` regularly. Each tick:

1. Revokes expired leases using the watchdog's monotonic clock.
2. Selects revoked or quarantined records whose stop is still unconfirmed.
3. Commits an attempt counter before calling the adapter's `terminate(binding)`.
4. Calls `is_stopped(binding)` and accepts only the literal boolean `True` as confirmation.
5. Records `stopped` on confirmed termination, or `quarantined` on failure/uncertainty.

Ordinary provider exceptions are recorded by class name without exception messages. One failed
stop does not skip other records. Unconfirmed stops are retried on subsequent ticks. Quarantine
is sticky: later confirmation sets `stop_confirmed` but does not release the resource for reuse.
The core never destroys resources, removes evidence, or clears quarantine. Retry cadence and
provider rate limits are the supervisor's responsibility; there is no internal polling thread.

`status()` reports the last persisted state without exporting the token hash. It does not sample
the clock, call a provider, prove current liveness, or authorize workload execution.

## Recovery and faults

Reopening the journal revokes every active lease with reason `watchdog_restart`. Monotonic
deadlines are deliberately not reconstructed across process or clock epochs. Existing stop
intent, quarantine, and confirmation survive reopening. The supervisor must call `tick()` after
opening to reconcile pending stops. Closing the context only releases local resources; it is
not a shutdown/termination operation.

A process death during a stop leaves durable intent and an attempt count. The next owner retries
termination against the same immutable binding. Production adapters must therefore be idempotent.
Database errors propagate and must trigger the external supervisor's emergency path. If the
journal cannot commit stop intent or an attempt, this core does not proceed with that external
action. Its local journal cannot be the only route to emergency shutdown.

A backwards, non-finite, or failed clock observation revokes all active leases and latches a clock
fault for the process lifetime. Renewals and new leases then fail; stop processing still works.
The default is Python's monotonic clock, independent of guest time. Host suspension, process
stalls, watchdog death, and a blocking adapter can prevent timely ticks. There is no wall-clock
or real-time termination guarantee from this library alone.

## Integration gate before real execution

The local [evidence supervisor](evidence-supervision.md) implements evidence checks before renewal
and revocation on ingestion failures. It does not provide the deployed services below.

The next deployment integration must supply:

- A supervisor outside the experiment worker's compromise domain, with an explicit polling
  bound, storage-failure path, restart recovery, alerting, and independent hard termination.
- A stop adapter that checks the full immutable binding before every action, uses narrowly
  scoped permissions, bounds calls, and verifies infrastructure termination independently.
  A missing Kubernetes object alone is insufficient. The existing marker-based `FakeBackend`
  is not a production stop adapter.
- Authenticated controller-to-watchdog transport and protection against unauthorized renewal,
  revocation, replay, or disclosure of credentials.
- Controller admission that requires a live lease before execution, renews only after trusted
  health checks, and revokes on collector faults, budget exhaustion, completion, and policy drift.
- Live failure tests for supervisor/worker outages, stuck calls, unknown provider results,
  deadline enforcement, isolation, orphan discovery, and quarantine retention.

These integrations depend on the selected AWS topology. The lease API grants no authority to
execute, and metadata inspection remains blocked regardless of watchdog state.

## Local verification

```sh
uv run pytest tests/test_watchdog.py -q
```

Tests exercise deadline boundaries, nonextendable hard limits, credentials and sequences,
revocation reasons, durable intent before external calls, clock faults, restart recovery,
crashes during stop/verification, uncertain termination, journal errors, and exclusive ownership.
