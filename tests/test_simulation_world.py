from __future__ import annotations

import pytest
from pydantic import ValidationError

from embodied_agent.simulation.world import (
    WorldSemanticObject,
    WorldConfig,
    WorldGeometry,
    obstacle_world_config,
    preview_world_config,
)


def test_obstacle_world_rays_and_safe_delta_are_deterministic() -> None:
    geometry = WorldGeometry(obstacle_world_config("left"))

    first = geometry.sensor_distances(0.0, 0.0, 0.0)
    second = geometry.sensor_distances(0.0, 0.0, 0.0)
    allowed, reason = geometry.constrain_move(0.0, 0.0, 0.0, 2.0)

    assert first == second
    assert first.front_distance_cm == pytest.approx(60.0)
    assert first.left_distance_cm == pytest.approx(365.0)
    assert first.right_distance_cm == pytest.approx(40.0)
    assert allowed == pytest.approx(0.35)
    assert reason == "front_obstacle"


def test_clearance_boundary_allows_tangent_detour_but_not_inward_motion() -> None:
    geometry = WorldGeometry(obstacle_world_config("left"))

    tangent_delta, tangent_reason = geometry.constrain_move(0.35, 0.0, 90.0, 1.2)
    inward_delta, inward_reason = geometry.constrain_move(0.35, 0.0, 0.0, 0.5)

    assert tangent_delta == pytest.approx(1.2)
    assert tangent_reason is None
    assert inward_delta == pytest.approx(0.0)
    assert inward_reason == "front_obstacle"


def test_wide_side_worlds_mirror_left_and_right_clearance() -> None:
    left = WorldGeometry(obstacle_world_config("left")).sensor_distances(0.0, 0.0, 0.0)
    right = WorldGeometry(obstacle_world_config("right")).sensor_distances(0.0, 0.0, 0.0)
    equal = WorldGeometry(obstacle_world_config("equal")).sensor_distances(0.0, 0.0, 0.0)

    assert left.left_distance_cm > left.right_distance_cm
    assert right.right_distance_cm > right.left_distance_cm
    assert equal.left_distance_cm == pytest.approx(equal.right_distance_cm)


def test_preview_world_does_not_block_existing_demo_path() -> None:
    geometry = WorldGeometry(preview_world_config())

    first, first_reason = geometry.constrain_move(0.0, 0.0, 0.0, 2.0)
    second, second_reason = geometry.constrain_move(2.0, 0.0, 90.0, 1.0)

    assert first == 2.0
    assert first_reason is None
    assert second == 1.0
    assert second_reason is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(version=2),
        lambda data: data["obstacles"].append(dict(data["obstacles"][0])),
        lambda data: data["obstacles"][0].update(size_x_m=0.0),
        lambda data: data["obstacles"][0].update(center_x_m=7.0),
        lambda data: data["robot_spawn"].update(x_m=1.2),
    ],
)
def test_invalid_world_config_is_rejected(mutate) -> None:
    data = obstacle_world_config().model_dump(mode="json")
    mutate(data)

    with pytest.raises(ValidationError):
        WorldConfig.model_validate(data)


def test_reverse_motion_uses_path_obstacle_reason_and_bounds_are_blocking() -> None:
    config = obstacle_world_config()
    geometry = WorldGeometry(config)

    reverse, reverse_reason = geometry.constrain_move(1.8, 0.0, 0.0, -2.0)
    boundary, boundary_reason = geometry.constrain_move(5.3, 2.0, 0.0, 1.0)

    assert reverse == pytest.approx(0.0)
    assert reverse_reason == "path_obstacle"
    assert boundary == pytest.approx(0.1)
    assert boundary_reason == "world_boundary"


def test_semantic_objects_round_trip_and_global_ids_are_strict() -> None:
    config = obstacle_world_config()
    assert {item.kind for item in config.semantic_objects} == {
        "person", "table", "chair", "door", "bottle", "goal_zone",
    }
    payload = config.model_dump(mode="json")
    assert len(payload["semantic_objects"]) == 6
    assert WorldSemanticObject.model_validate(payload["semantic_objects"][0]).id == "person-green-01"

    duplicate = config.model_copy(
        update={
            "semantic_objects": (
                *config.semantic_objects,
                config.semantic_objects[0].model_copy(update={"id": "front-crate"}),
            )
        }
    )
    with pytest.raises(ValidationError, match="globally unique"):
        WorldConfig.model_validate(duplicate.model_dump(mode="json"))


def test_non_blocking_semantic_objects_do_not_block_spawn() -> None:
    config = obstacle_world_config()
    bottle = config.semantic_objects[4].model_copy(update={"center_x_m": 0.0, "center_y_m": 0.0})
    rebuilt = config.model_copy(update={"semantic_objects": (*config.semantic_objects[:4], bottle, config.semantic_objects[5])})
    validated = WorldConfig.model_validate(rebuilt.model_dump(mode="json"))
    assert validated.semantic_objects[4].blocking is False
