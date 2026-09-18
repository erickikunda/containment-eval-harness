# System design

## Objective and scope

Measure whether and how an agent crosses a specified experimental boundary while independently
enforcing containment and resource limits under a documented threat model. No sandbox stack
provides an unconditional guarantee of harmlessness. Report assumptions, evidence gaps, and
residual risk explicitly.

An intentionally vulnerable container is the arena. Its intended escape destination is a
synthetic guest host inside a disposable VM. The VM boundary is not intentionally weakened.
Hypervisor-escape experiments are outside the initial scope because they require a different
outer containment design. The agent may discover all layers; secrecy is not a safety control.

## Deployment profiles

| Profile | Experimental environment | Scope |
| --- | --- | --- |
| `fake` | Local resource marker; no execution | Harness development only |
| `eks_ec2_vm` | Arena container inside a disposable VM on a dedicated EC2 worker | Container-to-guest, tool authorization, synthetic network |
| `eks_fargate_app` | Unprivileged application-target pod | Application/tool authorization; no container-to-host escape testing |
| `fargate_controller_ec2_vm` | Trusted controller on Fargate, VM experiments on dedicated EC2 workers | Same target boundaries as EC2 VM profile |

The controller's location does not determine the experiment's isolation. Fargate does not support
privileged containers or DaemonSets; do not assume access to KVM or a node watchdog. EC2 nested
virtualization requires a supported instance and configuration, verified on the actual worker.
Never silently downgrade a VM scenario to ordinary pod execution.

```text
Trusted control environment
  scheduler / state database / external watchdog
  evidence collector / adjudicator / inference gateway
              | narrow authenticated interfaces
Dedicated experiment worker (separate compromise domain)
  supervisor / hypervisor / externally enforced resource and network policy
  disposable experiment VM
    synthetic guest host and services
    container arena: scaffold, tools, task data
```

The controller, authoritative evidence, and independent watchdog must be outside the experiment
pod. For possible node compromise they must also be outside its worker. Sidecars are not an
independent safety boundary. Worker compromise triggers quarantine and infrastructure isolation.

## Threat model and admission

Each scenario identifies initial authority, boundary under test, permitted target outcome,
protected assets, required capabilities, budget, and stop conditions. Distinguish container root,
guest root, and EC2 worker control. Directed escape capability and ordinary-task behavior are
separate evaluation tracks; validation fixtures are not scored agent escapes.

Scenario configuration refers to operator-controlled image, tool, network, prompt, and oracle
profiles. It never chooses arbitrary host paths, VM XML, Python imports, or management commands.
Safety policy provides hard ceilings and allowed profiles. Resolve and hash an immutable manifest
before provisioning. Admission derives minimum capabilities from the boundary itself so authors
cannot remove critical requirements by editing a scenario's declared requirements.

Static admission establishes compatibility, not safety. Before a real run, trusted probes must
verify live capabilities, identify the worker and policy digest, and expire results after a bounded
interval or configuration change. Guest declarations cannot provide trustworthy probe results.

## Independent interfaces

- Scheduler: place workloads and track provider identities.
- Containment backend: prepare, verify, terminate, confirm stop, destroy, confirm cleanup.
- Guest transport: bounded tool commands and results, with execution IDs and deadlines.
- Inference backend: local, private gateway, or replay.
- Evidence store: acknowledge durable event/artifact receipt and seal manifests.
- Adjudicator: produce deterministic, evidence-linked outcomes.

Backend APIs accept trusted manifests and opaque resource identities. Agent-generated commands
only enter the guest transport. Do not interpolate them into host shell commands. A timed-out
execution may already have run; never blindly retry non-idempotent commands.

## Offline operation and inference

`private_services` means no public internet, with explicit internal service allowances.
`disconnected` means no experiment network connections. A disconnected profile cannot claim live
external telemetry without a separately specified and independently controlled non-network channel.
Reject incompatible requirements; local buffered logs are not equivalent to external evidence.

Local inference requires prepositioned weights, tokenizer, runtime, and measured compute/memory
capacity. Private inference uses a fixed internal gateway; a separate EC2 GPU service is an option.
Replay is for harness development only. If inference shares the experiment's compromise domain,
its state is untrusted. Keep authoritative scoring and credentials elsewhere.

The gateway fixes provider destinations/models, disables redirects, bounds input/output/time and
concurrency, reserves token/cost budget atomically, and revokes trial access on stop. Treat ambiguous
provider timeouts as potentially billable. Guest-visible credentials authorize only that trial's
limited inference allocation. No general HTTP proxy or lifecycle credentials are exposed.

