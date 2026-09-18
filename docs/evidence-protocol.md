# Evidence protocol, slice 2

## Scope

This slice implements a local collector library and harmless oracle fixtures. It does not launch
agents, capture system calls, watch VM boundaries, or establish an external security boundary.
The library assumes its administrative caller and local database directory are trusted.

## Trusted setup and untrusted ingress

`Collector.create(trial_id, manifest_digest, roles, limits)` creates a fixed source registry.
Each source gets a random credential scoped to that trial and role. Only SHA-256 hashes of those
high-entropy credentials are stored. Plaintext credentials are returned once and hidden from the
grant object's repr. A future transport must deliver observer credentials only to independent
observers, and guest credentials only to their associated producers. Tokens do not prove that
a compromised producer tells the truth.

Only `ingest(trial_id, token, raw_bytes)` belongs on a future producer-facing API. Creation, sealing,
verification, and database access are trusted administrative operations. A producer cannot select
its source identity or role inside the event. Unknown fields are rejected.

```json
{"sequence": 1, "kind": "submission", "text": "synthetic-value"}
```

Guest kinds: `note`, `submission`, `end`. Observer kinds: `note`, `setup_valid`, `setup_leak`,
`protected_read`, `end`. The fixture has exactly one protected canary per trial; `protected_read`
means the trusted fixture observer asserts access to that target. A real observer will need an
explicit target/capability schema and an independently validated implementation.

The collector authenticates the token/trial before parsing the event. It imposes raw event byte,
canonical receipt byte, and event-count limits. Receipt byte accounting includes JSON encoding
expansion and collector metadata. These limits bound each collection, not total database size or
physical SQLite storage overhead. A deployment must also enforce disk, request, concurrency, and
rate quotas outside this library. No producer may create unlimited collections.

## Receipts and durable acknowledgments

Each accepted event receives a trial-local collector sequence, server timestamp, authenticated
source identity/role, producer sequence, observed missing-sequence count, event digest, preceding
receipt digest, and receipt digest. SQLite uses synchronous FULL and BEGIN IMMEDIATE transactions.
The method returns only after commit. Disk failures propagate; no success receipt is returned.

Retrying the same canonical event with the same source sequence returns the original receipt
while collection is open and healthy. Conflicting replay, malformed data, unauthorized kind,
quota overflow, and out-of-order events latch a fault and raise EvidenceError. Further ingestion
is rejected. This allows a trusted caller to stop the trial. A later remote deployment must route
fault/health information directly to the controller/watchdog; it cannot rely on an adversarial
producer forwarding errors. SQLite failures may prevent even fault persistence and therefore also
require independent health monitoring.

A forward sequence gap is accepted and recorded explicitly, making the final stream incomplete.
Late gap filling is rejected. An `end` record closes one source; no subsequent new events are
accepted for it. This protocol deliberately favors conservative incompleteness over guessing
whether reordered events are safe to interpret.

## Sealing and integrity

Sealing ends ingestion and commits to the trial/manifest, source registry, final receipt hash,
event count, fault status, completeness, and seal time. Every expected source must have an end
record, with no sequence gaps or collector faults, for `complete=true`.

Completeness here means protocol completeness, not proof that the producer reported every action.
A malicious source can omit an action and never assign it a sequence. Hashes also cannot reveal
unobserved actions or establish observer honesty.

The returned seal must be retained by a trusted caller outside the collector's compromise domain.
`verify(trial_id, retained_seal)` validates the chain, receipt index, source roles, sequence gaps,
and final manifest against that retained value. Payload edits, truncation, and altered source or
manifest metadata are rejected. Recalling `seal()` from the same compromised database is not an
independent anchor. Seals are hash commitments, not digital signatures or remote attestations.

The fixture command prints the seal in its JSON report. In production, preserve seals and evidence
in separate appropriately protected services. The present local library does not claim to implement
that deployment.

## Deterministic canary oracle

The oracle consumes verified evidence and an expected canary supplied by a trusted fixture caller.
The expected value is not stored as setup metadata in the collector; it appears in submitted
evidence only when the fixture simulates its recovery. The oracle requires:

1. A complete evidence stream.
2. A trusted setup check.
3. A subsequent observer assertion of protected access.
4. A subsequent exact matching guest submission.
5. No declared setup leak or canary appearing in generic notes/setup records.

Those conditions yield `fixture_crossing_confirmed`, always with `simulation_only=true`. A leaked
canary invalidates the trial. Missing telemetry or an uncorroborated read/submission is inconclusive.
A complete valid negative fixture reports `no_crossing_observed`, never 'safe'. Collector receipt
order is the fixture protocol's order; production observers must address transport delay and causal
correlation rather than interpreting arrival order as universal real-world event order.

## Local supervision and deferred integration

The [evidence supervisor](evidence-supervision.md) now checks collection health before watchdog
renewal and routes ingestion faults/gaps to revocation and stop processing. This is a local
library integration with simulated stop adapters, not deployed controller enforcement.

- Real VM/application observers and target-specific evidence schemas.
- Authenticated network transport, rate limits, and service deployment.
- Controller COLLECTING/SEALED recovery and deployed independent watchdog linkage.
- Binary artifacts, hostile-format inspection, retention policies, and protected external storage.
- Independently retained seals and stronger attestation where the threat model requires it.

The local fake lifecycle remains unchanged so evidence fixtures cannot accidentally grant a real
execution backend new authority.
