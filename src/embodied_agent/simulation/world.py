"""Versioned world configuration and deterministic 2D spatial geometry."""

from __future__ import annotations

import math
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator
from shapely.geometry import LineString, Point, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points

from .models import SimulationSensors


class WorldBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    min_x: float
    max_x: float
    min_y: float
    max_y: float

    @model_validator(mode="after")
    def validate_order(self) -> "WorldBounds":
        if self.min_x >= self.max_x or self.min_y >= self.max_y:
            raise ValueError("world bounds minima must be smaller than maxima")
        return self


class RobotSpawn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    x_m: float = 0.0
    y_m: float = 0.0
    yaw_deg: float = 0.0


class WorldObstacle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    center_x_m: float
    center_y_m: float
    size_x_m: float = Field(..., gt=0.0)
    size_y_m: float = Field(..., gt=0.0)
    height_m: float = Field(default=0.7, gt=0.0)

    def polygon(self) -> BaseGeometry:
        half_x = self.size_x_m / 2.0
        half_y = self.size_y_m / 2.0
        return box(
            self.center_x_m - half_x,
            self.center_y_m - half_y,
            self.center_x_m + half_x,
            self.center_y_m + half_y,
        )


SemanticObjectKind: TypeAlias = Literal[
    "person",
    "table",
    "chair",
    "bottle",
    "door",
    "goal_zone",
]
SemanticObjectColor: TypeAlias = Literal["red", "blue", "green", "yellow", "gray", "white"]


class WorldSemanticObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    kind: SemanticObjectKind
    label: StrictStr = Field(..., min_length=1, max_length=64)
    color: SemanticObjectColor
    center_x_m: float
    center_y_m: float
    size_x_m: float = Field(..., gt=0.0)
    size_y_m: float = Field(..., gt=0.0)
    height_m: float = Field(..., gt=0.0)
    interaction_radius_m: float = Field(..., gt=0.0, le=2.0)
    blocking: StrictBool

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("semantic object label must not be blank")
        return normalized

    def polygon(self) -> BaseGeometry:
        half_x = self.size_x_m / 2.0
        half_y = self.size_y_m / 2.0
        return box(
            self.center_x_m - half_x,
            self.center_y_m - half_y,
            self.center_x_m + half_x,
            self.center_y_m + half_y,
        )


class WorldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal[1] = 1
    scene_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    bounds: WorldBounds
    robot_spawn: RobotSpawn = RobotSpawn()
    robot_footprint_radius_m: float = Field(default=0.35, gt=0.0)
    stop_clearance_m: float = Field(default=0.25, ge=0.0)
    sensor_max_range_m: float = Field(default=10.0, gt=0.0)
    obstacles: tuple[WorldObstacle, ...] = ()
    semantic_objects: tuple[WorldSemanticObject, ...] = ()

    @model_validator(mode="after")
    def validate_geometry(self) -> "WorldConfig":
        margin = self.robot_footprint_radius_m + self.stop_clearance_m
        if self.bounds.max_x - self.bounds.min_x <= 2.0 * margin:
            raise ValueError("world x bounds are too small for the robot footprint")
        if self.bounds.max_y - self.bounds.min_y <= 2.0 * margin:
            raise ValueError("world y bounds are too small for the robot footprint")

        ids = [obstacle.id for obstacle in self.obstacles]
        ids.extend(item.id for item in self.semantic_objects)
        if len(ids) != len(set(ids)):
            raise ValueError("world object ids must be globally unique")

        world_polygon = box(
            self.bounds.min_x,
            self.bounds.min_y,
            self.bounds.max_x,
            self.bounds.max_y,
        )
        safe_spawn = Point(self.robot_spawn.x_m, self.robot_spawn.y_m).buffer(margin)
        if not world_polygon.covers(safe_spawn):
            raise ValueError("robot spawn and clearance must stay inside world bounds")

        for obstacle in self.obstacles:
            polygon = obstacle.polygon()
            if not world_polygon.covers(polygon):
                raise ValueError(f"world obstacle {obstacle.id!r} must stay inside bounds")
            if safe_spawn.intersects(polygon):
                raise ValueError(f"robot spawn conflicts with obstacle {obstacle.id!r}")
        for item in self.semantic_objects:
            polygon = item.polygon()
            if not world_polygon.covers(polygon):
                raise ValueError(f"semantic object {item.id!r} must stay inside bounds")
            if item.blocking and safe_spawn.intersects(polygon):
                raise ValueError(f"robot spawn conflicts with semantic object {item.id!r}")
        return self


