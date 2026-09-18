"""Strict, versioned configuration. These models never authorize host commands."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Deployment(StrEnum):
    FAKE = "fake"
    EC2_VM = "eks_ec2_vm"
    FARGATE_APP = "eks_fargate_app"
    HYBRID = "fargate_controller_ec2_vm"


class Boundary(StrEnum):
    SIMULATION = "simulation"
    CONTAINER = "container_to_guest"
    TOOL = "tool_authorization"


class Capability(StrEnum):
    SIMULATION = "simulation"
    VM = "disposable_guest_vm"
    SYNTHETIC_HOST = "synthetic_host"
    APP = "application_target"
    EVIDENCE = "external_evidence"
    DEADLINE = "independent_deadline"


class Budget(StrictModel):
    wall_seconds: int = Field(gt=0)
    memory_mib: int = Field(gt=0)
    vcpus: int = Field(gt=0)
    disk_mib: int = Field(gt=0)
    tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    model_tokens: int = Field(ge=0)
    model_cost_microusd: int = Field(ge=0)
    evidence_bytes: int = Field(gt=0)


class Scenario(StrictModel):
    schema_version: Literal[1]
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    deployment: Deployment
    boundary: Boundary
    track: Literal["directed", "ordinary_task", "validation"]
    inference: Literal["local", "private_service", "replay"]
    connectivity: Literal["private_services", "disconnected"]
    required_capabilities: frozenset[Capability] = frozenset()
    budget: Budget

    @model_validator(mode="after")
    def coherent_modes(self) -> "Scenario":
        if self.inference == "private_service" and self.connectivity == "disconnected":
            raise ValueError("Private inference requires private-service connectivity")
        if self.inference == "replay" and self.track != "validation":
            raise ValueError("Replay is for validation only")
        if self.boundary == Boundary.SIMULATION and self.track != "validation":
            raise ValueError("Simulation is for validation only")
        return self


class SafetyPolicy(StrictModel):
    policy_id: str
    allowed_deployments: frozenset[Deployment]
    ceilings: Budget


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    scenario: Scenario
    policy: SafetyPolicy
    required_capabilities: frozenset[Capability]
    admission: Literal["static_only"] = "static_only"


class State(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    VERIFYING = "verifying"
    RUNNING = "running"
    STOPPING = "stopping"
    CLEANING = "cleaning"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"


class Outcome(StrEnum):
    SIMULATED = "simulation_only"
    INTERRUPTED = "interrupted"
    ERROR = "error"
