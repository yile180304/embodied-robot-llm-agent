"""Versioned state frames used by the continuous simulation runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SimulationRobotState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_m: float = 0.0
    y_m: float = 0.0
    yaw_deg: float = 0.0
    linear_speed_mps: float = 0.0
    angular_speed_dps: float = 0.0
    gait: Literal["stand", "walk", "turn", "stopped"] = "stand"
    gait_phase: float = Field(default=0.0, ge=0.0, lt=1.0)
    emergency_stopped: bool = False


class SimulationSensors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    front_distance_cm: float = Field(default=1_000.0, ge=0.0)
    left_distance_cm: float = Field(default=1_000.0, ge=0.0)
    right_distance_cm: float = Field(default=1_000.0, ge=0.0)


class ActiveSimulationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1, max_length=64)
    seq: int = Field(..., ge=1)
    tool: Literal["move_robot", "turn_robot"]
    progress: float = Field(..., ge=0.0, le=1.0)


class SimulationFrame(BaseModel):
    """One authoritative simulation snapshot for browser rendering."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["simulation.frame"] = "simulation.frame"
    version: Literal[1] = 1
    revision: int = Field(..., ge=0)
    sim_time_ms: int = Field(..., ge=0)
    robot: SimulationRobotState
    sensors: SimulationSensors
    active_command: ActiveSimulationCommand | None = None


__all__ = [
    "ActiveSimulationCommand",
    "SimulationFrame",
    "SimulationRobotState",
    "SimulationSensors",
]