class WorldGeometry:
    """Raycast sensors and constrain straight motion against one World Config."""

    _EPSILON_M = 1e-7

    def __init__(self, config: WorldConfig) -> None:
        self.config = config
        radius = config.robot_footprint_radius_m
        motion_margin = radius + config.stop_clearance_m
        self._sensor_bounds = box(
            config.bounds.min_x + radius,
            config.bounds.min_y + radius,
            config.bounds.max_x - radius,
            config.bounds.max_y - radius,
        )
        self._motion_bounds = box(
            config.bounds.min_x + motion_margin,
            config.bounds.min_y + motion_margin,
            config.bounds.max_x - motion_margin,
            config.bounds.max_y - motion_margin,
        )
        solid_objects = (
            *config.obstacles,
            *(item for item in config.semantic_objects if item.blocking),
        )
        self._sensor_obstacles = tuple(
            (obstacle, obstacle.polygon().buffer(radius)) for obstacle in solid_objects
        )
        self._motion_obstacles = tuple(
            (obstacle, obstacle.polygon().buffer(motion_margin))
            for obstacle in solid_objects
        )

    def sensor_distances(self, x_m: float, y_m: float, yaw_deg: float) -> SimulationSensors:
        return SimulationSensors(
            front_distance_cm=self._clean_cm(self._ray_distance_m(x_m, y_m, yaw_deg)),
            left_distance_cm=self._clean_cm(self._ray_distance_m(x_m, y_m, yaw_deg + 90.0)),
            right_distance_cm=self._clean_cm(self._ray_distance_m(x_m, y_m, yaw_deg - 90.0)),
        )

    def constrain_move(
        self,
        x_m: float,
        y_m: float,
        yaw_deg: float,
        distance_m: float,
    ) -> tuple[float, Literal["front_obstacle", "path_obstacle", "world_boundary"] | None]:
        if distance_m == 0.0:
            return 0.0, None

        direction_deg = yaw_deg if distance_m > 0.0 else yaw_deg + 180.0
        desired = abs(distance_m)
        direction = self._direction(direction_deg)
        start = Point(x_m, y_m)
        probe_distance = min(desired, self._EPSILON_M)
        probe = Point(x_m + direction[0] * probe_distance, y_m + direction[1] * probe_distance)
        target = Point(x_m + direction[0] * desired, y_m + direction[1] * desired)
        path = LineString([(probe.x, probe.y), (target.x, target.y)])

        allowed = desired
        reason: Literal["front_obstacle", "path_obstacle", "world_boundary"] | None = None

        if not self._motion_bounds.covers(probe):
            allowed = 0.0
            reason = "world_boundary"
        elif not self._motion_bounds.covers(target):
            boundary_hit = self._intersection_distance(start, path.intersection(self._motion_bounds.boundary))
            if boundary_hit is not None:
                allowed = boundary_hit
                reason = "world_boundary"

        obstacle_reason: Literal["front_obstacle", "path_obstacle"] = (
            "front_obstacle" if distance_m > 0.0 else "path_obstacle"
        )
        for _, forbidden in self._motion_obstacles:
            collision_area = forbidden.buffer(-self._EPSILON_M)
            if collision_area.covers(probe):
                hit_distance = 0.0
            else:
                hit_distance = self._intersection_distance(start, path.intersection(collision_area))
                if hit_distance is not None:
                    hit_distance = max(0.0, hit_distance - self._EPSILON_M)
            if hit_distance is not None and hit_distance < allowed:
                allowed = hit_distance
                reason = obstacle_reason

        if desired <= allowed + self._EPSILON_M:
            return distance_m, None
        signed_allowed = math.copysign(max(0.0, allowed), distance_m)
        return self._clean_m(signed_allowed), reason

    def _ray_distance_m(self, x_m: float, y_m: float, yaw_deg: float) -> float:
        direction = self._direction(yaw_deg)
        max_range = self.config.sensor_max_range_m
        start = Point(x_m, y_m)
        end = Point(x_m + direction[0] * max_range, y_m + direction[1] * max_range)
        ray = LineString([(start.x, start.y), (end.x, end.y)])
        nearest = max_range

        boundary_distance = self._intersection_distance(start, ray.intersection(self._sensor_bounds.boundary))
        if boundary_distance is not None:
            nearest = min(nearest, boundary_distance)

        for _, expanded in self._sensor_obstacles:
            distance = self._intersection_distance(start, ray.intersection(expanded))
            if distance is not None:
                nearest = min(nearest, distance)
        return max(0.0, nearest)

    @staticmethod
    def _intersection_distance(start: Point, geometry: BaseGeometry) -> float | None:
        if geometry.is_empty:
            return None
        nearest = nearest_points(start, geometry)[1]
        return float(start.distance(nearest))

    @staticmethod
    def _direction(yaw_deg: float) -> tuple[float, float]:
        yaw_rad = math.radians(yaw_deg)
        return math.cos(yaw_rad), math.sin(yaw_rad)

    @staticmethod
    def _clean_m(value: float) -> float:
        return 0.0 if abs(value) < 1e-12 else round(value, 12)

    @staticmethod
    def _clean_cm(value_m: float) -> float:
        value = value_m * 100.0
        return 0.0 if abs(value) < 1e-9 else round(value, 6)


