# Evidence health and watchdog integration

`EvidenceSupervisor` connects the local collector to the watchdog lease core. The trusted
controller routes evidence ingestion and lease heartbeats through this object. It adds no
execution authority, network service, scheduler, or AWS calls. The existing simulation CLI
remains unchanged; tests exercise real local collector/watchdog journals with simulated stops.

## Health checks before renewal

`Collector.health(trial_id)` reads the collection and validates its current receipt chain in a
single SQLite transaction. It returns identity, manifest digest, collection fault/gap flags,
sealed state, and source/observer/end counts. It does not seal the collection, change evidence,
or return tokens or producer text. Missing collections, inconsistent receipts, and storage errors
raise exceptions.

For an active lease, `EvidenceSupervisor.heartbeat(trial_id, token, sequence=...)` obtains a fresh
health snapshot and renews only when:

- Trial identity and manifest digest match the watchdog's immutable binding.
- The collection is open, with no latched fault or producer-sequence gap.
- There is at least one registered observer, and no registered source has ended.

Failure or inability to read health revokes the lease with reason `evidence_loss`. Heartbeats
always run watchdog stop processing afterward, including when renewal fails because of invalid
credentials, sequence, clock, or deadline. A slow health read cannot extend an already expired
lease. Non-active leases are never renewed. A returned watchdog status can be stopped or
quarantined; it is not an admission receipt.

Health is a local consistency check. An empty collection with a registered observer can pass
during startup. This does not prove observer liveness, heartbeat freshness, expected event rate,
source authenticity beyond the existing registry, or semantic validity of evidence. A database
owner can rewrite an unsealed history consistently. External evidence protection and retained
seals remain required; the health snapshot is not a substitute for them.

## Ingestion failures and immediate stop processing

Use `supervisor.ingest(trial_id, producer_token, raw_bytes)` for ingress. It preserves collector
authentication, role checks, receipt acknowledgments, and idempotent retries, then checks health
and runs stop processing before returning. A receipt still means the evidence was committed;
it does not mean the workload is allowed to continue. For example, a sequence gap is retained
as evidence and triggers revocation in the same call.

Malformed authenticated events, forbidden roles, and quota overflows latch collector faults and
stop the trial. Ordinary storage exceptions also revoke it, even if rollback leaves no fault
flag and the next health read succeeds. Those exceptions are replaced with a generic ingestion
error without storage details. Invalid producer credentials alone cannot taint or revoke an
otherwise healthy collection. An independent evidence fault or expired deadline can still stop
the trial during that call.

Faults detected through direct `Collector.ingest` calls are seen at the next supervised heartbeat
only if they remain observable. A transient write failure through that direct path may leave no
record; routing all ingress through the supervisor is therefore an integration requirement.

## Completion and recovery

On normal completion, revoke the watchdog with `reason="completed"` and confirm termination,
then collect final `end` events and seal the evidence. Final ingestion remains possible after
stop. Existing stop reasons are preserved. Ending any source while the lease is still active is
conservatively treated as evidence loss, since that source cannot continue reporting.

If a detected failure cannot be written to the watchdog journal, the supervisor retains an
in-memory failure latch and retries revocation before any later renewal. Database failures still
propagate and require the external emergency path described in [the watchdog contract](watchdog.md).
Do not replace the supervisor object while keeping active leases after an error. Restart the
supervisor and watchdog together: reopening the watchdog journal revokes all active leases,
covering loss of in-memory state. There is no atomic transaction across the two databases.

Unconfirmed termination stays quarantined. Later evidence recovery cannot revive a revoked
lease or release quarantine. The bridge does not clean up resources or automatically seal
evidence. Interrupted ingestion may leave an acknowledged or unacknowledged committed receipt;
existing collector retry rules apply.

## Integration boundary

These APIs belong to the trusted controller, not to the agent. Only evidence bytes and producer
credentials enter through the routed ingress path. Lease credentials, scope routing, revocation,
health inspection, and journals must remain outside the experiment's compromise domain.

The supervisor checks only the trial named in a call; watchdog `tick()` also processes expired
and pending stops for its other records. A future scheduler must check every active trial at a
bounded cadence. Calls are synchronous: a blocked collector or provider call can stall this
process. Independently scheduled watchdog ticks, authenticated remote transport, provider call
bounds, observer freshness policy, COLLECTING/SEALED lifecycle recovery, and live isolation and
termination tests remain deployment gates.

Run the local integration tests with:

```sh
uv run pytest tests/test_supervision.py -q
```
