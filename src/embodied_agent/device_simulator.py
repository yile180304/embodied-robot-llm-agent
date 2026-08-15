"""A deterministic, in-process device simulator for phase 1 tests.

The simulator intentionally has no MQTT, subprocess, shell, or model execution
surface.  A future transport adapter can call ``process_payload`` and publish
the returned ``ObservationMessage`` without changing the safety or idempotency
rules.
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from .safety import SafetyGuard, SafetyStatus
from .schemas import (
    CommandMessage,
    ObservationMessage,
    ObservationStatus,
    RobotState,
    TelemetryMessage,
)


Clock = Callable[[], int]
CacheKey = tuple[str, int]


@dataclass(frozen=True)
class _CachedResult:
    fingerprint: str
    observation: ObservationMessage


class DeviceSimulator:
    """Simulate one ``dog01``-style device with deterministic state transitions."""

    def __init__(
        self,
        device_id: str = "dog01",
        *,
        clock_ms: Clock | None = None,
        initial_state: RobotState | None = None,
        safety_guard: SafetyGuard | None = None,
        obstacle_on_first_move: bool = True,
        cache_size: int = 256,
    ) -> None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.device_id = device_id.strip()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._state = (initial_state or RobotState()).model_copy(deep=True)
        self._safety = safety_guard or SafetyGuard()
        self._cache_size = cache_size
        self._cache: OrderedDict[CacheKey, _CachedResult] = OrderedDict()
        self._latest_seq: dict[str, int] = {}
        self._obstacle_on_first_move = obstacle_on_first_move
        self._first_move_blocked = False
        self.commands_seen = 0
        self.executed_command_count = 0
        self.event_log: list[dict[str, Any]] = []

    @property
    def state(self) -> RobotState:
        return self._state.model_copy(deep=True)

    @property
    def cached_keys(self) -> tuple[CacheKey, ...]:
        return tuple(self._cache.keys())

    def process_payload(self, payload: Mapping[str, Any] | str | bytes, now_ms: int | None = None) -> ObservationMessage:
        """Parse an untrusted payload and always return structured feedback."""

        received_at = self._now(now_ms)
        try:
            raw: Any
            if isinstance(payload, (str, bytes, bytearray)):
                raw = json.loads(payload)
            else:
                raw = payload
            command = CommandMessage.model_validate(raw)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._schema_rejection(payload, exc, received_at)
        return self.process_command(command, received_at)

    def process_command(self, command: CommandMessage, now_ms: int | None = None) -> ObservationMessage:
        """Process one validated command with business-level idempotency."""

        received_at = self._now(now_ms)
        key = (command.task_id, command.seq)
        fingerprint = command.fingerprint()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            if cached.fingerprint == fingerprint:
                self._log("duplicate", command, cached.observation)
                return cached.observation.model_copy(deep=True)
            conflict = self._feedback(
                command,
                ObservationStatus.REJECTED,
                received_at,
                code="duplicate_conflict",
                message="same task_id and seq carried different command data",
            )
            self._log("duplicate_conflict", command, conflict)
            return conflict

        latest = self._latest_seq.get(command.task_id)
        if latest is not None and command.seq < latest:
            stale = self._feedback(
                command,
                ObservationStatus.REJECTED,
                received_at,
                code="stale_sequence",
                message=f"sequence {command.seq} is older than latest sequence {latest}",
            )
            self._remember(command, stale, fingerprint, update_latest=False)
            self._log("stale_sequence", command, stale)
            return stale

        self.commands_seen += 1
        self._state = self._state.model_copy(
            update={"last_task_id": command.task_id, "last_seq": command.seq}
        )
        decision = self._safety.check(command, self._state, received_at)
        if decision.status is SafetyStatus.TIMEOUT:
            result = self._feedback(
                command,
                ObservationStatus.TIMEOUT,
                received_at,
                code=decision.code,
                message=decision.message,
            )
            self._remember(command, result, fingerprint)
            self._log("timeout", command, result)
            return result
        if decision.status is SafetyStatus.BLOCKED:
            extra: dict[str, Any] = {"reason": decision.code}
            if command.tool == "move_robot" and float(command.params.get("distance_m", 0.0)) > 0:
                # The default scene reports the first obstacle in the safety layer.
                # Mark it consumed so a later turn can expose the alternate route.
                self._first_move_blocked = True
                distance = float(command.params["distance_m"])
                extra.update(
                    {
                        "requested_distance_m": distance,
                        "moved_distance_m": 0.0,
                        "remaining_distance_m": distance,
                    }
                )
            result = self._feedback(
                command,
                ObservationStatus.BLOCKED,
                received_at,
                code=decision.code,
                message=decision.message,
                extra=extra,
            )
            self._remember(command, result, fingerprint)
            self._log("blocked", command, result)
            return result
        if not decision.allowed:
            result = self._feedback(
                command,
                ObservationStatus.REJECTED,
                received_at,
                code=decision.code,
                message=decision.message,
            )
            self._remember(command, result, fingerprint)
            self._log("rejected", command, result)
            return result

        self.executed_command_count += 1
        result = self._execute(command, received_at)
        self._remember(command, result, fingerprint)
        self._log("executed", command, result)
        return result

    def telemetry(self, reported_at_ms: int | None = None) -> TelemetryMessage:
        return TelemetryMessage(
            version=1,
            device_id=self.device_id,
            state=self._state.model_copy(deep=True),
            reported_at_ms=self._now(reported_at_ms),
        )

    def set_obstacles(
        self,
        *,
        front_distance_cm: float,
        left_distance_cm: float,
        right_distance_cm: float,
    ) -> None:
        values = (front_distance_cm, left_distance_cm, right_distance_cm)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("obstacle distances must be numbers")
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise ValueError("obstacle distances must be finite and non-negative")
        self._state = self._state.model_copy(
            update={
                "front_distance_cm": float(front_distance_cm),
                "left_distance_cm": float(left_distance_cm),
                "right_distance_cm": float(right_distance_cm),
            }
        )

    def set_pose(self, *, roll_deg: float | None = None, pitch_deg: float | None = None) -> None:
        updates: dict[str, float] = {}
        if roll_deg is not None:
            updates["roll_deg"] = self._finite_float(roll_deg, "roll_deg")
        if pitch_deg is not None:
            updates["pitch_deg"] = self._finite_float(pitch_deg, "pitch_deg")
        if updates:
            self._state = self._state.model_copy(update=updates)

    def clear_emergency_stop(self) -> None:
        """Test/operator helper; there is deliberately no model-facing resume tool."""

        self._state = self._state.model_copy(update={"emergency_stopped": False, "gait": "stand"})

    def dump_event_log(self, path: str | Path) -> None:
        destination = Path(path)
        destination.write_text(
            json.dumps(self.event_log, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _execute(self, command: CommandMessage, received_at: int) -> ObservationMessage:
        if command.tool == "move_robot":
            return self._move(command, received_at)
        if command.tool == "turn_robot":
            return self._turn(command, received_at)
        if command.tool == "get_robot_state":
            return self._feedback(
                command,
                ObservationStatus.SUCCESS,
                received_at,
                extra={"state": self._state_snapshot()},
            )
        if command.tool == "scan_obstacles":
            return self._feedback(
                command,
                ObservationStatus.SUCCESS,
                received_at,
                extra={
                    "front_distance_cm": self._state.front_distance_cm,
                    "left_distance_cm": self._state.left_distance_cm,
                    "right_distance_cm": self._state.right_distance_cm,
                },
            )
        if command.tool == "inspect_semantic_world":
            return self._feedback(
                command,
                ObservationStatus.REJECTED,
                received_at,
                code="semantic_unavailable",
                message="semantic ground truth is only available in continuous simulation bridge mode",
            )
        if command.tool == "emergency_stop":
            reason = str(command.params["reason"])
            self._state = self._state.model_copy(update={"emergency_stopped": True, "gait": "stopped"})
            return self._feedback(
                command,
                ObservationStatus.EMERGENCY_STOP,
                received_at,
                extra={"emergency_stopped": True, "reason": reason},
            )
        # CommandMessage's Literal and parameter validation make this unreachable.
        return self._feedback(
            command,
            ObservationStatus.REJECTED,
            received_at,
            code="unregistered_tool",
            message="tool is not in the device allowlist",
        )

    def _move(self, command: CommandMessage, received_at: int) -> ObservationMessage:
        distance = float(command.params["distance_m"])
        if distance > 0 and self._obstacle_on_first_move and not self._first_move_blocked:
            self._first_move_blocked = True
            return self._feedback(
                command,
                ObservationStatus.BLOCKED,
                received_at,
                code="front_obstacle",
                message="deterministic simulator obstacle on first forward move",
                extra={
                    "reason": "front_obstacle",
                    "requested_distance_m": distance,
                    "moved_distance_m": 0.0,
                    "remaining_distance_m": distance,
                },
            )

        yaw = math.radians(self._state.yaw_deg)
        updates = {
            "x_m": self._clean_zero(self._state.x_m + distance * math.cos(yaw)),
            "y_m": self._clean_zero(self._state.y_m + distance * math.sin(yaw)),
        }
        self._state = self._state.model_copy(update=updates)
        return self._feedback(
            command,
            ObservationStatus.SUCCESS,
            received_at,
            extra={"moved_distance_m": distance, "state": self._state_snapshot()},
        )

    def _turn(self, command: CommandMessage, received_at: int) -> ObservationMessage:
        angle = float(command.params["angle_deg"])
        new_yaw = ((self._state.yaw_deg + angle + 180.0) % 360.0) - 180.0
        if angle > 0:
            new_front = self._state.left_distance_cm
        elif angle < 0:
            new_front = self._state.right_distance_cm
        else:
            new_front = self._state.front_distance_cm
        self._state = self._state.model_copy(
            update={"yaw_deg": new_yaw, "front_distance_cm": new_front}
        )
        return self._feedback(
            command,
            ObservationStatus.SUCCESS,
            received_at,
            extra={"turned_angle_deg": angle, "state": self._state_snapshot()},
        )

    def _feedback(
        self,
        command: CommandMessage,
        status: ObservationStatus,
        received_at: int,
        *,
        code: str | None = None,
        message: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ObservationMessage:
        details = self._state_snapshot()
        if extra:
            details.update(dict(extra))
        return ObservationMessage(
            version=1,
            task_id=command.task_id,
            seq=command.seq,
            status=status,
            observation=details,
            error_code=code,
            error_message=message,
            received_at_ms=received_at,
        )

    def _schema_rejection(self, payload: Any, error: Exception, received_at: int) -> ObservationMessage:
        task_id, seq = self._fallback_correlation(payload)
        text = str(error).replace("\n", " ")
        if len(text) > 256:
            text = text[:253] + "..."
        code = "dangerous_parameter" if self._looks_like_dangerous_payload(payload, text) else "schema_validation_error"
        return ObservationMessage(
            version=1,
            task_id=task_id,
            seq=seq,
            status=ObservationStatus.REJECTED,
            observation=self._state_snapshot(),
            error_code=code,
            error_message=text,
            received_at_ms=received_at,
        )

    @staticmethod
    def _looks_like_dangerous_payload(payload: Any, error_text: str) -> bool:
        """Classify range/type violations without relabeling missing fields."""

        if not isinstance(payload, Mapping):
            return False
        tool = payload.get("tool")
        params = payload.get("params")
        if not isinstance(params, Mapping):
            return False
        if tool == "move_robot":
            checks = {
                "distance_m": (-2.0, 2.0),
                "speed_mps": (0.05, 0.5),
            }
        elif tool == "turn_robot":
            checks = {
                "angle_deg": (-180.0, 180.0),
                "angular_speed_dps": (5.0, 180.0),
            }
        else:
            return False
        for field, (lower, upper) in checks.items():
            if field not in params:
                continue
            value = params[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return True
            if not math.isfinite(float(value)) or not lower <= float(value) <= upper:
                return True
        return any(marker in error_text.lower() for marker in ("finite", "must be a number"))

    def _remember(
        self,
        command: CommandMessage,
        observation: ObservationMessage,
        fingerprint: str,
        *,
        update_latest: bool = True,
    ) -> None:
        if update_latest:
            latest = self._latest_seq.get(command.task_id)
            if latest is None or command.seq > latest:
                self._latest_seq[command.task_id] = command.seq
            self._state = self._state.model_copy(
                update={"last_task_id": command.task_id, "last_seq": command.seq}
            )
        key = (command.task_id, command.seq)
        self._cache[key] = _CachedResult(fingerprint, observation.model_copy(deep=True))
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _state_snapshot(self) -> dict[str, Any]:
        return self._state.model_dump(mode="json")

    def _log(self, event: str, command: CommandMessage, observation: ObservationMessage) -> None:
        self.event_log.append(
            {
                "event": event,
                "task_id": command.task_id,
                "seq": command.seq,
                "tool": command.tool,
                "status": observation.status.value,
                "error_code": observation.error_code,
                "received_at_ms": observation.received_at_ms,
            }
        )

    def _now(self, now_ms: int | None) -> int:
        value = self._clock_ms() if now_ms is None else now_ms
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("now_ms must be a non-negative integer")
        return value

    @staticmethod
    def _finite_float(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{name} must be finite")
        return result

    @staticmethod
    def _clean_zero(value: float, epsilon: float = 1e-12) -> float:
        return 0.0 if abs(value) < epsilon else value

    @staticmethod
    def _fallback_correlation(payload: Any) -> tuple[str, int]:
        if isinstance(payload, Mapping):
            task_id = payload.get("task_id")
            seq = payload.get("seq")
            if isinstance(task_id, str) and task_id.strip():
                safe_task_id = task_id[:64]
            else:
                safe_task_id = "invalid-command"
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
                safe_seq = seq
            else:
                safe_seq = 1
            return safe_task_id, safe_seq
        return "invalid-command", 1


__all__ = ["DeviceSimulator"]