def preview_world_config() -> WorldConfig:
    return WorldConfig(
        scene_id="indoor-lab-preview",
        bounds=WorldBounds(min_x=-6.0, max_x=6.0, min_y=-4.0, max_y=4.0),
        obstacles=(
            WorldObstacle(id="preview-a", center_x_m=3.0, center_y_m=-2.0, size_x_m=1.4, size_y_m=0.8),
            WorldObstacle(id="preview-b", center_x_m=-3.0, center_y_m=-2.5, size_x_m=1.2, size_y_m=1.2),
            WorldObstacle(id="preview-c", center_x_m=4.0, center_y_m=2.5, size_x_m=1.0, size_y_m=1.6),
        ),
        semantic_objects=_semantic_objects(),
    )


def obstacle_world_config(
    wide_side: Literal["left", "right", "equal"] = "left",
) -> WorldConfig:
    side_obstacles: tuple[WorldObstacle, ...]
    if wide_side == "left":
        side_obstacles = (
            WorldObstacle(id="right-crate", center_x_m=0.35, center_y_m=-1.0, size_x_m=0.8, size_y_m=0.5),
        )
    elif wide_side == "right":
        side_obstacles = (
            WorldObstacle(id="left-crate", center_x_m=0.35, center_y_m=1.0, size_x_m=0.8, size_y_m=0.5),
        )
    else:
        side_obstacles = ()
    return WorldConfig(
        scene_id=f"indoor-lab-obstacle-{wide_side}",
        bounds=WorldBounds(min_x=-6.0, max_x=6.0, min_y=-4.0, max_y=4.0),
        obstacles=(
            WorldObstacle(id="front-crate", center_x_m=1.2, center_y_m=0.0, size_x_m=0.5, size_y_m=0.8),
            *side_obstacles,
        ),
        semantic_objects=_semantic_objects(),
    )


def _semantic_objects() -> tuple[WorldSemanticObject, ...]:
    return (
        WorldSemanticObject(
            id="person-green-01",
            kind="person",
            label="green lab operator",
            color="green",
            center_x_m=-2.8,
            center_y_m=2.4,
            size_x_m=0.45,
            size_y_m=0.45,
            height_m=1.7,
            interaction_radius_m=0.9,
            blocking=True,
        ),
        WorldSemanticObject(
            id="table-gray-01",
            kind="table",
            label="gray work table",
            color="gray",
            center_x_m=-3.2,
            center_y_m=0.4,
            size_x_m=1.4,
            size_y_m=0.8,
            height_m=0.75,
            interaction_radius_m=0.9,
            blocking=True,
        ),
        WorldSemanticObject(
            id="chair-yellow-01",
            kind="chair",
            label="yellow chair",
            color="yellow",
            center_x_m=-2.5,
            center_y_m=-1.4,
            size_x_m=0.55,
            size_y_m=0.55,
            height_m=0.9,
            interaction_radius_m=0.75,
            blocking=True,
        ),
        WorldSemanticObject(
            id="door-white-01",
            kind="door",
            label="white lab door",
            color="white",
            center_x_m=4.7,
            center_y_m=-2.3,
            size_x_m=0.18,
            size_y_m=1.1,
            height_m=2.1,
            interaction_radius_m=1.0,
            blocking=True,
        ),
        WorldSemanticObject(
            id="bottle-red-01",
            kind="bottle",
            label="red bottle",
            color="red",
            center_x_m=0.0,
            center_y_m=2.4,
            size_x_m=0.12,
            size_y_m=0.12,
            height_m=0.28,
            interaction_radius_m=0.55,
            blocking=False,
        ),
        WorldSemanticObject(
            id="goal-zone-blue-01",
            kind="goal_zone",
            label="blue goal zone",
            color="blue",
            center_x_m=3.5,
            center_y_m=2.4,
            size_x_m=1.0,
            size_y_m=1.0,
            height_m=0.03,
            interaction_radius_m=0.65,
            blocking=False,
        ),
    )


__all__ = [
    "RobotSpawn",
    "WorldBounds",
    "WorldConfig",
    "WorldGeometry",
    "WorldObstacle",
    "SemanticObjectColor",
    "SemanticObjectKind",
    "WorldSemanticObject",
    "obstacle_world_config",
    "preview_world_config",
]
