import copy
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from containment.aws_inspection import (
    AwsSnapshot,
    InspectionTarget,
    assess,
    inspection_digest,
    inspection_policy,
)
from containment.aws_reader import collect
from containment.cli import main
from containment.deployment import AssetManifest, DeploymentConfig

EXAMPLES = Path(__file__).parents[1] / "deployment" / "examples"


@pytest.fixture
def inputs():
    return (
        DeploymentConfig.model_validate_json((EXAMPLES / "eks-fargate-app.json").read_text()),
        AssetManifest.model_validate_json((EXAMPLES / "assets.json").read_text()),
        InspectionTarget.model_validate_json((EXAMPLES / "inspection-target.json").read_text()),
    )


@pytest.fixture
def snapshot(inputs):
    config, assets, target = inputs
    table_id = "rtb-0123456789abcdef0"
    data = {
        "identity": {"Account": config.account_id},
        "cluster": {
            "name": target.cluster_name,
            "arn": f"arn:aws:eks:{config.region}:{config.account_id}:cluster/{target.cluster_name}",
            "status": "ACTIVE",
            "resourcesVpcConfig": {
                "vpcId": target.vpc_id,
                "endpointPrivateAccess": True,
                "endpointPublicAccess": False,
            },
        },
        "placement": {
            "status": "ACTIVE",
            "clusterName": target.cluster_name,
            "fargateProfileName": config.fargate_profile,
            "subnets": list(target.subnet_ids),
            "selectors": [{"namespace": config.experiment_namespace}],
            "podExecutionRoleArn": f"arn:aws:iam::{config.account_id}:role/fargate-execution",
        },
        "subnets": {
            "Subnets": [
                {
                    "SubnetId": target.subnet_ids[0],
                    "VpcId": target.vpc_id,
                    "OwnerId": config.account_id,
                    "State": "available",
                    "MapPublicIpOnLaunch": False,
                }
            ]
        },
        "security_groups": {
            "SecurityGroups": [
                {
                    "GroupId": config.security_group_ids[0],
                    "OwnerId": config.account_id,
                    "VpcId": target.vpc_id,
                    "IpPermissions": [],
                    "IpPermissionsEgress": [],
                }
            ]
        },
        "route_tables": {
            "RouteTables": [
                {
                    "RouteTableId": table_id,
                    "VpcId": target.vpc_id,
                    "Associations": [{"Main": True, "AssociationState": {"State": "associated"}}],
                    "Routes": [
                        {
                            "GatewayId": "local",
                            "DestinationCidrBlock": "10.0.0.0/16",
                            "State": "active",
                        }
                    ],
                }
            ]
        },
        "endpoints": {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": f"vpce-0123456789abcdef{i}",
                    "VpcId": target.vpc_id,
                    "State": "available",
                    "ServiceName": f"com.amazonaws.{config.region}.{service}",
                    "VpcEndpointType": "Gateway" if service == "s3" else "Interface",
                    "PrivateDnsEnabled": service != "s3",
                    "RouteTableIds": [table_id] if service == "s3" else [],
                }
                for i, service in enumerate(("ecr.api", "ecr.dkr", "s3"))
            ]
        },
        "image": {
            "imageDetails": [
                {
                    "registryId": config.account_id,
                    "repositoryName": "containment-preflight",
                    "imageDigest": config.image.split("@")[1],
                }
            ]
        },
    }
    return AwsSnapshot(
        inspection_digest=inspection_digest(config, assets, target),
        collected_at=datetime.now(UTC),
        responses=data,
        errors={},
    )


def statuses(inputs, snapshot):
    return {finding["id"]: finding["status"] for finding in assess(*inputs, snapshot)["findings"]}


def test_safe_metadata_never_authorizes_execution(inputs, snapshot):
    report = assess(*inputs, snapshot)
    assert all(finding["status"] == "pass" for finding in report["findings"])
    assert report["readiness"] == "blocked"
    assert report["execution_authorized"] is False
    assert report["source"] == "snapshot"
    assert "external_evidence_and_watchdog" in report["unverified_live_checks"]


