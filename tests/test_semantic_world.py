from __future__ import annotations

import pytest
from pydantic import ValidationError

from embodied_agent import CommandMessage, ObservationStatus, ToolCall, ToolRegistry
from embodied_agent.simulation import (
    SimulationAdapter,
    SimulationEngine,
    WorldBounds,
    WorldConfig,
    WorldGeometry,
    WorldSemanticObject,
    obstacle_world_config,
    query_semantic_world,
)


def semantic_item(*, blocking: bool, kind: str = "table") -> WorldSemanticObject:
    return WorldSemanticObject(
        id=f"{kind}-test",
        kind=kind,
        label=f"test {kind}",
        color="gray" if kind != "bottle" else "red",
        center_x_m=1.5,
        center_y_m=0.0,
        size_x_m=0.5,
        size_y_m=0.5,
        height_m=0.7,
        interaction_radius_m=0.6,
        blocking=blocking,
    )


def test_query_returns_stable_relative_ground_truth() -> None:
    result = query_semantic_world(
        obstacle_world_config(),
        x_m=0.0,
        y_m=0.0,
        yaw_deg=0.0,
        kind="bottle",
        color="red",
        max_results=4,
    )

    assert result.source == "simulation_ground_truth"
    assert result.query.model_dump(mode="json") == {
        "kind": "bottle",
        "color": "red",
        "label": None,
        "max_results": 4,
    }
    assert len(result.objects) == 1
    bottle = result.objects[0]
    assert bottle.id == "bottle-red-01"
    assert bottle.distance_m == pytest.approx(2.4)
    assert bottle.bearing_deg == pytest.approx(90.0)
    assert bottle.within_interaction_radius is False


def test_query_filters_labels_and_returns_empty_without_guessing() -> None:
    config = obstacle_world_config()
    matching = query_semantic_world(
        config,
        x_m=0.0,
        y_m=2.0,
        yaw_deg=90.0,
        label="RED BOTTLE",
    )
    missing = query_semantic_world(
        config,
        x_m=0.0,
        y_m=0.0,
        yaw_deg=0.0,
        kind="bottle",
        color="blue",
    )

    assert matching.objects[0].within_interaction_radius is True
    assert missing.objects == ()


def test_blocking_semantic_objects_join_geometry_but_non_blocking_objects_do_not() -> None:
    base = dict(
        scene_id="semantic-geometry",
        bounds=WorldBounds(min_x=-4.0, max_x=4.0, min_y=-3.0, max_y=3.0),
    )
    blocking = WorldGeometry(WorldConfig(**base, semantic_objects=(semantic_item(blocking=True),)))
    non_blocking = WorldGeometry(
        WorldConfig(**base, semantic_objects=(semantic_item(blocking=False, kind="bottle"),))
    )

    blocked_delta, blocked_reason = blocking.constrain_move(0.0, 0.0, 0.0, 2.0)
    free_delta, free_reason = non_blocking.constrain_move(0.0, 0.0, 0.0, 2.0)

    assert blocked_delta == pytest.approx(0.65)
    assert blocked_reason == "front_obstacle"
    assert blocking.sensor_distances(0.0, 0.0, 0.0).front_distance_cm == pytest.approx(90.0)
    assert free_delta == pytest.approx(2.0)
    assert free_reason is None


def test_semantic_tool_schema_is_strict() -> None:
    command = ToolRegistry().build_command(
        ToolCall(
            name="inspect_semantic_world",
            arguments={"kind": "bottle", "color": "red", "max_results": 4},
        ),
        task_id="semantic-schema",
        seq=1,
        sent_at_ms=1_000,
    )
    assert command.params == {"kind": "bottle", "color": "red", "max_results": 4}

    with pytest.raises(ValidationError):
        CommandMessage(
            version=1,
            task_id="semantic-invalid",
            seq=1,
            tool="inspect_semantic_world",
            params={"kind": "camera"},
            deadline_ms=3_000,
            sent_at_ms=1_000,
        )


def test_adapter_semantic_query_is_immediate_idempotent_and_does_not_move() -> None:
    engine = SimulationEngine(demo_mode=False, world_config=obstacle_world_config())
    adapter = SimulationAdapter(engine, clock_ms=lambda: 1_000)
    command = CommandMessage(
        version=1,
        task_id="semantic-query",
        seq=1,
        tool="inspect_semantic_world",
        params={"kind": "bottle", "color": "red", "max_results": 4},
        deadline_ms=3_000,
        sent_at_ms=1_000,
    )
    first: list = []
    replay: list = []
    before = engine.snapshot()

    adapter.submit_payload(command.model_dump_json(), first.append)
    adapter.submit_payload(command.model_dump_json(), replay.append)
    after = engine.snapshot()

    assert first[0].status is ObservationStatus.SUCCESS
    assert first[0].observation["source"] == "simulation_ground_truth"
    assert first[0].model_dump(mode="json") == replay[0].model_dump(mode="json")
    assert (after.robot.x_m, after.robot.y_m, after.robot.yaw_deg) == (
        before.robot.x_m,
        before.robot.y_m,
        before.robot.yaw_deg,
    )
    assert after.revision == before.revision
    assert adapter.executed_motion_count == 0
    assert [entry["event"] for entry in adapter.event_log] == ["receive", "completed", "receive", "duplicate"]
