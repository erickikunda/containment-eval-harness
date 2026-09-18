"""Bounded scripted inference with pure echo/fixture tools. No external model or host tools."""

import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from containment.models import StrictModel
from containment.replay_journal import ReplayJournal


class ReplayScript(StrictModel):
    schema_version: Literal[1]
    prompt: str = Field(max_length=2048)
    responses: tuple[Annotated[str, Field(max_length=8192)], ...] = Field(
        min_length=1, max_length=32
    )
    allowed_tools: frozenset[Literal["echo", "lookup"]]
    fixtures: dict[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        Annotated[str, Field(max_length=2048)],
    ] = Field(default_factory=dict, max_length=16)
    max_response_bytes: int = Field(default=2048, gt=0, le=8192)
    max_output_bytes: int = Field(default=1024, gt=0, le=4096)
    total_output_bytes: int = Field(default=16384, gt=0, le=65536)

    def digest(self):
        data = self.model_dump(mode="json")
        data["allowed_tools"] = sorted(data["allowed_tools"])
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ToolCall(StrictModel):
    kind: Literal["tool"]
    name: str = Field(max_length=64)
    arguments: dict[str, str] = Field(max_length=2)


class Finish(StrictModel):
    kind: Literal["finish"]
    text: str = Field(max_length=4096)


MESSAGE = TypeAdapter(Annotated[ToolCall | Finish, Field(discriminator="kind")])


def execute_tool(call: ToolCall, script: ReplayScript) -> str:
    if call.name not in script.allowed_tools:
        raise ValueError("Tool is not allowed")
    if call.name == "echo" and set(call.arguments) == {"text"}:
        return call.arguments["text"]
    if call.name == "lookup" and set(call.arguments) == {"key"}:
        if call.arguments["key"] in script.fixtures:
            return script.fixtures[call.arguments["key"]]
    raise ValueError("Invalid tool arguments or unknown fixture")


def run_replay(
    trial: UUID,
    script: ReplayScript,
    journal: ReplayJournal,
    checkpoint: Callable[[], None],
    emit: Callable[[dict], None],
) -> str:
    """One pass only. An exception leaves charged reservations for controller recovery."""
    input_text = script.prompt
    emit({"event": "replay_started", "script_digest": script.digest()})
    for raw in script.responses:
        checkpoint()
        input_units = len(input_text.encode("utf-8"))
        action = journal.reserve(
            trial,
            "model",
            tokens=input_units + script.max_response_bytes,
            output_bytes=script.max_response_bytes,
        )
        emit({"event": "model_reserved", "action": action})
        checkpoint()
        # Scripted response only. Assess malformed bytes after reservation.
        size = len(raw.encode("utf-8"))
        journal.settle(trial, action, raw, tokens=input_units + size)
        emit(
            {
                "event": "model_received",
                "action": action,
                "bytes": size,
                "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
        )
        checkpoint()
        message = MESSAGE.validate_json(raw)
        if isinstance(message, Finish):
            if len(message.text.encode()) > script.max_output_bytes:
                raise ValueError("Final output exceeds limit")
            emit({"event": "replay_finished", "bytes": len(message.text.encode())})
            checkpoint()
            journal.complete(trial)
            return message.text
        if message.name not in script.allowed_tools:
            raise ValueError("Tool is not allowed")
        action = journal.reserve(trial, "tool", output_bytes=script.max_output_bytes)
        emit({"event": "tool_reserved", "action": action, "tool": message.name})
        checkpoint()
        result = execute_tool(message, script)
        journal.settle(trial, action, result)
        emit(
            {
                "event": "tool_completed",
                "action": action,
                "bytes": len(result.encode()),
                "sha256": hashlib.sha256(result.encode()).hexdigest(),
            }
        )
        input_text = result
    raise ValueError("Replay ended without a finish response")
