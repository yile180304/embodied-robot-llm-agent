"""Deterministic safety checks shared by the device simulator and future adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .schemas import CommandMessage, RobotState


class SafetyStatus(str, Enum):
    ALLOW = "allow"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class SafetyLimits:
    """Business limits; schema limits are intentionally repeated at this boundary."""

    max_distance_m: float = 2.0
    max_speed_mps: float = 0.5
    max_angle_deg: float = 180.0
    max_angular_speed_dps: float = 180.0
    min_front_distance_cm: float = 25.0
    max_abs_roll_deg: float = 20.0
    max_abs_pitch_deg: float = 20.0


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    status: SafetyStatus
    code: str
    message: str

    @property
    def is_blocked(self) -> bool:
        return self.status is SafetyStatus.BLOCKED

    @property
    def is_rejected(self) -> bool:
        return self.status is SafetyStatus.REJECTED


class SafetyGuard:
    """Apply stateful and environmental constraints after schema validation."""

    MOTION_TOOLS = frozenset({"move_robot", "turn_robot"})

    def __init__(self, limits: SafetyLimits | None = None) -> None:
        self.limits = limits or SafetyLimits()

    def check(self, command: CommandMessage, state: RobotState, now_ms: int) -> SafetyDecision:
        if command.is_expired(now_ms):
            return SafetyDecision(
                allowed=False,
                status=SafetyStatus.TIMEOUT,
                code="deadline_expired",
                message="command deadline has expired",
            )

        tool = command.tool
        if tool not in {
            "move_robot",
            "turn_robot",
            "get_robot_state",
            "scan_obstacles",
            "inspect_semantic_world",
            "emergency_stop",
        }:
            return self._reject("unregistered_tool", "tool is not in the device allowlist")

        if tool == "emergency_stop":
            return SafetyDecision(True, SafetyStatus.ALLOW, "allowed", "emergency stop accepted")

        if tool in self.MOTION_TOOLS:
            if state.emergency_stopped:
                return self._reject("emergency_stopped", "motion is disabled after emergency stop")
            if abs(state.roll_deg) > self.limits.max_abs_roll_deg:
                return self._reject("unsafe_roll", "roll exceeds the motion safety limit")
            if abs(state.pitch_deg) > self.limits.max_abs_pitch_deg:
                return self._reject("unsafe_pitch", "pitch exceeds the motion safety limit")

        if tool == "move_robot":
            distance = self._number(command.params.get("distance_m"))
            speed = self._number(command.params.get("speed_mps"))
            if distance is None or abs(distance) > self.limits.max_distance_m:
                return self._reject("dangerous_parameter", "distance is outside the safety limit")
            if speed is None or speed < 0.05 or speed > self.limits.max_speed_mps:
                return self._reject("dangerous_parameter", "speed is outside the safety limit")
            if distance > 0 and state.front_distance_cm < self.limits.min_front_distance_cm:
                return SafetyDecision(
                    allowed=False,
                    status=SafetyStatus.BLOCKED,
                    code="front_obstacle",
                    message="front obstacle is inside the stopping distance",
                )

        if tool == "turn_robot":
            angle = self._number(command.params.get("angle_deg"))
            angular_speed = self._number(command.params.get("angular_speed_dps"))
            if angle is None or abs(angle) > self.limits.max_angle_deg:
                return self._reject("dangerous_parameter", "angle is outside the safety limit")
            if (
                angular_speed is None
                or angular_speed < 5.0
                or angular_speed > self.limits.max_angular_speed_dps
            ):
                return self._reject("dangerous_parameter", "angular speed is outside the safety limit")

        return SafetyDecision(True, SafetyStatus.ALLOW, "allowed", "command passed safety checks")

    def _reject(self, code: str, message: str) -> SafetyDecision:
        return SafetyDecision(False, SafetyStatus.REJECTED, code, message)

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)


# Keep the longer name available for callers that use the handoff terminology.
SafetyGuardrail = SafetyGuard


__all__ = ["SafetyDecision", "SafetyGuard", "SafetyGuardrail", "SafetyLimits", "SafetyStatus"]
