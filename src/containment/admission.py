"""Static compatibility checks; never substitute these for live safety probes."""

import hashlib
import json

from containment.models import (
    Boundary,
    Budget,
    Capability,
    Deployment,
    Manifest,
    SafetyPolicy,
    Scenario,
)

VM_CAPABILITIES = frozenset(
    {
        Capability.VM,
        Capability.SYNTHETIC_HOST,
        Capability.EVIDENCE,
        Capability.DEADLINE,
        Capability.APP,
    }
)
PROFILE_CAPABILITIES = {
    Deployment.FAKE: frozenset({Capability.SIMULATION}),
    Deployment.EC2_VM: VM_CAPABILITIES,
    Deployment.HYBRID: VM_CAPABILITIES,
    Deployment.FARGATE_APP: frozenset({Capability.APP, Capability.EVIDENCE, Capability.DEADLINE}),
}
BOUNDARY_REQUIREMENTS = {
    Boundary.SIMULATION: frozenset({Capability.SIMULATION}),
    Boundary.CONTAINER: frozenset(
        {Capability.VM, Capability.SYNTHETIC_HOST, Capability.EVIDENCE, Capability.DEADLINE}
    ),
    Boundary.TOOL: frozenset({Capability.APP, Capability.EVIDENCE, Capability.DEADLINE}),
}


def development_policy() -> SafetyPolicy:
    """Local-only policy. Real execution needs a separately provisioned operator policy."""
    return SafetyPolicy(
        policy_id="local-simulation-v1",
        allowed_deployments=frozenset({Deployment.FAKE}),
        ceilings=Budget(
            wall_seconds=60,
            memory_mib=1024,
            vcpus=1,
            disk_mib=1024,
            tool_calls=0,
            model_calls=0,
            model_tokens=0,
            model_cost_microusd=0,
            evidence_bytes=1_048_576,
        ),
    )


def resolve(scenario: Scenario, policy: SafetyPolicy) -> Manifest:
    if scenario.deployment not in policy.allowed_deployments:
        raise ValueError(f"Deployment {scenario.deployment} is not allowed by {policy.policy_id}")
    for field in Budget.model_fields:
        if getattr(scenario.budget, field) > getattr(policy.ceilings, field):
            raise ValueError(f"Budget exceeds safety policy: {field}")
    required = BOUNDARY_REQUIREMENTS[scenario.boundary] | scenario.required_capabilities
    missing = required - PROFILE_CAPABILITIES[scenario.deployment]
    if missing:
        raise ValueError(f"Deployment lacks capabilities: {', '.join(sorted(missing))}")
    if scenario.connectivity == "disconnected" and Capability.EVIDENCE in required:
        raise ValueError("Disconnected profile has no independent external-evidence channel")
    if scenario.deployment == Deployment.FAKE and scenario.inference != "replay":
        raise ValueError("Fake deployment supports replay inference only")
    return Manifest(scenario=scenario, policy=policy, required_capabilities=required)


def canonical_manifest(manifest: Manifest) -> str:
    # Pydantic serializes sets to arrays; sort them explicitly for cross-process stability.
    data = manifest.model_dump(mode="json")
    data["required_capabilities"] = sorted(data["required_capabilities"])
    data["scenario"]["required_capabilities"] = sorted(data["scenario"]["required_capabilities"])
    data["policy"]["allowed_deployments"] = sorted(data["policy"]["allowed_deployments"])
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_digest(manifest: Manifest) -> str:
    return hashlib.sha256(canonical_manifest(manifest).encode()).hexdigest()