@pytest.mark.parametrize(
    "section,path,value,check",
    [
        ("identity", ["Account"], "000000000000", "caller_account"),
        (
            "cluster",
            ["resourcesVpcConfig", "endpointPublicAccess"],
            True,
            "cluster_private_endpoint",
        ),
        (
            "cluster",
            ["resourcesVpcConfig", "endpointPrivateAccess"],
            False,
            "cluster_private_endpoint",
        ),
        ("cluster", ["resourcesVpcConfig", "vpcId"], "vpc-00000000", "cluster_private_endpoint"),
        ("placement", ["selectors"], [{"namespace": "different"}], "placement_configuration"),
        ("placement", ["subnets"], ["subnet-00000000"], "placement_configuration"),
        (
            "placement",
            ["podExecutionRoleArn"],
            "arn:aws:iam::000000000000:role/wrong",
            "placement_configuration",
        ),
        ("placement", ["status"], "DELETING", "placement_configuration"),
    ],
)
def test_unsafe_metadata_fails(inputs, snapshot, section, path, value, check):
    node = snapshot.responses[section]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert statuses(inputs, snapshot)[check] == "fail"


@pytest.mark.parametrize(
    "destination,target",
    [
        ({"DestinationCidrBlock": "0.0.0.0/0"}, {"NatGatewayId": "nat-0123456789abcdef0"}),
        ({"DestinationCidrBlock": "0.0.0.0/0"}, {"GatewayId": "igw-0123456789abcdef0"}),
        (
            {"DestinationIpv6CidrBlock": "::/0"},
            {"EgressOnlyInternetGatewayId": "eigw-0123456789abcdef0"},
        ),
        ({"DestinationCidrBlock": "10.20.0.0/16"}, {"TransitGatewayId": "tgw-0123456789abcdef0"}),
        (
            {"DestinationCidrBlock": "10.20.0.0/16"},
            {"VpcPeeringConnectionId": "pcx-0123456789abcdef0"},
        ),
        ({"DestinationCidrBlock": "0.0.0.0/1"}, {"NetworkInterfaceId": "eni-0123456789abcdef0"}),
    ],
)
def test_indirect_and_split_routes_are_not_treated_as_private(
    inputs, snapshot, destination, target
):
    table = snapshot.responses["route_tables"]["RouteTables"][0]
    table["Routes"].append({**destination, **target, "State": "active"})
    assert statuses(inputs, snapshot)["route_targets"] == "fail"


def test_explicit_route_table_overrides_main(inputs, snapshot):
    tables = snapshot.responses["route_tables"]["RouteTables"]
    unsafe = copy.deepcopy(tables[0])
    unsafe["RouteTableId"] = "rtb-11111111"
    unsafe["Associations"] = [
        {"SubnetId": inputs[2].subnet_ids[0], "AssociationState": {"State": "associated"}}
    ]
    unsafe["Routes"].append(
        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-11111111", "State": "active"}
    )
    tables.append(unsafe)
    assert statuses(inputs, snapshot)["route_targets"] == "fail"


def test_ambiguous_or_changing_association_not_assumed_safe(inputs, snapshot):
    table = snapshot.responses["route_tables"]["RouteTables"][0]
    table["Associations"].append(
        {"SubnetId": inputs[2].subnet_ids[0], "AssociationState": {"State": "associating"}}
    )
    assert statuses(inputs, snapshot)["route_targets"] == "unverified"


def test_s3_gateway_route_requires_available_associated_regional_endpoint(inputs, snapshot):
    table = snapshot.responses["route_tables"]["RouteTables"][0]
    endpoint = snapshot.responses["endpoints"]["VpcEndpoints"][2]
    table["Routes"].append(
        {
            "DestinationPrefixListId": "pl-11111111",
            "GatewayId": endpoint["VpcEndpointId"],
            "State": "active",
        }
    )
    assert statuses(inputs, snapshot)["route_targets"] == "pass"
    endpoint["RouteTableIds"] = []
    assert statuses(inputs, snapshot)["route_targets"] == "fail"
    assert statuses(inputs, snapshot)["private_image_endpoint_metadata"] == "fail"


@pytest.mark.parametrize(
    "direction,field,cidr",
    [
        ("IpPermissions", "IpRanges", {"CidrIp": "0.0.0.0/0"}),
        ("IpPermissionsEgress", "Ipv6Ranges", {"CidrIpv6": "::/0"}),
    ],
)
def test_world_cidrs_fail(inputs, snapshot, direction, field, cidr):
    snapshot.responses["security_groups"]["SecurityGroups"][0][direction] = [{field: [cidr]}]
    assert statuses(inputs, snapshot)["security_groups_no_world_ranges"] == "fail"


@pytest.mark.parametrize("section", ["cluster", "placement", "route_tables", "endpoints", "image"])
def test_denied_or_missing_sections_are_not_passes(inputs, snapshot, section):
    del snapshot.responses[section]
    snapshot.errors[section] = "AccessDenied"
    assert "unverified" in statuses(inputs, snapshot).values()


