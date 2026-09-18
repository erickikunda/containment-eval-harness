# Containment evaluation harness

Python orchestration for controlled agent containment experiments on private AWS infrastructure.
The intended deployments are EC2-backed EKS, Fargate application evaluations, and a Fargate
controller with dedicated EC2 experiment workers. Deployment and inference are separate choices.

**Current status: slice 1, local simulation only.** No agent code, containers, VMs, AWS resources,
network rules, model calls, independent watchdogs, or escape detection are implemented. A successful
simulation is not evidence of containment or agent capability. Real backends fail closed.

## Quick start

Install Python 3.12+ and uv, then:

```sh
uv sync --extra dev --locked
uv run containment validate examples/simulation.json
uv run containment simulate examples/simulation.json
uv run containment list
uv run containment reconcile
uv run pytest
uv run ruff check .
```

Commands use `.harness/` for local state; select another directory with the global
`--state-dir PATH` option (before the subcommand). `validate` performs static admission only and
does not create state. `simulate` accepts only the fake deployment with replay inference.
`reconcile` terminates and cleans up interrupted simulations without replaying their workload.
Commands that mutate local state take a nonblocking process lock. Only one local controller may
mutate a given state directory at a time. SQLite and filesystem state must reside on a local disk.

The fake backend writes a small resource marker, never executes the scenario prompt, and uses
the same lifecycle interface that later backends will implement. Its resource markers persist
across controller restarts so recovery can be tested.

## Design and progress

- [System design](docs/system-design.md)
- [Implementation slices and acceptance gates](docs/implementation-plan.md)
- [Scenario example](examples/simulation.json)

Dependencies are locked in `uv.lock`. Building and dependency installation may use the internet;
the simulation runtime does not need it. Offline AWS deployments will use prebuilt, pinned images
and prepositioned assets, not runtime package installation.
