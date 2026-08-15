"""Fixed-step, deterministic motion engine for preview and bridge modes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .models import (
    ActiveSimulationCommand,
    SimulationFrame,
    SimulationRobotState,
    SimulationSensors,
)
from .world import WorldConfig, WorldGeometry, preview_world_config


DEFAULT_TICK_MS = 50
GAIT_CYCLE_MS = 600


@dataclass(frozen=True)
class SimulationAction:
    """One validated high-level motion action owned by the simulation tick loop."""

    task_id: str
    seq: int
    tool: Literal["move_robot", "turn_robot"]
    amount: float
    rate_per_second: float


@dataclass(frozen=True)
class SimulationActionCompletion:
    """One authoritative success or blocked result produced by the tick owner."""

    action: SimulationAction
    status: Literal["success", "blocked"]
    moved_amount: float
    remaining_amount: float
    reason: Literal["front_obstacle", "path_obstacle", "world_boundary"] | None = None


_DEMO_PROGRAM = (
    SimulationAction("demo-program", 1, "move_robot", 2.0, 0.5),
    SimulationAction("demo-program", 2, "turn_robot", 90.0, 90.0),
    SimulationAction("demo-program", 3, "move_robot", 1.0, 0.5),
)


class SimulationEngine:
    """Advance one quadruped through a deterministic preview or external action."""

    def __init__(
        self,
        *,
        demo_mode: bool = True,
        world_config: WorldConfig | None = None,
    ) -> None:
        self._demo_mode = demo_mode
        self.world_config = world_config or preview_world_config()
        self._geometry = WorldGeometry(self.world_config)
        self._revision = 0
        self._sim_time_ms = 0
        self._robot = self._spawn_robot()
        self._sensors = self._spatial_sensors()
        self._action_index = 0
        self._action_progress = 0.0
        self._external_action: SimulationAction | None = None
        self._external_progress = 0.0
        self._action_completion: SimulationActionCompletion | None = None
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def demo_mode(self) -> bool:
        return self._demo_mode

    @property
    def has_active_action(self) -> bool:
        return self._external_action is not None or (
            self._demo_mode and self._action_index < len(_DEMO_PROGRAM)
        )

    def snapshot(self) -> SimulationFrame:
        return SimulationFrame(
            revision=self._revision,
            sim_time_ms=self._sim_time_ms,
            robot=self._robot.model_copy(deep=True),
            sensors=self._sensors.model_copy(deep=True),
            active_command=self._active_command(),
        )

    def submit_action(self, action: SimulationAction) -> None:
        """Start one external action; only the runtime tick owner calls this."""

        if self._demo_mode:
            raise RuntimeError("external actions require bridge mode")
        if self._external_action is not None:
            raise RuntimeError("simulation action is already active")
        if action.amount == 0.0:
            self._action_completion = SimulationActionCompletion(
                action=action,
                status="success",
                moved_amount=0.0,
                remaining_amount=0.0,
            )
            self._stop_motion()
            self._revision += 1
            return
        self._external_action = action
        self._external_progress = 0.0
        self._robot = self._robot.model_copy(
            update={"gait": "walk" if action.tool == "move_robot" else "turn"}
        )
        self._revision += 1

    def take_completed_action(self) -> SimulationAction | None:
        """Compatibility view for callers that only need the completed action."""

        completion = self.take_action_completion()
        return completion.action if completion is not None else None

    def take_action_completion(self) -> SimulationActionCompletion | None:
        completion = self._action_completion
        self._action_completion = None
        return completion

    def cancel_action(self, *, emergency_stopped: bool = False) -> SimulationAction | None:
        """Cancel the active external action without waiting for its duration."""

        action = self._external_action
        if action is None:
            if emergency_stopped:
                self._stop_motion(emergency_stopped=True)
                self._revision += 1
            return None
        self._external_action = None
        self._external_progress = 0.0
        self._stop_motion(emergency_stopped=emergency_stopped)
        self._revision += 1
        return action

    def advance(self, dt_ms: int = DEFAULT_TICK_MS) -> SimulationFrame:
        if isinstance(dt_ms, bool) or not isinstance(dt_ms, int) or dt_ms <= 0:
            raise ValueError("dt_ms must be a positive integer")
        if self._paused:
            return self.snapshot()

        remaining_ms = dt_ms
        if self._external_action is not None:
            self._advance_external_action(remaining_ms)
        elif self._demo_mode:
            while remaining_ms > 0 and self._action_index < len(_DEMO_PROGRAM):
                consumed_ms = self._advance_demo_action(remaining_ms)
                remaining_ms -= consumed_ms

        self._sim_time_ms += dt_ms
        self._revision += 1
        if self._demo_mode and self._action_index >= len(_DEMO_PROGRAM):
            self._stop_motion()
        return self.snapshot()

    def pause(self) -> SimulationFrame:
        if not self._paused:
            self._paused = True
            self._revision += 1
        return self.snapshot()

    def resume(self) -> SimulationFrame:
        if self._paused:
            self._paused = False
            self._revision += 1
        return self.snapshot()

    def reset(self) -> SimulationFrame:
        self._sim_time_ms = 0
        self._robot = self._spawn_robot()
        self._sensors = self._spatial_sensors()
        self._action_index = 0
        self._action_progress = 0.0
        self._external_action = None
        self._external_progress = 0.0
        self._action_completion = None
        self._paused = False
        self._revision += 1
        return self.snapshot()

    def _advance_demo_action(self, available_ms: int) -> int:
        action = _DEMO_PROGRAM[self._action_index]
        consumed_ms, delta = self._motion_delta(action, self._action_progress, available_ms)
        self._apply_motion(action, delta, consumed_ms)
        self._sensors = self._spatial_sensors()
        self._action_progress = self._clean(self._action_progress + abs(delta))
        if math.isclose(self._action_progress, abs(action.amount), abs_tol=1e-9):
            self._action_index += 1
            self._action_progress = 0.0
        return consumed_ms

    def _advance_external_action(self, available_ms: int) -> int:
        action = self._external_action
        if action is None:
            return available_ms
        consumed_ms, delta = self._motion_delta(action, self._external_progress, available_ms)
        applied_delta = delta
        blocked_reason: Literal["front_obstacle", "path_obstacle", "world_boundary"] | None = None
        if action.tool == "move_robot":
            applied_delta, blocked_reason = self._geometry.constrain_move(
                self._robot.x_m,
                self._robot.y_m,
                self._robot.yaw_deg,
                delta,
            )
        self._apply_motion(action, applied_delta, consumed_ms)
        self._external_progress = self._clean(self._external_progress + abs(applied_delta))
        self._sensors = self._spatial_sensors()
        if blocked_reason is not None:
            moved_amount = math.copysign(self._external_progress, action.amount)
            self._external_action = None
            self._external_progress = 0.0
            self._action_completion = SimulationActionCompletion(
                action=action,
                status="blocked",
                moved_amount=moved_amount,
                remaining_amount=self._clean(action.amount - moved_amount),
                reason=blocked_reason,
            )
            self._stop_motion()
            return consumed_ms
        if math.isclose(self._external_progress, abs(action.amount), abs_tol=1e-9):
            self._external_action = None
            self._external_progress = 0.0
            self._action_completion = SimulationActionCompletion(
                action=action,
                status="success",
                moved_amount=action.amount,
                remaining_amount=0.0,
            )
            self._stop_motion()
        return consumed_ms

    def _motion_delta(
        self,
        action: SimulationAction,
        progress: float,
        available_ms: int,
    ) -> tuple[int, float]:
        remaining_amount = abs(action.amount) - progress
        remaining_action_ms = max(1, math.ceil(remaining_amount / action.rate_per_second * 1_000))
        consumed_ms = min(available_ms, remaining_action_ms)
        delta = min(remaining_amount, action.rate_per_second * consumed_ms / 1_000)
        return consumed_ms, math.copysign(delta, action.amount)

    def _apply_motion(self, action: SimulationAction, delta: float, consumed_ms: int) -> None:
        if action.tool == "move_robot":
            yaw_rad = math.radians(self._robot.yaw_deg)
            self._robot = self._robot.model_copy(
                update={
                    "x_m": self._clean(self._robot.x_m + delta * math.cos(yaw_rad)),
                    "y_m": self._clean(self._robot.y_m + delta * math.sin(yaw_rad)),
                    "linear_speed_mps": math.copysign(action.rate_per_second, delta),
                    "angular_speed_dps": 0.0,
                    "gait": "walk",
                    "gait_phase": self._gait_phase(consumed_ms),
                }
            )
        else:
            self._robot = self._robot.model_copy(
                update={
                    "yaw_deg": self._clean(self._robot.yaw_deg + delta),
                    "linear_speed_mps": 0.0,
                    "angular_speed_dps": math.copysign(action.rate_per_second, delta),
                    "gait": "turn",
                    "gait_phase": self._gait_phase(consumed_ms),
                }
            )

    def _active_command(self) -> ActiveSimulationCommand | None:
        if self._external_action is not None:
            return ActiveSimulationCommand(
                task_id=self._external_action.task_id,
                seq=self._external_action.seq,
                tool=self._external_action.tool,
                progress=min(1.0, self._external_progress / abs(self._external_action.amount)),
            )
        if not self._demo_mode or self._action_index >= len(_DEMO_PROGRAM):
            return None
        action = _DEMO_PROGRAM[self._action_index]
        return ActiveSimulationCommand(
            task_id=action.task_id,
            seq=action.seq,
            tool=action.tool,
            progress=min(1.0, self._action_progress / abs(action.amount)),
        )

    def _gait_phase(self, consumed_ms: int) -> float:
        return ((self._sim_time_ms + consumed_ms) % GAIT_CYCLE_MS) / GAIT_CYCLE_MS

    def _stop_motion(self, *, emergency_stopped: bool | None = None) -> None:
        update: dict[str, float | str | bool] = {
            "linear_speed_mps": 0.0,
            "angular_speed_dps": 0.0,
            "gait": "stopped",
            "gait_phase": 0.0,
        }
        if emergency_stopped is not None:
            update["emergency_stopped"] = emergency_stopped
        self._robot = self._robot.model_copy(update=update)

    def _spawn_robot(self) -> SimulationRobotState:
        spawn = self.world_config.robot_spawn
        return SimulationRobotState(x_m=spawn.x_m, y_m=spawn.y_m, yaw_deg=spawn.yaw_deg)

    def _spatial_sensors(self) -> SimulationSensors:
        return self._geometry.sensor_distances(
            self._robot.x_m,
            self._robot.y_m,
            self._robot.yaw_deg,
        )

    @staticmethod
    def _clean(value: float) -> float:
        return 0.0 if abs(value) < 1e-12 else round(value, 12)


__all__ = [
    "DEFAULT_TICK_MS",
    "SimulationAction",
    "SimulationActionCompletion",
    "SimulationEngine",
]