@pytest.mark.parametrize("age", [301, -10])
def test_stale_or_future_snapshot_not_assessed(inputs, snapshot, age):
    now = snapshot.collected_at + timedelta(seconds=age)
    report = assess(*inputs, snapshot, now=now)
    assert [row["id"] for row in report["findings"]] == ["snapshot_binding", "snapshot_freshness"]
    assert report["findings"][1]["status"] == "fail"


def test_wrong_binding_not_assessed(inputs, snapshot):
    altered = snapshot.model_copy(update={"inspection_digest": "0" * 64})
    assert statuses(inputs, altered)["snapshot_binding"] == "fail"
    assert "caller_account" not in statuses(inputs, altered)


@pytest.mark.parametrize("profile", ["eks-ec2-vm", "fargate-controller-ec2-vm"])
def test_ec2_and_hybrid_placement(inputs, snapshot, profile):
    config = DeploymentConfig.model_validate_json((EXAMPLES / f"{profile}.json").read_text())
    _, assets, target = inputs
    snapshot.responses["placement"] = {
        "clusterName": target.cluster_name,
        "nodegroupName": config.nodegroup,
        "status": "ACTIVE",
        "subnets": list(target.subnet_ids),
    }
    snapshot = snapshot.model_copy(
        update={"inspection_digest": inspection_digest(config, assets, target)}
    )
    assert statuses((config, assets, target), snapshot)["placement_configuration"] == "pass"


def test_snapshot_cli_does_not_load_live_sdk(tmp_path, capsys, snapshot, monkeypatch):
    import sys

    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    monkeypatch.setitem(sys.modules, "containment.aws_reader", None)
    result = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "aws-inspect",
            str(EXAMPLES / "eks-fargate-app.json"),
            str(EXAMPLES / "assets.json"),
            str(EXAMPLES / "inspection-target.json"),
            "--snapshot",
            str(path),
        ]
    )
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "snapshot"
    assert report["execution_authorized"] is False
    assert not (tmp_path / "state").exists()


@pytest.fixture
def sdk_clients():
    clients = {
        name: boto3.client(
            name,
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            config=Config(ignore_configured_endpoint_urls=True),
        )
        for name in ("sts", "eks", "ec2", "ecr")
    }
    with ExitStack() as stack:
        stubs = {name: stack.enter_context(Stubber(client)) for name, client in clients.items()}
        yield clients, stubs
        for stub in stubs.values():
            stub.assert_no_pending_responses()
    for client in clients.values():
        client.close()


def test_live_reader_stops_after_wrong_account(inputs, sdk_clients):
    clients, stubs = sdk_clients
    stubs["sts"].add_response("get_caller_identity", {"Account": "000000000000"}, {})
    result = collect(*inputs, clients)
    assert set(result.responses) == {"identity"}


def stub_start(inputs, snapshot, stubs):
    config, _, target = inputs
    data = snapshot.responses
    stubs["sts"].add_response("get_caller_identity", data["identity"], {})
    stubs["eks"].add_response(
        "describe_cluster", {"cluster": data["cluster"]}, {"name": target.cluster_name}
    )
    stubs["eks"].add_response(
        "describe_fargate_profile",
        {"fargateProfile": data["placement"]},
        {"clusterName": target.cluster_name, "fargateProfileName": config.fargate_profile},
    )
    stubs["ec2"].add_response(
        "describe_subnets", data["subnets"], {"SubnetIds": list(target.subnet_ids)}
    )
    stubs["ec2"].add_response(
        "describe_security_groups",
        data["security_groups"],
        {"GroupIds": list(config.security_group_ids)},
    )


def test_sdk_reads_only_expected_scoped_operations(inputs, snapshot, sdk_clients):
    config, assets, target = inputs
    clients, stubs = sdk_clients
    data = snapshot.responses
    stub_start(inputs, snapshot, stubs)
    filters = {"Filters": [{"Name": "vpc-id", "Values": [target.vpc_id]}], "MaxResults": 100}
    stubs["ec2"].add_response(
        "describe_route_tables", {"RouteTables": [], "NextToken": "page-2"}, filters
    )
    stubs["ec2"].add_response(
        "describe_route_tables", data["route_tables"], {**filters, "NextToken": "page-2"}
    )
    stubs["ec2"].add_response("describe_vpc_endpoints", data["endpoints"], filters)
    stubs["ecr"].add_response(
        "describe_images",
        data["image"],
        {
            "registryId": config.account_id,
            "repositoryName": "containment-preflight",
            "imageIds": [{"imageDigest": config.image.split("@")[1]}],
        },
    )
    actual = collect(config, assets, target, clients)
    assert actual.errors == {}
    assert actual.responses == snapshot.responses
    assert all(row["status"] == "pass" for row in assess(*inputs, actual)["findings"])


