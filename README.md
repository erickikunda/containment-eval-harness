# Containment evaluation harness

Python orchestration for controlled agent containment experiments on private AWS infrastructure.
The intended deployments are EC2-backed EKS, Fargate application evaluations, and a Fargate
controller with dedicated EC2 experiment workers. Deployment and inference are separate choices.

**Current status: supervised local simulation, AWS preparation, and read-only inspection.**
Local simulation, evidence fixtures, deployment planning, offline asset preflight, and optional
AWS metadata inspection are available. Agent execution, experiment
containers/VMs, AWS provisioning, live network enforcement, model calls, independent watchdogs,
and real escape detection are not implemented. A successful fixture is not evidence of containment or agent capability.
Real backends fail closed.

A local watchdog lease core also provides hard/health deadlines, persistent revocation, and
stop recovery for future supervisors. It is tested with simulated failures; no independent
watchdog service or real stop adapter is deployed.
The local evidence supervisor now gates lease renewal on collection health and routes ingestion
faults to stop processing. New CLI simulations integrate these components and retain evidence
seals before cleanup; production transport and live controller integration remain pending.

## Quick start

Install Python 3.12+ and uv, then:

```sh
uv sync --extra dev --locked
uv run containment validate examples/simulation.json
uv run containment simulate examples/simulation.json
uv run containment list
uv run containment reconcile
uv run containment evidence-fixture positive
uv run containment evidence-fixture leaked
uv run pytest
uv run ruff check .
```

Commands use `.harness/` for local state; select another directory with the global
`--state-dir PATH` option (before the subcommand). `validate` performs static admission only and
does not create state. `simulate` accepts only the fake deployment with replay inference.
`reconcile` stops interrupted simulations without replaying their workload. New trials require
verified, retained evidence seals before cleanup; recovered evidence can be incomplete.
Commands that mutate local state take a nonblocking process lock. Only one local controller may
mutate a given state directory at a time. SQLite and filesystem state must reside on a local disk.

The fake backend writes a small resource marker, never executes the scenario prompt, and uses
the same lifecycle interface that later backends will implement. Its resource markers persist
across controller restarts so recovery can be tested.

`evidence-fixture` exercises a separate local collector and deterministic oracle. Modes are
`positive`, `negative`, `leaked`, `claim_only`, and `gap`. Every verdict is labeled simulation-only.
The command prints a JSON report containing the seal and verdict; retain the seal outside the
collector's trust domain for meaningful later integrity checks. Fixture completion returns exit
code 0 even for expected negative/invalid verdicts; execution errors return 2.

The collector persists authenticated source identities, bounded events, hash-linked receipts,
and sealed manifests in `.harness/evidence.sqlite3`. It is a library, not a deployed network
service. Binary artifact ingestion, TLS identities, protected external storage, and deployed
collector-to-watchdog integration remain future work. [Evidence protocol and limits](docs/evidence-protocol.md).

## Design and progress

- [System design](docs/system-design.md)
- [Implementation slices and acceptance gates](docs/implementation-plan.md)
- [Evidence protocol and deterministic oracle](docs/evidence-protocol.md)
- [AWS deployment preparation and offline image](deployment/README.md)
- [Read-only AWS inspection and IAM policy](docs/aws-inspection.md)
- [Watchdog lease core and integration gates](docs/watchdog.md)
- [Evidence health and watchdog integration](docs/evidence-supervision.md)
- [Supervised simulation lifecycle and recovery](docs/supervised-lifecycle.md)
- [Scenario example](examples/simulation.json)

Dependencies are locked in `uv.lock`. Building and dependency installation may use the internet;
the simulation runtime does not need it. Offline AWS deployments will use prebuilt, pinned images
and prepositioned assets, not runtime package installation.

## Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs on pull requests and pushes to `main`, with an
optional manual trigger. It checks Ruff lint/formatting, runs tests on Linux with Python 3.12,
3.13, and 3.14, builds the package, and smoke-tests the installed wheel in a clean environment
outside the source checkout. Runtime dependencies come from `uv.lock`; build-system dependencies
still follow the constraints in `pyproject.toml`.

An additional Docker job builds the preflight image and runs it offline as a non-root user with a
read-only filesystem. It verifies asset integrity and confirms AWS readiness remains blocked.

The workflow uses commit-pinned actions, a fixed uv version, read-only repository permissions,
and no AWS credentials. It exercises local simulations and fixtures only. These hosted runners
use internet access to install tools and dependencies; they do not verify offline AWS containment.
The aggregate check named **CI** succeeds only when every quality and matrix job passes. It can
be selected as a required check in repository rules; adding this workflow does not itself enforce
branch protection.
