# Read-only AWS metadata inspection

This increment of slice 3b checks infrastructure metadata for all three deployment profiles.
It never provisions resources, executes an agent, or admits an experiment. Every inspection
returns `readiness: blocked`, `execution_authorized: false`, and all required live checks as
unverified, even when every metadata finding passes. Unit tests use SDK stubs and dummy
credentials; this implementation has not been validated against a deployed AWS environment.

## Prepare the inputs

Copy a deployment example, the asset manifest, and
`deployment/examples/inspection-target.json` to operator-controlled files. Replace the placeholder
account, region, cluster, VPC, subnet IDs, security groups, placement name, and image digest with
the intended deployment. The target subnet set must exactly match the selected Fargate profile
or managed EC2 nodegroup. Hybrid inspection checks the EC2 worker placement.

Render a permissions policy for review without contacting AWS:

```sh
uv run containment aws-inspection-policy deployment.json inspection-target.json > inspection-policy.json
```

The policy scopes EKS and ECR reads to the selected resources. EC2 Describe actions require
wildcard resources and are region-conditioned; collection additionally filters by VPC or exact
IDs. STS identity discovery uses a wildcard resource. The command does not create a role, attach
a policy, or establish a trust relationship. Review this alongside the caller's existing policies;
the inspector does not determine effective IAM permissions.

## Explicit live inspection

Install the optional SDK during connected preparation:

```sh
uv sync --locked --extra aws
uv run containment aws-inspect deployment.json assets.json inspection-target.json --live --aws-profile inspection --save-snapshot observation.json
```

Use an existing operator-approved AWS credential source. Omit `--aws-profile` to use the SDK's
default credential chain. Do not give inspection credentials to an experiment. A trusted private
runner needs access to STS, EKS service API, EC2, and ECR API endpoints and its credential provider;
private Kubernetes API access alone is insufficient. The default offline preflight image excludes
the AWS SDK and does not run this command. Bake dependencies into a separate inspector image
before moving it into a disconnected environment.

Collection first checks the caller account and stops further reads if it differs from the
configuration. Fixed Describe/Get operations collect placement, subnets, route tables, endpoints,
security groups, and the pinned ECR image. SDK endpoint URL overrides are ignored. Requests use
bounded retries and timeouts; route/endpoint inventories stop after 20 pages of up to 100 rows.
Missing permissions, incomplete pagination, or collection failures leave affected findings
unverified and discard partial inventories. Error reports retain AWS error codes or exception
types, not SDK error messages.

`--save-snapshot` creates a new file exclusively and refuses to overwrite an existing path.
Snapshots contain infrastructure metadata and caller identity; store them with appropriate access
controls. They contain no exported credentials. No local trial database is created.

## Offline assessment

The default installation can assess an exported snapshot without importing the AWS SDK:

```sh
uv run containment aws-inspect deployment.json assets.json inspection-target.json --snapshot observation.json
```

Snapshot inputs are limited to 8 MiB. A digest binds the deployment, asset manifest, and inspection
target; observations must be no more than five minutes old and cannot be future-dated. Stale or
mismatched snapshots are reported without assessing their metadata. These checks detect input
mix-ups, not forgery: JSON snapshots are unsigned and are not independent attestations. Editing a
timestamp or digest cannot confer execution authority. The report identifies offline input as
`source: snapshot`; only the live CLI path reports `source: live_aws`.

Both successful inspection paths return exit code **1** because execution remains blocked.
Input and setup errors return **2**. Inspect individual finding statuses (`pass`, `fail`, or
`unverified`) rather than interpreting exit 1 as a collection failure.

## Meaning and limits of findings

- Cluster metadata must identify the expected active cluster with private-only API access.
- Placement must be active and match the cluster, exact subnet set, and applicable selectors.
  The Fargate execution-role account reference is checked; trust and permissions are not.
- Subnets must belong to the intended account/VPC and disable automatic public IPv4 assignment.
- Effective route tables use explicit subnet associations before the VPC main table. Ambiguous
  or changing associations are unverified. The conservative route policy permits only local
  routes and available regional S3 gateway endpoints associated with the effective tables.
  NAT, internet gateway, peering, transit gateway, and ENI routes fail this policy.
- ECR interface endpoints must advertise private DNS; an S3 gateway endpoint must associate with
  every effective route table. Endpoint policies, DNS resolution, and connectivity are unverified.
- Configured security groups must match the account/VPC and contain no literal `0.0.0.0/0` or
  `::/0` rule. This does not analyze narrower CIDRs, prefix lists, peer groups, or combined rules.
- The configured image digest must exist in the expected ECR repository. Its presence does not
  verify provenance, architecture, image contents, or the actual running image.

Metadata is a point-in-time observation, not traffic enforcement. Actual pod placement and ENI
attachments, IAM/RBAC, metadata access, DNS, IPv6, CNI behavior, cross-trial isolation, model
availability, independent evidence, and termination still require live verification. No finding
is promoted into the execution admission gate by this slice.

## Primary references

- [Private EKS cluster requirements](https://docs.aws.amazon.com/eks/latest/userguide/private-clusters.html)
- [Fargate profiles and selectors](https://docs.aws.amazon.com/eks/latest/userguide/fargate-profile.html)
- [EC2 route table API](https://docs.aws.amazon.com/boto3/latest/reference/services/ec2/client/describe_route_tables.html)
- [VPC endpoint API](https://docs.aws.amazon.com/boto3/latest/reference/services/ec2/client/describe_vpc_endpoints.html)
- [SDK client configuration](https://docs.aws.amazon.com/botocore/latest/reference/config.html)
