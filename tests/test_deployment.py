import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from containment.deployment import (
    Asset,
    AssetManifest,
    DeploymentConfig,
    configuration_digest,
    deployment_plan,
)
from containment.preflight import preflight, verify_asset

EXAMPLES = Path(__file__).parents[1] / "deployment" / "examples"


@pytest.fixture
def config_data():
    return json.loads((EXAMPLES / "eks-ec2-vm.json").read_text())


@pytest.fixture
def assets():
    return AssetManifest(
        schema_version=1,
        assets=(
            Asset(
                path="probe.txt",
                kind="task",
                sha256=hashlib.sha256(b"hello").hexdigest(),
                size_bytes=5,
            ),
        ),
    )


@pytest.mark.parametrize("profile", ["eks-ec2-vm", "eks-fargate-app", "fargate-controller-ec2-vm"])
def test_rendered_preflight_has_no_execution_authority(profile, assets):
    config = DeploymentConfig.model_validate_json((EXAMPLES / f"{profile}.json").read_text())
    plan = deployment_plan(config, assets)
    assert plan["readiness"] == "blocked"
    assert plan["execution_authorized"] is False
    resources = {item["kind"]: item for item in plan["manifests"]["items"]}
    assert not {"RoleBinding", "ClusterRoleBinding", "Secret"} & resources.keys()
    assert resources["ServiceAccount"]["automountServiceAccountToken"] is False
    job = resources["Job"]["spec"]
    assert job["suspend"] is True
    assert job["backoffLimit"] == 0
    assert job["activeDeadlineSeconds"] <= 120
    pod = job["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert not any(pod[flag] for flag in ("hostNetwork", "hostPID", "hostIPC"))
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    security = container["securityContext"]
    assert security["privileged"] is False
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    assert not any("hostPath" in volume for volume in pod["volumes"])
    assert container["args"][0] == "preflight"
    if profile == "eks-fargate-app":
        assert "NetworkPolicy" not in resources
        assert "fargate_profile_and_execution_role" in plan["required_live_checks"]
        assert pod["nodeSelector"] == {"eks.amazonaws.com/compute-type": "fargate"}
    else:
        assert "kvm_and_disposable_vm_isolation" in plan["required_live_checks"]
        assert resources["NetworkPolicy"]["spec"]["egress"] == []
        assert resources["NetworkPolicy"]["spec"]["ingress"] == []
        assert pod["nodeSelector"]["eks.amazonaws.com/nodegroup"] == config.nodegroup
    assert resources["SecurityGroupPolicy"]["spec"]["podSelector"]["matchLabels"].items() <= (
        job["template"]["metadata"]["labels"].items()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "python:3.12"),
        ("image", "111122223333.dkr.ecr.us-east-1.amazonaws.com/app:latest"),
        ("account_id", "000000000000"),
        ("region", "eu-west-1"),
        ("experiment_namespace", "containment-control"),
        ("experiment_namespace", "kube-system"),
        ("experiment_namespace", "../escape"),
        ("security_group_ids", []),
        ("security_group_ids", ["sg-0123456789abcdef0", "sg-0123456789abcdef0"]),
        ("nodegroup", None),
        ("fargate_profile", "extra-profile"),
        ("profile", "fake"),
        ("execution_authorized", True),
    ],
)
def test_unsafe_or_inconsistent_configuration_rejected(config_data, field, value):
    config_data[field] = value
    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate_json(json.dumps(config_data))


def test_fargate_rejects_ec2_selector(config_data):
    config_data.update(profile="eks_fargate_app", fargate_profile="preflight")
    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate_json(json.dumps(config_data))


def test_local_inference_requires_model_and_tokenizer(config_data, assets):
    config_data["inference"] = "local"
    config = DeploymentConfig.model_validate_json(json.dumps(config_data))
    with pytest.raises(ValueError, match="model_weights"):
        deployment_plan(config, assets)


def test_private_inference_adds_gate(config_data, assets):
    config_data["inference"] = "private_service"
    config = DeploymentConfig.model_validate_json(json.dumps(config_data))
    assert (
        "private_inference_gateway_and_budgets"
        in deployment_plan(config, assets)["required_live_checks"]
    )


def test_digest_binds_profile_and_asset_content(config_data, assets):
    config = DeploymentConfig.model_validate_json(json.dumps(config_data))
    first = configuration_digest(config, assets)
    assert first == configuration_digest(config, assets)
    config_data["nodegroup"] = "other-workers"
    assert first != configuration_digest(
        DeploymentConfig.model_validate_json(json.dumps(config_data)), assets
    )
    changed = AssetManifest(
        schema_version=1,
        assets=(Asset(path="probe.txt", kind="task", sha256="a" * 64, size_bytes=5),),
    )
    assert first != configuration_digest(config, changed)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../secret", "dir/../secret", "./file", "dir//file", "dir/", "a\\b"]
)
def test_asset_traversal_and_noncanonical_paths_rejected(path):
    with pytest.raises(ValidationError):
        Asset(path=path, kind="task", sha256="a" * 64, size_bytes=0)


def test_duplicate_asset_rejected(assets):
    with pytest.raises(ValidationError, match="Duplicate"):
        AssetManifest(schema_version=1, assets=(assets.assets[0], assets.assets[0]))


def test_asset_hash_and_size_checked(tmp_path, assets):
    asset = assets.assets[0]
    (tmp_path / asset.path).write_bytes(b"hello")
    assert verify_asset(tmp_path, asset)["status"] == "pass"
    (tmp_path / asset.path).write_bytes(b"HELLO")
    assert verify_asset(tmp_path, asset)["status"] == "fail"
    (tmp_path / asset.path).write_bytes(b"hello!")
    assert verify_asset(tmp_path, asset)["status"] == "fail"
    (tmp_path / asset.path).unlink()
    assert verify_asset(tmp_path, asset)["status"] == "fail"


@pytest.mark.parametrize("component", ["file", "directory", "root"])
def test_symlinks_are_not_followed(tmp_path, assets, component):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "probe.txt").write_bytes(b"hello")
    asset = assets.assets[0]
    if component == "file":
        (root / "probe.txt").symlink_to(outside / "probe.txt")
    elif component == "directory":
        (root / "dir").symlink_to(outside, target_is_directory=True)
        asset = Asset(path="dir/probe.txt", kind="task", sha256=asset.sha256, size_bytes=5)
    else:
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    assert verify_asset(root, asset)["status"] == "fail"


def test_fifo_rejected_without_blocking(tmp_path, assets):
    import os

    os.mkfifo(tmp_path / "probe.txt")
    assert verify_asset(tmp_path, assets.assets[0])["status"] == "fail"


def test_successful_local_probes_never_authorize_execution(tmp_path, config_data, assets):
    (tmp_path / "probe.txt").write_bytes(b"hello")
    config = DeploymentConfig.model_validate_json(json.dumps(config_data))
    report = preflight(config, assets, tmp_path)
    assert report["checks"][0]["status"] == "pass"
    assert any(check["status"] == "unverified" for check in report["checks"])
    assert report["readiness"] == "blocked"
    assert report["execution_authorized"] is False