@pytest.mark.parametrize("failure", ["denied", "repeated_token", "too_many_pages"])
def test_incomplete_pagination_discards_partial_inventory(inputs, snapshot, sdk_clients, failure):
    config, _, target = inputs
    clients, stubs = sdk_clients
    stub_start(inputs, snapshot, stubs)
    filters = {"Filters": [{"Name": "vpc-id", "Values": [target.vpc_id]}], "MaxResults": 100}
    if failure == "denied":
        stubs["ec2"].add_client_error(
            "describe_route_tables",
            service_error_code="UnauthorizedOperation",
            service_message="do-not-echo-this",
            expected_params=filters,
        )
    else:
        request = dict(filters)
        for index in range(2 if failure == "repeated_token" else 20):
            token = "same-token" if failure == "repeated_token" else f"page-{index}"
            stubs["ec2"].add_response(
                "describe_route_tables",
                {
                    **snapshot.responses["route_tables"],
                    "NextToken": token,
                },
                dict(request),
            )
            request["NextToken"] = token
    stubs["ec2"].add_response("describe_vpc_endpoints", snapshot.responses["endpoints"], filters)
    stubs["ecr"].add_response(
        "describe_images",
        snapshot.responses["image"],
        {
            "registryId": config.account_id,
            "repositoryName": "containment-preflight",
            "imageIds": [{"imageDigest": config.image.split("@")[1]}],
        },
    )
    actual = collect(*inputs, clients)
    assert "route_tables" not in actual.responses
    assert "route_tables" in actual.errors
    assert "do-not-echo-this" not in actual.model_dump_json()
    assert statuses(inputs, actual)["route_targets"] == "unverified"


def test_missing_credentials_are_reported_without_other_queries(inputs, sdk_clients, monkeypatch):
    from botocore.exceptions import NoCredentialsError

    clients, _ = sdk_clients

    def no_credentials():
        raise NoCredentialsError()

    monkeypatch.setattr(clients["sts"], "get_caller_identity", no_credentials)
    actual = collect(*inputs, clients)
    assert actual.responses == {}
    assert actual.errors == {"identity": "NoCredentialsError"}


def test_iam_policy_is_read_only_and_scoped(inputs):
    config, _, target = inputs
    policy = inspection_policy(config, target)
    by_action = {
        action: row
        for row in policy["Statement"]
        for action in (row["Action"] if isinstance(row["Action"], list) else [row["Action"]])
    }
    assert set(by_action) == {
        "sts:GetCallerIdentity",
        "eks:DescribeCluster",
        "eks:DescribeFargateProfile",
        "ecr:DescribeImages",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeRouteTables",
        "ec2:DescribeVpcEndpoints",
    }
    assert by_action["eks:DescribeCluster"]["Resource"].endswith(f"cluster/{target.cluster_name}")
    assert by_action["eks:DescribeFargateProfile"]["Resource"].endswith(
        f"/{config.fargate_profile}/*"
    )
    assert by_action["ecr:DescribeImages"]["Resource"].endswith("repository/containment-preflight")
    assert by_action["ec2:DescribeSubnets"]["Condition"] == {
        "StringEquals": {"aws:RequestedRegion": config.region}
    }


def test_cli_live_snapshot_export_is_explicit_and_never_overwrites(
    inputs, snapshot, tmp_path, monkeypatch, capsys
):
    import containment.aws_reader

    calls = []

    def fake_live(*args):
        calls.append(args)
        return snapshot

    monkeypatch.setattr(containment.aws_reader, "live_snapshot", fake_live)
    destination = tmp_path / "captured.json"
    args = [
        "aws-inspect",
        str(EXAMPLES / "eks-fargate-app.json"),
        str(EXAMPLES / "assets.json"),
        str(EXAMPLES / "inspection-target.json"),
        "--live",
        "--save-snapshot",
        str(destination),
    ]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["source"] == "live_aws"
    assert AwsSnapshot.model_validate_json(destination.read_text()) == snapshot
    before = destination.read_bytes()
    assert main(args) == 2
    assert "error:" in capsys.readouterr().err
    assert destination.read_bytes() == before


def test_aws_policy_cli_is_offline(tmp_path, capsys, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "containment.aws_reader", None)
    assert (
        main(
            [
                "--state-dir",
                str(tmp_path / "state"),
                "aws-inspection-policy",
                str(EXAMPLES / "eks-fargate-app.json"),
                str(EXAMPLES / "inspection-target.json"),
            ]
        )
        == 0
    )
    policy = json.loads(capsys.readouterr().out)
    assert policy["Version"] == "2012-10-17"
    assert not (tmp_path / "state").exists()
