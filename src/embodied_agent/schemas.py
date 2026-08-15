"""Versioned message and tool parameter contracts for the phase 1 MVP."""

from __future__ import annotations

import json
import math
from enum import Enum
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


PROTOCOL_VERSION = 1
MAX_TASK_ID_LENGTH = 64
MAX_DEADLINE_MS = 600_000

ToolName: TypeAlias = Literal[
    "move_robot",
    "turn_robot",
    "get_robot_state",
    "scan_obstacles",
    "inspect_semantic_world",
    "emergency_stop",
]

SemanticQueryKind: TypeAlias = Literal[
    "person",
    "table",
    "chair",
    "bottle",
    "door",
    "goal_zone",
]
SemanticQueryColor: TypeAlias = Literal["red", "blue", "green", "yellow", "gray", "white"]


def _finite_number(value: Any, field_name: str) -> float:
    """Accept JSON numbers but reject booleans, strings, NaN and infinity."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


class ToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveRobotParams(ToolParams):
    distance_m: float = Field(..., ge=-2.0, le=2.0)
    speed_mps: float = Field(..., ge=0.05, le=0.5)
    timeout_ms: StrictInt | None = Field(default=None, ge=100, le=10_000)

    @field_validator("distance_m", "speed_mps", mode="before")
    @classmethod
    def validate_numbers(cls, value: Any, info: Any) -> float:
        return _finite_number(value, info.field_name)


class TurnRobotParams(ToolParams):
    angle_deg: float = Field(..., ge=-180.0, le=180.0)
    angular_speed_dps: float = Field(..., ge=5.0, le=180.0)
    timeout_ms: StrictInt | None = Field(default=None, ge=100, le=10_000)

    @field_validator("angle_deg", "angular_speed_dps", mode="before")
    @classmethod
    def validate_numbers(cls, value: Any, info: Any) -> float:
        return _finite_number(value, info.field_name)


class GetRobotStateParams(ToolParams):
    pass


class ScanObstaclesParams(ToolParams):
    pass


class InspectSemanticWorldParams(ToolParams):
    kind: SemanticQueryKind | None = None
    color: SemanticQueryColor | None = None
    label: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    max_results: StrictInt = Field(default=8, ge=1, le=8)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("label must not be blank")
        return normalized


class EmergencyStopParams(ToolParams):
    reason: StrictStr = Field(..., min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


ToolParamsModel: TypeAlias = (
    MoveRobotParams
    | TurnRobotParams
    | GetRobotStateParams
    | ScanObstaclesParams
    | InspectSemanticWorldParams
    | EmergencyStopParams
)

TOOL_PARAM_MODELS: dict[str, type[ToolParams]] = {
    "move_robot": MoveRobotParams,
    "turn_robot": TurnRobotParams,
    "get_robot_state": GetRobotStateParams,
    "scan_obstacles": ScanObstaclesParams,
    "inspect_semantic_world": InspectSemanticWorldParams,
    "emergency_stop": EmergencyStopParams,
}


def parse_tool_params(tool: str, params: Mapping[str, Any]) -> ToolParams:
    """Validate and normalize the parameter object for one registered tool."""

    model_type = TOOL_PARAM_MODELS.get(tool)
    if model_type is None:
        raise ValueError(f"unregistered tool: {tool}")
    if not isinstance(params, Mapping):
        raise TypeError("params must be an object")
    return model_type.model_validate(dict(params))


class CommandMessage(BaseModel):
    """A command sent to one device.

    ``task_id`` and ``seq`` form the business idempotency key.  The broker's
    delivery semantics are intentionally not represented as exactly-once here.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    task_id: StrictStr = Field(
        ...,
        min_length=1,
        max_length=MAX_TASK_ID_LENGTH,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )
    seq: StrictInt = Field(..., ge=1)
    tool: ToolName
    params: dict[str, Any]
    deadline_ms: StrictInt = Field(..., ge=1, le=MAX_DEADLINE_MS)
    sent_at_ms: StrictInt = Field(..., ge=0)

    @field_validator("params", mode="before")
    @classmethod
    def validate_params_object(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("params must be an object")
        return dict(value)

    @model_validator(mode="after")
    def validate_tool_specific_params(self) -> "CommandMessage":
        validated = parse_tool_params(self.tool, self.params)
        object.__setattr__(self, "params", validated.model_dump(exclude_none=True, mode="json"))
        return self

    def is_expired(self, now_ms: int) -> bool:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise TypeError("now_ms must be an integer")
        return now_ms >= self.sent_at_ms + self.deadline_ms

    def fingerprint(self) -> str:
        """Return a stable representation used to detect conflicting duplicates."""

        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )


class ObservationStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    EMERGENCY_STOP = "emergency_stop"


class ObservationMessage(BaseModel):
    """Structured execution feedback returned by the device."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    task_id: StrictStr = Field(..., min_length=1, max_length=MAX_TASK_ID_LENGTH)
    seq: StrictInt = Field(..., ge=1)
    status: ObservationStatus
    observation: dict[str, Any] = Field(default_factory=dict)
    error_code: StrictStr | None = Field(default=None, max_length=64)
    error_message: StrictStr | None = Field(default=None, max_length=256)
    received_at_ms: StrictInt = Field(..., ge=0)


class RobotState(BaseModel):
    """Deterministic state exposed by the simulator and telemetry contract."""

    model_config = ConfigDict(extra="forbid")

    x_m: float = 0.0
    y_m: float = 0.0
    yaw_deg: float = 0.0
    gait: StrictStr = "stand"
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    front_distance_cm: float = 18.0
    left_distance_cm: float = 120.0
    right_distance_cm: float = 35.0
    emergency_stopped: bool = False
    last_task_id: StrictStr | None = None
    last_seq: StrictInt | None = None

    @field_validator(
        "x_m",
        "y_m",
        "yaw_deg",
        "roll_deg",
        "pitch_deg",
        "front_distance_cm",
        "left_distance_cm",
        "right_distance_cm",
        mode="before",
    )
    @classmethod
    def validate_floats(cls, value: Any, info: Any) -> float:
        return _finite_number(value, info.field_name)

    @field_validator("front_distance_cm", "left_distance_cm", "right_distance_cm")
    @classmethod
    def validate_distances(cls, value: float, info: Any) -> float:
        if value < 0:
            raise ValueError(f"{info.field_name} must be non-negative")
        return value


class TelemetryMessage(BaseModel):
    """Periodic state snapshot; it is separate from command feedback."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    state: RobotState
    reported_at_ms: StrictInt = Field(..., ge=0)


class DeviceEventMessage(BaseModel):
    """Low-rate lifecycle event, such as a Last Will offline notification."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    device_id: StrictStr = Field(..., min_length=1, max_length=64)
    event: StrictStr = Field(..., min_length=1, max_length=64)
    details: dict[str, Any] = Field(default_factory=dict)
    reported_at_ms: StrictInt = Field(..., ge=0)


def schema_bundle() -> dict[str, Any]:
    """Return the contracts that can later be supplied to a Function Calling API."""

    return {
        "command": CommandMessage.model_json_schema(),
        "observation": ObservationMessage.model_json_schema(),
        "telemetry": TelemetryMessage.model_json_schema(),
        "event": DeviceEventMessage.model_json_schema(),
        "tool_params": {
            name: model.model_json_schema() for name, model in TOOL_PARAM_MODELS.items()
        },
    }


__all__ = [
    "CommandMessage",
    "DeviceEventMessage",
    "EmergencyStopParams",
    "GetRobotStateParams",
    "MoveRobotParams",
    "ObservationMessage",
    "ObservationStatus",
    "RobotState",
    "ScanObstaclesParams",
    "TelemetryMessage",
    "TOOL_PARAM_MODELS",
    "ToolName",
    "ToolParams",
    "TurnRobotParams",
    "parse_tool_params",
    "schema_bundle",
]