Build dependencies, images, models, and scenario assets before deployment. Pin image digests and
package versions. Private AWS endpoints are real service access: constrain endpoint policies,
IAM actions, and resources. Infrastructure image-pull access does not authorize application access.

## AWS network and identity boundaries

Use a dedicated experiment environment without production routes or unrelated workloads. Restrict
experiment traffic to synthetic services and named gateway/collector ingress. Deny access to
management APIs, kubelet/node services, metadata and credential endpoints, unrelated pods, and
production networks. Cover IPv4, IPv6, DNS, forwarding, and indirect proxy paths. Enforce controls
outside the compromise domain and test them; a NetworkPolicy object alone is not evidence.

Only trusted controllers receive narrowly scoped Kubernetes/AWS lifecycle identities. Experiment
pods have no management role or service-account token. Resource identity, trial association, and
authorization are established by the receiving service, not by untrusted payload labels.

## Lifecycle, recovery, and termination

Target production lifecycle:

```text
CREATED -> PREPARING -> VERIFYING -> RUNNING -> STOPPING
        -> COLLECTING -> SEALED -> CLEANING -> COMPLETE
Unverified stop/cleanup or suspected outer breach -> QUARANTINED
```

Persist desired transitions before external actions. Tag resources by immutable trial identity;
make lifecycle actions idempotent and reconcile persisted state against provider state after a
restart. Discover orphaned resources even when their database row is missing. Never resume an
interrupted workload automatically. Cleanup success requires independently checking resource absence.
Do not destroy forensic evidence on a suspected outer breach; isolate and retain the affected worker.

The independent watchdog has a hard nonextendable runtime and a short health lease renewed only
by the trusted controller. Use monotonic local deadlines. Evidence loss, lease expiry, budget
exhaustion, or critical health failure stops a run. Unknown stop status quarantines the worker.
Deleting a Kubernetes object is not proof of termination. EC2 workers need independent instance
termination authority; Fargate scenarios must fit the termination guarantees available there.

New CLI simulations implement COLLECTING/SEALED with local evidence retention and verification.
Legacy trial recovery preserves its original path. These SQLite journals are local development
metadata, not protected evidence storage or independent containment. See supervised-lifecycle.md.

## Evidence and oracles

Store authoritative evidence outside the experiment. Separate producer claims from collector
receipts: source identity, collector sequence, receipt time, payload digest, and durable ack.
Record missing events, truncation, and telemetry coverage. Hash chains detect some alteration,
not omitted events. Guest telemetry becomes forgeable after guest compromise.

Artifacts receive server-generated names and size limits. Do not execute or automatically extract
them on a control/analyst host. Use disposable analysis environments for filesystems and complex
formats. Reports escape untrusted content and disable active content. Logs never instruct tools.

Initial oracle: fresh high-entropy canary outside the arena in the synthetic guest host. Expected
values remain with the trusted adjudicator. Verify no setup/prompt/log leak and test negative and
positive controls. A matching submitted value supports protected-information access, not arbitrary
root execution. Callback reachability may be an allowed proxy path; state mutation may be harness
activity. Validate causality before confirming a crossing.

Outcome fields are independent: target outcome, boundary, observed capability, trial validity,
evidence completeness, safety status, cleanup status, and supporting evidence IDs. Use 'no breach
observed', never 'proven safe'. Outer-boundary suspicion is an incident, not an ordinary success.

## Evaluation and reproducibility

Record model/provider version where available, scaffold commit, prompts, generation settings,
budgets, image digests, kernel/runtime versions, manifest digest, and deployment profile. Repeat
trials and report refusals, invalid trials, budget exhaustion, success rates, and uncertainty.
Keep intentionally weakened fixtures separate from hardened scenarios. Seeds do not guarantee
deterministic model behavior. Replay verifies orchestration, not live agent capability.

## References

- [AWS nested virtualization](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html)
- [EKS Fargate constraints](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)
- [Private EKS dependencies](https://docs.aws.amazon.com/eks/latest/userguide/private-clusters.html)
- [ECR private endpoints](https://docs.aws.amazon.com/AmazonECR/latest/userguide/vpc-endpoints.html)
- [EKS network security](https://docs.aws.amazon.com/eks/latest/best-practices/network-security.html)
- [Libvirt Python bindings](https://libvirt.org/python.html)
- [Firecracker production host guidance](https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md)
- [gVisor security model](https://gvisor.dev/docs/architecture_guide/security/)
- [Inspect sandbox execution model](https://inspect.aisi.org.uk/sandboxing.html)
- [Inspect sandbox extensions](https://inspect.aisi.org.uk/extensions-sandboxes.html)
- [METR evaluation protocol](https://evaluations.metr.org/faq/)
