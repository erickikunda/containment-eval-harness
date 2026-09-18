# Local Docker simulation lab

Docker provides a Linux integration environment while AWS provisioning is pending. This workflow
tests the installed package, persistent journals, evidence sealing, and controller crash recovery.
It uses the existing fake backend: it does not run agents, model calls, exploits, nested containers,
or real containment experiments. The controller, collector, and watchdog still share one process
and compromise domain. Docker success is not evidence of AWS isolation or independent termination.

## One-command verification

From the repository root, with Docker running, Python/uv installed, and build connectivity:

```sh
uv sync --locked --extra dev
sh scripts/check-docker-simulation.sh
```

The script builds the pinned preflight image, runs the existing offline preflight check, then:

1. Creates a dedicated temporary Docker volume and completes a supervised simulation.
2. Lists the persisted trial from a different container.
3. Starts a test-only fake backend that sends SIGKILL to its own container process immediately
   after writing a running marker. The expected container exit code is 137.
4. Uses a fresh container to reconcile the same volume, without replaying the workload.
5. Runs reconciliation again and checks that it has no further work.
6. Runs a bounded replay/tool loop, then crashes a second replay after tool execution but before
   result settlement and recovers it without retrying the uncertain action.
7. Verifies all four retained evidence seals, resource cleanup, and explicit incomplete evidence for
   the interrupted trial. The interrupted record must have no replayed synthetic events.

Each container runs as UID/GID 10001 with no network, a read-only root filesystem, all capabilities
dropped, no-new-privileges, and CPU/memory/PID limits. Docker's init process supervises the controller
child so signal exit status is preserved. Only the dedicated state volume is writable; the examples
and test probe are mounted read-only. No Docker socket or AWS credentials are mounted.
The runtime probe checks UID, active network interfaces, effective capabilities, no-new-privileges,
and rejection of a root-filesystem write. Inactive kernel tunnel interfaces are allowed.

Local reports are retained in the printed `.harness/docker-lab.*` directory; CI runner files are
ephemeral and this workflow does not upload them as artifacts. The temporary volume is
removed only after all assertions pass. On failure it is retained and its name is printed for
investigation. Preserve it until understood; this test does not prune unrelated Docker resources.
The local image tag is `containment-preflight:check`. The script builds/downloads dependencies
while connected; the test containers run without network access.

The crash probe is a read-only test script, not a new public harness command or a shipped package
feature. It kills only its own container process. A Docker outage or interrupted test may require
operator inspection of the retained volume and Docker processes.

## Interactive workflow with persistent state

Build once using the preflight check:

```sh
sh scripts/check-offline-image.sh
docker volume create containment-local-state
```

Define this helper in the same shell, while in the repository root:

```sh
harness_local() {
  docker run --rm --init --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
    --mount type=volume,src=containment-local-state,dst=/var/lib/containment \
    --mount "type=bind,src=$PWD/examples,dst=/examples,readonly" \
    containment-preflight:check --state-dir /var/lib/containment "$@"
}

harness_local validate /examples/simulation.json
harness_local simulate /examples/simulation.json
harness_local list
harness_local reconcile
harness_local evidence-fixture positive
harness_local evidence-fixture leaked
harness_local replay /examples/replay-scenario.json /examples/replay-script.json
```

The image prepares the state directory for UID 10001; a new Docker volume inherits that directory's
initial contents/ownership. If reusing a differently initialized volume causes permission errors,
inspect it rather than switching the controller to root. Use one controller at a time. Preserve the
mount target `/var/lib/containment` across invocations because marker paths are bound into leases.
The named volume survives removal of each container and is never deleted by this helper.

Use shell redirection to export JSON reports. Quarantined records remain quarantined, and an empty
`reconcile` result does not clear them. Preserve the volume and outputs when investigating a failure.
There is no supported release-quarantine or resume command. Refer to
[lifecycle recovery](../docs/supervised-lifecycle.md) for evidence completeness and stop semantics.

## What this lets us develop next

This lab supports repeatable Linux integration tests, operator demonstrations, additional failure
injection, and the [bounded replay runner](../docs/replay-runner.md) before cloud resources exist.
Future local model or service integration needs its own implementation and resource/network design;
this change does not supply those features.

Dedicated EC2/Fargate execution adapters, actual network and identity enforcement, externally
protected evidence, independent watchdog deployment, and verified infrastructure termination still
need AWS implementation and live validation. Do not use this development workflow to run a real
agent attempting host/container escape on a workstation.

CI runs this same script on Linux amd64. Local Docker Desktop runs may use another architecture;
neither substitutes for tests on the intended AWS worker type.

Docker option and volume semantics: [Docker's container run reference](https://docs.docker.com/engine/containers/run/).
