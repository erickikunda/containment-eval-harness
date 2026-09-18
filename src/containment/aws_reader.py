"""Optional read-only boto3 adapter. Imported only for explicitly requested live inspection."""

from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from containment.aws_inspection import AwsSnapshot, InspectionTarget, inspection_digest
from containment.deployment import AssetManifest, DeploymentConfig
from containment.models import Deployment


def clients_for(config: DeploymentConfig, profile_name: str | None = None) -> dict:
    session = boto3.Session(profile_name=profile_name, region_name=config.region)
    settings = Config(
        region_name=config.region,
        connect_timeout=3,
        read_timeout=5,
        retries={"mode": "standard", "total_max_attempts": 2},
        ignore_configured_endpoint_urls=True,
    )
    return {
        service: session.client(service, config=settings)
        for service in ("sts", "eks", "ec2", "ecr")
    }


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items() if key != "ResponseMetadata"}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def collect(
    config: DeploymentConfig, assets: AssetManifest, target: InspectionTarget, clients: dict
) -> AwsSnapshot:
    responses = {}
    errors = {}
    started = datetime.now(UTC)

    def read(section, service, method, kwargs, result_key=None, paginate=False):
        # All call sites below are fixed read-only SDK operations, never producer-selected.
        try:
            operation = getattr(clients[service], method)
            if paginate:
                rows, seen = [], set()
                request = {**kwargs, "MaxResults": 100}
                for _ in range(20):
                    page = operation(**request)
                    rows.extend(page[result_key])
                    token = page.get("NextToken")
                    if not token:
                        responses[section] = _json_value({result_key: rows})
                        return
                    if token in seen:
                        raise ValueError("RepeatedPaginationToken")
                    seen.add(token)
                    request["NextToken"] = token
                raise ValueError("PaginationLimitExceeded")
            response = operation(**kwargs)
            if response.get("NextToken") or response.get("nextToken"):
                raise ValueError("UnexpectedPagination")
            responses[section] = _json_value(response[result_key] if result_key else response)
        except ClientError as exc:
            errors[section] = exc.response.get("Error", {}).get("Code", "ClientError")
        except (BotoCoreError, KeyError, ValueError) as exc:
            errors[section] = type(exc).__name__

    read("identity", "sts", "get_caller_identity", {})
    # Do not query the rest of an unintended account, even if credentials permit it.
    if responses.get("identity", {}).get("Account") == config.account_id:
        read("cluster", "eks", "describe_cluster", {"name": target.cluster_name}, "cluster")
        if config.profile == Deployment.FARGATE_APP:
            read(
                "placement",
                "eks",
                "describe_fargate_profile",
                {
                    "clusterName": target.cluster_name,
                    "fargateProfileName": config.fargate_profile,
                },
                "fargateProfile",
            )
        else:
            read(
                "placement",
                "eks",
                "describe_nodegroup",
                {
                    "clusterName": target.cluster_name,
                    "nodegroupName": config.nodegroup,
                },
                "nodegroup",
            )
        read("subnets", "ec2", "describe_subnets", {"SubnetIds": list(target.subnet_ids)})
        read(
            "security_groups",
            "ec2",
            "describe_security_groups",
            {"GroupIds": list(config.security_group_ids)},
        )
        filters = {"Filters": [{"Name": "vpc-id", "Values": [target.vpc_id]}]}
        read("route_tables", "ec2", "describe_route_tables", filters, "RouteTables", True)
        read("endpoints", "ec2", "describe_vpc_endpoints", filters, "VpcEndpoints", True)
        repository, image_digest = config.image.split("/", 1)[1].split("@", 1)
        read(
            "image",
            "ecr",
            "describe_images",
            {
                "registryId": config.account_id,
                "repositoryName": repository,
                "imageIds": [{"imageDigest": image_digest}],
            },
        )
    return AwsSnapshot(
        inspection_digest=inspection_digest(config, assets, target),
        collected_at=started,
        responses=responses,
        errors=errors,
    )


def live_snapshot(config, assets, target, profile_name=None) -> AwsSnapshot:
    try:
        clients = clients_for(config, profile_name)
    except BotoCoreError as exc:
        raise RuntimeError(f"AWS client initialization failed: {type(exc).__name__}") from exc
    try:
        return collect(config, assets, target, clients)
    finally:
        for client in clients.values():
            client.close()
