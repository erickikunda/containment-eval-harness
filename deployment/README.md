# Private AWS foundation: local preparation

For a Linux simulation/recovery environment while AWS is pending, use the
[local Docker lab](docker-local.md). It extends the preflight image with persistent simulation
state and a repeatable container-crash test; it does not deploy an experiment backend.

This directory supports **preparation**, not AWS provisioning or experiment admission. The example
account, security group, nodegroup/profile names, and ECR digest are placeholders. Planning and
preflight run locally. The separate [AWS inspector](../docs/aws-inspection.md) contacts AWS only
with explicit `--live`. No command applies manifests, pushes images, or grants execution authority.

## Profiles

| Configuration | Preflight placement | Later experiment scope |
| --- | --- | --- |
| `examples/eks-ec2-vm.json` | Dedicated EC2 nodegroup | Container arena in a disposable guest VM |
| `examples/eks-fargate-app.json` | Selected Fargate profile | Application/tool boundaries only |
| `examples/fargate-controller-ec2-vm.json` | Dedicated EC2 nodegroup | VM worker with separate Fargate controller |

The hybrid plan renders the **worker preflight**, not the Fargate controller. All profiles keep
trusted control and experiment namespaces distinct, but namespaces alone are not independent
containment. Real experiments also require separate nodes/compromise domains and infrastructure
enforcement. The preflight namespace uses restricted Pod Security Admission and is unsuitable for
a future privileged VM launcher; do not weaken it to make a VM worker fit. That worker needs its
own reviewed placement and privilege design.

## Inspect a plan

```sh
uv run containment deployment-plan deployment/examples/eks-fargate-app.json deployment/examples/assets.json
uv run containment preflight deployment/examples/eks-fargate-app.json deployment/examples/assets.json --asset-root deployment/examples/assets
```

`deployment-plan` returns a JSON report with a configuration digest, required live checks, and a
Kubernetes `List` under `manifests`. It returns 0 for a valid static plan. `preflight` checks local
asset sizes/hashes and process/runtime facts, then returns **1** because live AWS checks remain
unverified. Invalid configuration returns 2. Neither command creates local trial state. There is
no flag to turn a local probe result into execution authorization.

The report is intentionally not directly consumable by `kubectl apply`. Its embedded resources
are an operator-reviewable preview: dedicated Namespace, tokenless ServiceAccount, immutable
ConfigMap, ResourceQuota, SecurityGroupPolicy, and a **suspended** Job. EC2 profiles also include
a deny-all NetworkPolicy for the preflight pod. The job is non-root, has a read-only root filesystem,
drops capabilities, has no host mounts/namespaces, grants no RBAC/IAM permissions, and runs only
the read-only preflight command. No experiment or arbitrary agent command is rendered.

Resuming the job after infrastructure setup would run preflight, which still exits blocked until
a future live-verification implementation exists. A Kubernetes deadline is not an independent
termination guarantee. Export logs before the Job TTL removes completed/failed pods. The ConfigMap
is immutable; use a new deployment ID for a changed plan rather than mutating an existing run.
Use a dedicated namespace per plan because the quota intentionally admits only one Job.

## Network and identity assumptions that must be checked live

AWS VPC CNI network policies do **not** apply to Fargate. The Fargate preview therefore does not
include an ineffective NetworkPolicy; it selects operator-supplied pod security groups instead.
The renderer does not create those groups or validate their actual rules, ENI attachment, routing,
endpoint policies, or enforcement mode. Even the EC2 NetworkPolicy requires compatible/enabled
enforcement and real positive/negative traffic checks. Other policies and additional interfaces
can affect the result. A rendered rule is not evidence of isolation.

Before any experiment, independently verify:

- Private cluster access, private subnets, routes, and no unintended public/production access.
- Private ECR API/registry and S3 connectivity for infrastructure image pulls. Add other private
  endpoints only for infrastructure components that need them; avoid blanket application access.
