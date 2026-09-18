"""Assessment of AWS metadata. Findings never grant experiment execution authority."""

import fnmatch
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from containment.deployment import (
    AssetManifest,
    DeploymentConfig,
    configuration_digest,
    required_live_checks,
)
from containment.models import Deployment, StrictModel

SubnetId = Annotated[str, Field(pattern=r"^subnet-([0-9a-f]{8}|[0-9a-f]{17})$")]


class InspectionTarget(StrictModel):
    schema_version: Literal[1]
    cluster_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
    vpc_id: str = Field(pattern=r"^vpc-([0-9a-f]{8}|[0-9a-f]{17})$")
    subnet_ids: tuple[SubnetId, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_subnets(self) -> "InspectionTarget":
        if len(set(self.subnet_ids)) != len(self.subnet_ids):
            raise ValueError("Duplicate target subnet IDs")
        return self


class AwsSnapshot(StrictModel):
    schema_version: Literal[1] = 1
    inspection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    collected_at: AwareDatetime
    responses: dict[str, dict[str, JsonValue]]
    errors: dict[str, str]


def inspection_digest(
    config: DeploymentConfig, assets: AssetManifest, target: InspectionTarget
) -> str:
    body = {
        "configuration_digest": configuration_digest(config, assets),
        "target": target.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _placement_matches(config: DeploymentConfig, target: InspectionTarget, placement: dict) -> bool:
    if placement["status"] != "ACTIVE" or placement["clusterName"] != target.cluster_name:
        return False
    if set(placement["subnets"]) != set(target.subnet_ids):
        return False
    if config.profile != Deployment.FARGATE_APP:
        return placement["nodegroupName"] == config.nodegroup
    labels = {
        "app.kubernetes.io/name": "containment-preflight",
        "containment.eval/deployment": config.deployment_id,
        "eks.amazonaws.com/fargate-profile": config.fargate_profile,
    }
    matches = any(
        fnmatch.fnmatchcase(config.experiment_namespace, selector["namespace"])
        and all(
            any(
                fnmatch.fnmatchcase(key, pattern) and fnmatch.fnmatchcase(value, expected)
                for key, value in labels.items()
            )
            for pattern, expected in selector.get("labels", {}).items()
        )
        for selector in placement["selectors"]
    )
    return (
        placement["fargateProfileName"] == config.fargate_profile
        and matches
        and placement["podExecutionRoleArn"].startswith(f"arn:aws:iam::{config.account_id}:role/")
    )


def _effective_tables(target: InspectionTarget, tables: list[dict]) -> list[dict]:
    tables = [table for table in tables if table["VpcId"] == target.vpc_id]
    selected = []
    for subnet in target.subnet_ids:
        explicit = [
            table
            for table in tables
            if any(
                assoc.get("SubnetId") == subnet
                and assoc.get("AssociationState", {}).get("State") == "associated"
                for assoc in table["Associations"]
            )
        ]
        # An explicit association being changed must not fall back to a seemingly safe main table.
        pending = any(
            assoc.get("SubnetId") == subnet
            and assoc.get("AssociationState", {}).get("State") != "associated"
            for table in tables
            for assoc in table["Associations"]
        )
        main = [
            table
            for table in tables
            if any(
                assoc.get("Main") is True
                and assoc.get("AssociationState", {}).get("State") == "associated"
                for assoc in table["Associations"]
            )
        ]
        candidates = explicit or main
        if pending or len(candidates) != 1:
            raise ValueError("Effective route table is ambiguous or unavailable")
        selected.append(candidates[0])
    return selected


def _routes_restricted(config, target, tables, endpoints) -> bool:
    selected = _effective_tables(target, tables)
    for table in selected:
        if not table["Routes"]:
            raise ValueError("Route inventory is empty")
        s3 = {
            endpoint["VpcEndpointId"]
            for endpoint in endpoints
            if endpoint["VpcId"] == target.vpc_id
            and endpoint["State"] == "available"
            and endpoint["VpcEndpointType"] == "Gateway"
            and endpoint["ServiceName"] == f"com.amazonaws.{config.region}.s3"
            and table["RouteTableId"] in endpoint["RouteTableIds"]
        }
        for route in table["Routes"]:
            if route["State"] != "active":
                return False
            targets = {
                key: value
                for key, value in route.items()
                if key.endswith("Id") and key not in {"DestinationPrefixListId"}
            }
            if targets == {"GatewayId": "local"}:
                continue
            if (
                set(targets) == {"GatewayId"}
                and targets["GatewayId"] in s3
                and route.get("DestinationPrefixListId")
            ):
                continue
            return False  # Includes NAT/IGW, peering, TGW, ENI, IPv6 and unknown targets.
    return True


def _private_endpoints_present(config, target, tables, endpoints) -> bool:
    eligible = [
        endpoint
        for endpoint in endpoints
        if endpoint["VpcId"] == target.vpc_id and endpoint["State"] == "available"
    ]
    for service in ("ecr.api", "ecr.dkr"):
        if not any(
            endpoint["ServiceName"] == f"com.amazonaws.{config.region}.{service}"
            and endpoint["VpcEndpointType"] == "Interface"
            and endpoint["PrivateDnsEnabled"] is True
            for endpoint in eligible
        ):
            return False
    return all(
        any(
            endpoint["ServiceName"] == f"com.amazonaws.{config.region}.s3"
            and endpoint["VpcEndpointType"] == "Gateway"
            and table["RouteTableId"] in endpoint["RouteTableIds"]
            for endpoint in eligible
        )
        for table in _effective_tables(target, tables)
    )


def assess(
    config: DeploymentConfig,
    assets: AssetManifest,
    target: InspectionTarget,
    snapshot: AwsSnapshot,
    *,
    source: Literal["snapshot", "live_aws"] = "snapshot",
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    findings = []

    def finding(name, sections, check, detail):
        if any(
            section in snapshot.errors or section not in snapshot.responses for section in sections
        ):
            status = "unverified"
        else:
            try:
                status = "pass" if check() else "fail"
            except (KeyError, TypeError, ValueError, AttributeError):
                status = "unverified"
        findings.append({"id": name, "status": status, "detail": detail})

    expected = inspection_digest(config, assets, target)
    bound = snapshot.inspection_digest == expected
    age = (now - snapshot.collected_at).total_seconds()
    findings.append(
        {
            "id": "snapshot_binding",
            "status": "pass" if bound else "fail",
            "detail": "Metadata must match this deployment, assets and inspection target",
        }
    )
    findings.append(
        {
            "id": "snapshot_freshness",
            "status": "pass" if 0 <= age <= 300 else "fail",
            "detail": "Snapshot observation must be within the preceding five minutes",
        }
    )
    data = snapshot.responses
    if bound and 0 <= age <= 300:
        finding(
            "caller_account",
            ("identity",),
            lambda: data["identity"]["Account"] == config.account_id,
            "Caller account matches configuration",
        )
        identity_ok = findings[-1]["status"] == "pass"
        if identity_ok:
            cluster_arn = (
                f"arn:aws:eks:{config.region}:{config.account_id}:cluster/{target.cluster_name}"
            )
            finding(
                "cluster_private_endpoint",
                ("cluster",),
                lambda: (
                    data["cluster"]["name"] == target.cluster_name
                    and data["cluster"]["arn"] == cluster_arn
                    and data["cluster"]["status"] == "ACTIVE"
                    and data["cluster"]["resourcesVpcConfig"]["vpcId"] == target.vpc_id
                    and data["cluster"]["resourcesVpcConfig"]["endpointPrivateAccess"] is True
                    and data["cluster"]["resourcesVpcConfig"]["endpointPublicAccess"] is False
                ),
                "Active expected EKS cluster has private-only Kubernetes API access",
            )
            finding(
                "placement_configuration",
                ("placement",),
                lambda: _placement_matches(config, target, data["placement"]),
                "Active placement matches cluster, selectors and exact subnet set; "
                "actual pod placement and IAM privileges remain unverified",
            )
            finding(
                "subnet_configuration",
                ("subnets",),
                lambda: (
                    len(data["subnets"]["Subnets"]) == len(target.subnet_ids)
                    and {row["SubnetId"] for row in data["subnets"]["Subnets"]}
                    == set(target.subnet_ids)
                    and all(
                        row["VpcId"] == target.vpc_id
                        and row["OwnerId"] == config.account_id
                        and row["State"] == "available"
                        and row["MapPublicIpOnLaunch"] is False
                        for row in data["subnets"]["Subnets"]
                    )
                ),
                "Expected subnets match account/VPC and disable public IPv4 auto-assignment",
            )
            finding(
                "route_targets",
                ("route_tables", "endpoints"),
                lambda: _routes_restricted(
                    config,
                    target,
                    data["route_tables"]["RouteTables"],
                    data["endpoints"]["VpcEndpoints"],
                ),
                "Routes use only VPC-local routing and available regional S3 gateway endpoints; "
                "no proof of traffic isolation",
            )
            finding(
                "private_image_endpoint_metadata",
                ("route_tables", "endpoints"),
                lambda: _private_endpoints_present(
                    config,
                    target,
                    data["route_tables"]["RouteTables"],
                    data["endpoints"]["VpcEndpoints"],
                ),
                "Available ECR interfaces advertise private DNS; S3 associates with route tables. "
                "DNS resolution, endpoint policies and connectivity remain unverified",
            )
            finding(
                "security_group_scope",
                ("security_groups",),
                lambda: (
                    len(data["security_groups"]["SecurityGroups"]) == len(config.security_group_ids)
                    and {row["GroupId"] for row in data["security_groups"]["SecurityGroups"]}
                    == set(config.security_group_ids)
                    and all(
                        row["VpcId"] == target.vpc_id and row["OwnerId"] == config.account_id
                        for row in data["security_groups"]["SecurityGroups"]
                    )
                ),
                "Configured groups match account/VPC; actual ENI attachment is unverified",
            )
            finding(
                "security_groups_no_world_ranges",
                ("security_groups",),
                lambda: (
                    not any(
                        cidr.get("CidrIp") == "0.0.0.0/0" or cidr.get("CidrIpv6") == "::/0"
                        for group in data["security_groups"]["SecurityGroups"]
                        for direction in ("IpPermissions", "IpPermissionsEgress")
                        for rule in group[direction]
                        for cidr in rule.get("IpRanges", []) + rule.get("Ipv6Ranges", [])
                    )
                ),
                "No literal world CIDRs in configured rules; narrower CIDRs, prefix lists, "
                "peer groups and rule semantics still require review",
            )
            finding(
                "ecr_digest_present",
                ("image",),
                lambda: (
                    len(data["image"]["imageDetails"]) == 1
                    and data["image"]["imageDetails"][0]["imageDigest"]
                    == config.image.split("@", 1)[1]
                    and data["image"]["imageDetails"][0]["registryId"] == config.account_id
                    and data["image"]["imageDetails"][0]["repositoryName"]
                    == config.image.split("/", 1)[1].split("@", 1)[0]
                ),
                "Image digest exists in ECR; provenance and runtime identity are unverified",
            )
    return {
        "schema_version": 1,
        "inspection_digest": expected,
        "source": source,
        "observed_at": snapshot.collected_at.isoformat(),
        "assessed_at": now.isoformat(),
        "readiness": "blocked",
        "execution_authorized": False,
        "findings": findings,
        "collection_errors": snapshot.errors,
        "unverified_live_checks": list(required_live_checks(config)),
    }


def inspection_policy(config: DeploymentConfig, target: InspectionTarget) -> dict:
    """Render an operator-reviewable IAM permissions policy; never create or attach it."""
    eks_prefix = f"arn:aws:eks:{config.region}:{config.account_id}"
    if config.profile == Deployment.FARGATE_APP:
        action = "eks:DescribeFargateProfile"
        placement = f"{eks_prefix}:fargateprofile/{target.cluster_name}/{config.fargate_profile}/*"
    else:
        action = "eks:DescribeNodegroup"
        placement = f"{eks_prefix}:nodegroup/{target.cluster_name}/{config.nodegroup}/*"
    repository = config.image.split("/", 1)[1].split("@", 1)[0]
    repository_arn = f"arn:aws:ecr:{config.region}:{config.account_id}:repository/{repository}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": "eks:DescribeCluster",
                "Resource": f"{eks_prefix}:cluster/{target.cluster_name}",
            },
            {"Effect": "Allow", "Action": action, "Resource": placement},
            {
                "Effect": "Allow",
                "Action": "ecr:DescribeImages",
                "Resource": repository_arn,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeRouteTables",
                    "ec2:DescribeVpcEndpoints",
                ],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": config.region}},
            },
        ],
    }
