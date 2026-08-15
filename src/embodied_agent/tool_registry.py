"""Allowlisted high-level robot tools and pre-MQTT safety execution."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from .safety import SafetyGuard, SafetyStatus
from .schemas import (
    TOOL_PARAM_MODELS,
    CommandMessage,
    ObservationMessage,
    ObservationStatus,
    RobotState,
)


TOOL_DESCRIPTIONS: dict[str, str] = {
    "move_robot": "Move the robot by a relative linear distance at a bounded speed.",
    "turn_robot": "Turn the robot by a relative angle at a bounded angular speed; positive is left.",
    "get_robot_state": "Read the current pose, attitude, obstacle distances, gait, and stop state.",
    "scan_obstacles": "Read front, left, and right obstacle distances before choosing a path.",
    "inspect_semantic_world": "Inspect fixed semantic objects from simulation ground truth without moving the robot.",
    "emergency_stop": "Stop all simulated motion immediately for a stated safety reason.",
}


class ToolCall(BaseModel):
    """Provider-neutral structured tool call; it never contains executable code."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(..., min_length=1, max_length=64)
    arguments: dict[str, Any]

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_arguments(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("tool arguments must be an object")
        return dict(value)

    @classmethod
    def from_function_call(cls, name: str, arguments_json: str) -> "ToolCall":
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise ToolCallRejected("invalid_arguments", f"tool arguments are not valid JSON: {exc.msg}") from exc
        return cls(name=name, arguments=arguments)


class ToolCallRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CommandTransport(Protocol):
    def execute(
        self,
        command: CommandMessage,
        *,
        timeout_s: float | None = None,
    ) -> ObservationMessage: ...


@dataclass(frozen=True)
class ToolExecutionRecord:
    tool_call: ToolCall
    command: CommandMessage | None
    observation: ObservationMessage
    published: bool


class ToolExecutionObserver(Protocol):
    def on_safety_rejected(self, record: ToolExecutionRecord) -> None: ...


class ToolRegistry:
    """Validate Tool Calls, emit Function Calling schemas, and build Commands."""

    def __init__(self, safety_guard: SafetyGuard | None = None) -> None:
        self._safety = safety_guard or SafetyGuard()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(TOOL_DESCRIPTIONS)

    def function_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, description in TOOL_DESCRIPTIONS.items():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": TOOL_PARAM_MODELS[name].model_json_schema(),
                    },
                }
            )
        return schemas

    def build_command(
        self,
        tool_call: ToolCall,
        *,
        task_id: str,
        seq: int,
        deadline_ms: int = 3_000,
        sent_at_ms: int | None = None,
        known_state: RobotState | None = None,
    ) -> CommandMessage:
        if tool_call.name not in TOOL_PARAM_MODELS:
            raise ToolCallRejected("unregistered_tool", f"tool is not allowlisted: {tool_call.name}")
        sent_at = int(time.time() * 1000) if sent_at_ms is None else sent_at_ms
        try:
            command = CommandMessage(
                version=1,
                task_id=task_id,
                seq=seq,
                tool=tool_call.name,
                params=tool_call.arguments,
                deadline_ms=deadline_ms,
                sent_at_ms=sent_at,
            )
        except ValidationError as exc:
            raise ToolCallRejected(
                self._validation_code(tool_call),
                self._compact_validation_error(exc),
            ) from exc

        safety_state = known_state or RobotState(
            front_distance_cm=1_000.0,
            left_distance_cm=1_000.0,
            right_distance_cm=1_000.0,
        )
        decision = self._safety.check(command, safety_state, sent_at)
        if decision.status is not SafetyStatus.ALLOW:
            raise ToolCallRejected(decision.code, decision.message)
        return command

    @staticmethod
    def _validation_code(tool_call: ToolCall) -> str:
        if tool_call.name in {"move_robot", "turn_robot"}:
            return "dangerous_parameter"
        return "schema_validation_error"

    @staticmethod
    def _compact_validation_error(error: ValidationError) -> str:
        messages: list[str] = []
        for item in error.errors(include_url=False):
            location = ".".join(str(part) for part in item["loc"])
            messages.append(f"{location}: {item['msg']}")
        text = "; ".join(messages)
        return text[:256]


class ToolExecutor:
    """Execute a validated Tool Call without publishing rejected commands."""

    def __init__(
        self,
        transport: CommandTransport,
        registry: ToolRegistry | None = None,
        *,
        observer: ToolExecutionObserver | None = None,
    ) -> None:
        self.transport = transport
        self.registry = registry or ToolRegistry()
        self.observer = observer

    def execute(
        self,
        tool_call: ToolCall,
        *,
        task_id: str,
        seq: int,
        deadline_ms: int = 3_000,
        timeout_s: float | None = None,
        known_state: RobotState | None = None,
        now_ms: int | None = None,
    ) -> ToolExecutionRecord:
        received_at = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            command = self.registry.build_command(
                tool_call,
                task_id=task_id,
                seq=seq,
                deadline_ms=deadline_ms,
                sent_at_ms=received_at,
                known_state=known_state,
            )
        except ToolCallRejected as exc:
            state_payload = known_state.model_dump(mode="json") if known_state else {}
            observation = ObservationMessage(
                version=1,
                task_id=task_id,
                seq=seq,
                status=ObservationStatus.REJECTED,
                observation=state_payload,
                error_code=exc.code,
                error_message=exc.message[:256],
                received_at_ms=received_at,
            )
            record = ToolExecutionRecord(tool_call, None, observation, False)
            if self.observer is not None:
                self.observer.on_safety_rejected(record)
            return record

        observation = self.transport.execute(command, timeout_s=timeout_s)
        return ToolExecutionRecord(tool_call, command, observation, True)


__all__ = [
    "CommandTransport",
    "ToolCall",
    "ToolCallRejected",
    "ToolExecutionRecord",
    "ToolExecutionObserver",
    "ToolExecutor",
    "ToolRegistry",
]