- Actual pod security groups, CNI behavior, DNS, both address families and additional interfaces.
- Denial of metadata/credential endpoints, management APIs, node services, and other trials.
- Dedicated placement and external evidence/watchdog services outside the experiment boundary.
- Confirmable termination and cleanup through an independent infrastructure path.
- EC2: supported virtualization hardware/configuration, guest isolation, and node isolation.
- Fargate: correct profile selection and a narrowly scoped pod execution role. That role is used
  by Fargate infrastructure; it does not grant its permissions to the application container.
- Inference: local model capacity/loading or a narrow private gateway with trial budgets.

The preflight ServiceAccount has token automount disabled and no role bindings or IRSA annotation.
This does not by itself prove the absence of inherited credentials, admission-time injection, or
node metadata access. Those are explicit unresolved checks. Controller IAM/RBAC will be defined
with the real backend operations instead of adding broad provisional permissions.

Initial configuration supports commercial AWS ECR registry names, managed EC2 nodegroup selectors,
and Fargate profile selectors. It does not assert support for GovCloud/China, EKS Auto Mode, or
arbitrary CNI/runtime combinations. EKS Fargate images must target its supported architecture;
CI tests the runtime image on Linux amd64. Local arm64 testing is not Fargate compatibility proof.

## Prepositioned assets and offline image

`assets.json` lists relative paths, roles, exact byte sizes, and SHA-256 hashes. The verifier rejects
absolute/traversal paths, duplicate paths, symlinks in any component, nonregular files, changed files,
size mismatch, and hash mismatch. Only declared files are verified; it is not a complete image or
filesystem inventory. The trusted manifest must be reviewed and retained separately. Keep assets
immutable after verification; a hash observation is not protection against later mutation.

Local inference additionally requires declared `model_weights` and `tokenizer` assets. This checks
presence/integrity, not that a model fits or can be loaded without fetching additional files.

The Dockerfile consumes a small prepared build context:

```text
requirements-runtime.txt   # exported from uv.lock, including hashes
dist/*.whl                 # exactly one harness wheel built from the reviewed source
assets/                    # files at paths declared in assets.json
```

Its connected builder downloads hash-locked wheels for the target Linux platform. The runtime
stage installs them with `--network=none` and `--no-index`, copies the assets, and runs as UID 10001.
The base Python image is pinned to a multi-platform digest. Building needs registry/package access;
running preflight does not. A future release job must publish the reviewed image to private ECR,
record its actual digest, and verify provenance. The example digest is not a published image.

Build and exercise the example locally (requires Docker and uv):

```sh
sh scripts/check-offline-image.sh
```

The script builds a local image and runs it with networking disabled, read-only root filesystem,
dropped capabilities, no new privileges, and CPU/memory/PID limits. It asserts successful asset
checks and blocked AWS readiness. It never pushes or deploys. CI runs the same check on Linux.
The script uses a fresh temporary build context and retains the JSON report in
`.harness/last-image-preflight.json`.

## Remaining slice 3 work

Slice **3a** provides config, rendering, image preparation, and local probes. The first part of
**3b** adds read-only AWS metadata checks and an IAM policy template. The remaining work needs
the chosen AWS account/region/cluster and reviewed network topology, infrastructure definitions,
real connectivity/identity tests, fresh worker-bound probe receipts, and external watchdog integration. No real backend is enabled by this change. Do not treat the
preflight's local JSON as a signed or independent attestation.

## Primary references

- [EKS network policy support and limitations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)
- [Security groups for pods](https://docs.aws.amazon.com/eks/latest/userguide/security-groups-for-pods.html)
- [SecurityGroupPolicy example](https://docs.aws.amazon.com/eks/latest/userguide/sg-pods-example-deployment.html)
- [Fargate constraints](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)
- [Fargate execution role](https://docs.aws.amazon.com/eks/latest/userguide/pod-execution-role.html)
- [Private EKS requirements](https://docs.aws.amazon.com/eks/latest/userguide/private-clusters.html)
- [Suspended Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
