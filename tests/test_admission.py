import json

import pytest
from pydantic import ValidationError

from containment.admission import development_policy, manifest_digest, resolve
from containment.models import Deployment, Manifest, SafetyPolicy, Scenario


def deployment_policy():
    return SafetyPolicy(
        policy_id="test-static-compatibility",
        allowed_deployments=frozenset(Deployment),
        ceilings=development_policy().ceilings,
    )


@pytest.mark.parametrize("value", ["30", True, 0, -1, 1.5])
def test_budget_rejects_coercion_and_invalid_values(scenario_data, value):
    scenario_data["budget"]["wall_seconds"] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate_json(json.dumps(scenario_data))


def test_unknown_fields_are_rejected(scenario_data):
    scenario_data["host_command"] = "anything"
    with pytest.raises(ValidationError):
        Scenario.model_validate_json(json.dumps(scenario_data))


def test_policy_ceiling_is_enforced(scenario_data):
    scenario_data["budget"]["wall_seconds"] = 61
    scenario = Scenario.model_validate_json(json.dumps(scenario_data))
    with pytest.raises(ValueError, match="wall_seconds"):
        resolve(scenario, development_policy())


@pytest.mark.parametrize("deployment", ["eks_ec2_vm", "fargate_controller_ec2_vm"])
def test_vm_profiles_statically_support_container_boundary(scenario_data, deployment):
    scenario_data.update(
        deployment=deployment, boundary="container_to_guest", connectivity="private_services"
    )
    manifest = resolve(Scenario.model_validate_json(json.dumps(scenario_data)), deployment_policy())
    assert manifest.admission == "static_only"


def test_fargate_cannot_omit_derived_vm_requirement(scenario_data):
    scenario_data.update(
        deployment="eks_fargate_app",
        boundary="container_to_guest",
        connectivity="private_services",
        required_capabilities=[],
    )
    with pytest.raises(ValueError, match="disposable_guest_vm"):
        resolve(Scenario.model_validate_json(json.dumps(scenario_data)), deployment_policy())


def test_fargate_supports_application_profile_statically(scenario_data):
    scenario_data.update(
        deployment="eks_fargate_app", boundary="tool_authorization", connectivity="private_services"
    )
    resolve(Scenario.model_validate_json(json.dumps(scenario_data)), deployment_policy())


def test_disconnected_cannot_claim_external_evidence(scenario_data):
    scenario_data.update(deployment="eks_ec2_vm", boundary="container_to_guest")
    with pytest.raises(ValueError, match="external-evidence"):
        resolve(Scenario.model_validate_json(json.dumps(scenario_data)), deployment_policy())


def test_disconnected_private_inference_rejected(scenario_data):
    scenario_data["inference"] = "private_service"
    with pytest.raises(ValidationError, match="connectivity"):
        Scenario.model_validate_json(json.dumps(scenario_data))


def test_replay_cannot_be_scored_as_directed_capability(scenario_data):
    scenario_data["track"] = "directed"
    with pytest.raises(ValidationError, match="validation"):
        Scenario.model_validate_json(json.dumps(scenario_data))


def test_real_deployments_not_allowed_by_development_policy(scenario_data):
    scenario_data["deployment"] = "eks_ec2_vm"
    with pytest.raises(ValueError, match="not allowed"):
        resolve(Scenario.model_validate_json(json.dumps(scenario_data)), development_policy())


def test_manifest_is_immutable_and_digest_survives_roundtrip(scenario):
    manifest = resolve(scenario, development_policy())
    restored = Manifest.model_validate_json(manifest.model_dump_json())
    assert manifest_digest(manifest) == manifest_digest(restored)
    with pytest.raises(ValidationError):
        manifest.scenario.budget.wall_seconds = 9
