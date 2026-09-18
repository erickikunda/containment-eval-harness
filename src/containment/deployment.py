"""Offline deployment planning. No Kubernetes/AWS clients or execution authorization."""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from containment.models import Deployment, StrictModel

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Group = Annotated[str, Field(pattern=r"^sg-([0-9a-f]{8}|[0-9a-f]{17})$")]


class DeploymentConfig(StrictModel):
    schema_version: Literal[1]
    deployment_id: Name
    profile: Deployment
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-[0-9]+$")
    experiment_namespace: Name
    control_namespace: Name
    image: str
    security_group_ids: tuple[Group, ...] = Field(min_length=1, max_length=5)
    nodegroup: Name | None = None
    fargate_profile: Name | None = None
    inference: Literal["local", "private_service", "replay"]

    @model_validator(mode="after")
    def validate_profile(self) -> "DeploymentConfig":
        if self.profile == Deployment.FAKE:
            raise ValueError("AWS deployment configuration cannot use the fake profile")
        if self.experiment_namespace == self.control_namespace:
            raise ValueError("Control and experiment namespaces must differ")
        if any(
            n.startswith(("kube-", "default"))
            for n in (self.experiment_namespace, self.control_namespace)
        ):
            raise ValueError("Use dedicated harness namespaces")
        registry = f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com/"
        if not self.image.startswith(registry) or not re.fullmatch(
            r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}",
            self.image.removeprefix(registry),
        ):
            raise ValueError("Image must be digest-pinned in the configured private ECR registry")
        if len(set(self.security_group_ids)) != len(self.security_group_ids):
            raise ValueError("Duplicate security group IDs")
        if self.profile == Deployment.FARGATE_APP:
            if self.fargate_profile is None or self.nodegroup is not None:
                raise ValueError("Fargate application profile requires only a Fargate selector")
        elif self.nodegroup is None or self.fargate_profile is not None:
            raise ValueError("EC2 experiment profiles require only an EC2 nodegroup selector")
        return self


class Asset(StrictModel):
    path: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9_./-]+$")
    kind: Literal["task", "runtime", "model_weights", "tokenizer"]
    sha256: Sha256
    size_bytes: int = Field(ge=0, le=2**40)

    @model_validator(mode="after")
    def relative_path(self) -> "Asset":
        if any(part in {"", ".", ".."} for part in self.path.split("/")):
            raise ValueError("Asset paths must be normalized relative paths")
        return self


class AssetManifest(StrictModel):
    schema_version: Literal[1]
    assets: tuple[Asset, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_paths(self) -> "AssetManifest":
        if len({asset.path for asset in self.assets}) != len(self.assets):
            raise ValueError("Duplicate asset paths")
        return self


def configuration_digest(config: DeploymentConfig, assets: AssetManifest) -> str:
    body = {"deployment": config.model_dump(mode="json"), "assets": assets.model_dump(mode="json")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def required_live_checks(config: DeploymentConfig) -> tuple[str, ...]:
    checks = (
        "private_cluster_and_routes",
        "image_digest_and_supply_chain",
        "pod_security_group_enforcement",
        "metadata_and_credential_endpoints_denied",
        "management_and_cross_trial_access_denied",
        "external_evidence_and_watchdog",
        "termination_and_cleanup_verified",
        "dedicated_worker_placement",
    )
    if config.profile == Deployment.FARGATE_APP:
        checks += ("fargate_profile_and_execution_role",)
    else:
        checks += ("ec2_network_policy_enforcement", "kvm_and_disposable_vm_isolation")
    if config.profile == Deployment.HYBRID:
        checks += ("controller_fargate_worker_ec2_separation",)
    if config.inference == "private_service":
        checks += ("private_inference_gateway_and_budgets",)
    elif config.inference == "local":
        checks += ("local_model_capacity_and_offline_loading",)
    return checks


def check_assets_for_profile(config: DeploymentConfig, assets: AssetManifest) -> None:
    if config.inference == "local":
        kinds = {asset.kind for asset in assets.assets}
        if not {"model_weights", "tokenizer"} <= kinds:
            raise ValueError("Local inference requires prepositioned model_weights and tokenizer")


def deployment_plan(config: DeploymentConfig, assets: AssetManifest) -> dict:
    check_assets_for_profile(config, assets)
    digest = configuration_digest(config, assets)
    namespace = config.experiment_namespace
    labels = {
        "app.kubernetes.io/name": "containment-preflight",
        "containment.eval/deployment": config.deployment_id,
    }
    name = f"{config.deployment_id}-preflight"

    def resource(api, kind, resource_name, **body):
        return {
            "apiVersion": api,
            "kind": kind,
            "metadata": {"name": resource_name, "namespace": namespace},
            **body,
        }

    items = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/warn": "restricted",
                },
            },
        }
    ]
    items += [
        resource("v1", "ServiceAccount", name, automountServiceAccountToken=False),
        resource(
            "v1",
            "ConfigMap",
            name,
            immutable=True,
            data={
                "deployment.json": config.model_dump_json(),
                "assets.json": assets.model_dump_json(),
            },
        ),
        resource(
            "v1",
            "ResourceQuota",
            name,
            spec={
                "hard": {
                    "requests.cpu": "1",
                    "requests.memory": "1Gi",
                    "limits.cpu": "2",
                    "limits.memory": "2Gi",
                    "pods": "2",
                    "count/jobs.batch": "1",
                }
            },
        ),
        resource(
            "vpcresources.k8s.aws/v1beta1",
            "SecurityGroupPolicy",
            name,
            spec={
                "podSelector": {"matchLabels": labels},
                "securityGroups": {"groupIds": list(config.security_group_ids)},
            },
        ),
    ]
    if config.profile != Deployment.FARGATE_APP:
        items.append(
            resource(
                "networking.k8s.io/v1",
                "NetworkPolicy",
                name,
                spec={
                    "podSelector": {"matchLabels": labels},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [],
                },
            )
        )
    pod_labels = dict(labels)
    placement = {}
    if config.profile == Deployment.FARGATE_APP:
        pod_labels["eks.amazonaws.com/fargate-profile"] = config.fargate_profile
        placement["nodeSelector"] = {"eks.amazonaws.com/compute-type": "fargate"}
    else:
        placement["nodeSelector"] = {"eks.amazonaws.com/nodegroup": config.nodegroup}
    items.append(
        resource(
            "batch/v1",
            "Job",
            name,
            spec={
                "suspend": True,
                "backoffLimit": 0,
                "activeDeadlineSeconds": 120,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {
                        "labels": pod_labels,
                        "annotations": {
                            "containment.eval/configuration-digest": digest,
                            "containment.eval/readiness": "blocked",
                        },
                    },
                    "spec": {
                        **placement,
                        "serviceAccountName": name,
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "enableServiceLinks": False,
                        "hostNetwork": False,
                        "hostPID": False,
                        "hostIPC": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "preflight",
                                "image": config.image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["containment"],
                                "args": [
                                    "preflight",
                                    "/config/deployment.json",
                                    "/config/assets.json",
                                    "--asset-root",
                                    "/opt/containment/assets",
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "privileged": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "256Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/config", "readOnly": True}
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": name}}],
                    },
                },
            },
        )
    )
    return {
        "schema_version": 1,
        "configuration_digest": digest,
        "readiness": "blocked",
        "execution_authorized": False,
        "required_live_checks": list(required_live_checks(config)),
        "manifests": {"apiVersion": "v1", "kind": "List", "items": items},
    }
