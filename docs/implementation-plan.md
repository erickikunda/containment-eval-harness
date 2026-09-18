# Implementation plan

Work in independently testable slices. Do not enable adversarial agents until the chosen real
backend passes its safety acceptance gates. AWS provisioning is a later slice, not a side effect
of a developer test or CLI smoke test.

| Slice | Scope | Acceptance gate | Status |
| --- | --- | --- | --- |
| 1 | Strict schemas, static capability admission, immutable manifests, SQLite lifecycle, persistent fake resources, simulation CLI, restart recovery | Invalid/incompatible input fails before resource creation; failed stop/cleanup quarantines; interrupted simulations reconcile | Implemented; see tests |
| 2 | Evidence envelopes, collector receipts, bounded event ingestion, sealing, deterministic fixture oracle | Spoofed source rejected; missing telemetry explicit; fixture controls distinguish leaked canary and corroborated synthetic access | Implemented locally; no real escape claims |
| 2a | Pull-request CI for the existing local foundation | Linux tests on Python 3.12–3.14, Ruff checks, package build, installed-wheel smoke test | Implemented in `.github/workflows/ci.yml` |
| 3a | AWS configuration/rendering, tokenless suspended preflight, offline image and asset checks | Invalid profiles rejected; local checks cannot authorize execution; image runs with no network | Implemented locally; see deployment/README.md |
| 3b | Live private AWS foundation, workload identity, infrastructure and runtime verification | Connectivity/identity tests demonstrate required isolation; fresh worker-bound evidence; independent shutdown | Read-only metadata inspection implemented; infrastructure and live isolation verification pending selected topology |
| 3c | Local watchdog lease core and durable stop recovery | Nonextendable hard deadline; revoked leases cannot revive; restart stops; unknown termination remains quarantined | Implemented as a library with simulated failure tests; deployment and independent shutdown pending |
| 3d | Local evidence health and watchdog bridge | Collector faults, gaps and write failures revoke; only healthy bound collections permit renewal; recovery cannot revive leases | Implemented as a library with local integration tests; deployed transport/scheduling and controller lifecycle integration pending |
| 4 | Fargate application backend and external controller | Application scenarios only; unknown termination stays unresolved; VM scenarios rejected | Planned |
| 5 | EC2 VM backend, guest transport, watchdog and infrastructure termination | Actual VM isolation, deadline, outage, stop verification and reset tests pass on dedicated workers | Planned |
| 6 | Replay/local/private inference interfaces, budgets, agent runner | Atomic reservations, expiry, output bounds, ambiguous timeout handling, no host command execution | Planned |
| 7 | Scenario suite and reports | Directed/ordinary/validation tracks separate; repeated results include validity and evidence | Planned |
| 8 | Multiple workers and trials | Cross-trial isolation, orphan discovery and concurrency failure campaign pass | Planned |

## Slice 1 boundaries

The fake backend only creates/removes a JSON marker. It cannot execute code or access AWS.
Static capability declarations express profile compatibility and are not live probes. Production
profiles are deliberately unavailable to the CLI executor. Fake execution has simulation-only
outcomes and does not claim resource-limit enforcement or safety verification.

SQLite uses short transactional state/event writes. The local controller uses a nonblocking
process lock to serialize mutation. State and markers are persisted on local disk. Recovery
stops interrupted trials and verifies cleanup rather than rerunning them. An unparseable marker
or failed cleanup quarantines the trial for inspection. This is a local development implementation;
distributed coordination and tamper-resistant evidence are later work.

## Slice 2 boundaries

Local SQLite collector transactions bind bearer credentials to a fixed trial/source/role registry.
Acknowledgments follow commits; quotas and retries serialize transactionally. Faults latch and
block further ingestion. Seals commit to receipt hashes and source identities; verification needs
an independently retained seal. This is tamper detection under that assumption, not immutable
storage or protection against a database owner rewriting history before a seal is retained.

Five deterministic fixtures exercise positive, negative, leaked, unsupported claim, and missing
telemetry cases. They simulate observer assertions, never perform an actual unauthorized read.
The existing simulation lifecycle is unchanged; the evidence CLI is a separate fixture path.
Slice 3d connects local collector health and ingestion failures to watchdog renewal/revocation.
Before real execution, integrate that bridge into the deployed controller, introduce
COLLECTING/SEALED lifecycle states with restart recovery, and deploy the collector outside the
experiment compromise domain. Network authentication, binary artifact ingestion, rate limiting,
and independent seal retention are still deployment/integration work. See evidence-protocol.md.

## Future deployment layout

While live topology selection is pending, slice 3c implements the reusable watchdog lease core.
It does not satisfy the live termination acceptance gate or enable a real backend. See
[watchdog contract and integration gates](watchdog.md) for timing, persistence, and recovery limits.

```text
deployment/base/{controller,evidence-collector,inference-gateway,rbac}.yaml
deployment/overlays/eks-ec2-vm/
deployment/overlays/eks-fargate-app/
deployment/overlays/fargate-controller-ec2-vm/
deployment/aws/{networking,private-endpoints,experiment-workers,identities}/
```

Choose the smallest permissions required by each profile. Do not copy permissive EC2 worker
settings into Fargate application pods. Keep real deployment manifests out of the execution path
until independently checked on the selected AWS topology.

## Libraries

Now: Python 3.12+, Pydantic v2, standard-library sqlite3/argparse, pytest, Ruff, uv lockfile.
Later: official Kubernetes client and boto3 in trusted orchestration; libvirt-python on EC2 VM
workers; HTTPX for bounded gateway requests; Hypothesis for generated state-machine sequences.
Consider SQLAlchemy/PostgreSQL when multi-controller transactional coordination becomes necessary.
Inspect can be an optional adapter; custom tool execution must still use the containment backend.

## Failure campaign

- Controller dies at every lifecycle transition: no workload replay on recovery.
- Duplicate prepare/stop/destroy: no duplicate execution or unrelated resource deletion.
- Unknown stop status: quarantine, no resource cleanup that could erase incident evidence.
- Evidence collector outage or overflow: stop, mark incompleteness.
- Guest floods output or changes clock: outer budgets/deadlines remain effective.
- Ambiguous model timeout: reserved budget retained; no blind retry.
- Stale/forged probe, changed worker, policy drift: admission revoked.
- DNS/IPv6/proxy path, metadata and credential endpoint access: denied by actual controls.
- Another trial's identity: rejected by inference, transport, collector, and lifecycle APIs.
- Cleanup interruption or orphan resource: discovered; worker not reused until resolved.
- Canary in setup logs: invalid trial, not confirmed crossing.

## Operational gates

Unit tests run on development machines. Real containment tests run only on dedicated Linux AWS
workers. No intentionally vulnerable workloads on general shared CI runners. Infrastructure stop
must be independently verifiable; absence from Kubernetes alone is insufficient. Snapshot reset
does not repair a compromised worker. Suspected outer failure requires isolation and rebuild.
