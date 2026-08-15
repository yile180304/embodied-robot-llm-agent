from __future__ import annotations

from dataclasses import dataclass

import pytest

from embodied_agent import (
    AgentGraph,
    CommandMessage,
    FakePlanner,
    ObservationMessage,
    ObservationStatus,
    ToolExecutor,
)
from embodied_agent.simulation import (
    SimulationAdapter,
    SimulationEngine,
    WorldConfig,
    WorldObstacle,
    obstacle_world_config,
)
from embodied_agent.simulation.runtime import SimulationRuntime


@dataclass
class RuntimeTransport:
    runtime: SimulationRuntime
    adapter: SimulationAdapter

    def execute(
        self,
        command: CommandMessage,
        *,
        timeout_s: float | None = None,
    ) -> ObservationMessage:
        results: list[ObservationMessage] = []
        self.adapter.submit_payload(command.model_dump_json(), results.append)
        now_ms = command.sent_at_ms
        for _ in range(500):
            self.runtime.tick_once(now_ms=now_ms)
            if results:
                return results[0]
            now_ms += self.runtime.tick_ms
        raise AssertionError("simulation command did not complete within the deterministic tick budget")


def run_detour(world_config: WorldConfig):
    engine = SimulationEngine(demo_mode=False, world_config=world_config)
    adapter = SimulationAdapter(engine)
    runtime = SimulationRuntime(engine, adapter=adapter)
    planner = FakePlanner(linear_speed_mps=0.5, turn_speed_dps=180.0)
    result = AgentGraph(planner, ToolExecutor(RuntimeTransport(runtime, adapter))).run(
        "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
        task_id=f"detour-{world_config.scene_id}",
        max_steps=8,
        deadline_ms=15_000,
    )
    return result, engine.snapshot(), adapter


@pytest.mark.parametrize(
    ("wide_side", "expected_angle", "expected_y"),
    [
        ("left", 90.0, 1.2),
        ("right", -90.0, -1.2),
        ("equal", 90.0, 1.2),
    ],
)
def test_fake_planner_completes_one_bounded_detour(
    wide_side: str,
    expected_angle: float,
    expected_y: float,
) -> None:
    result, frame, adapter = run_detour(obstacle_world_config(wide_side))

    assert result.final_status == "success"
    assert [step.tool_call.name for step in result.steps] == [
        "move_robot",
        "scan_obstacles",
        "turn_robot",
        "move_robot",
        "turn_robot",
        "move_robot",
    ]
    assert result.steps[0].observation.status is ObservationStatus.BLOCKED
    assert result.steps[2].tool_call.arguments["angle_deg"] == expected_angle
    assert result.steps[3].tool_call.arguments["distance_m"] == pytest.approx(1.2)
    assert result.steps[4].tool_call.arguments["angle_deg"] == -expected_angle
    assert result.steps[5].tool_call.arguments["distance_m"] == pytest.approx(1.65)
    assert frame.robot.x_m == pytest.approx(2.0)
    assert frame.robot.y_m == pytest.approx(expected_y)
    assert frame.robot.yaw_deg == pytest.approx(0.0)
    assert adapter.executed_motion_count == 5


def test_second_blocked_during_detour_exits_without_looping() -> None:
    data = obstacle_world_config("left").model_dump(mode="json")
    data["obstacles"].append(
        WorldObstacle(
            id="detour-crate",
            center_x_m=1.5,
            center_y_m=1.2,
            size_x_m=0.4,
            size_y_m=0.4,
        ).model_dump(mode="json")
    )

    result, frame, adapter = run_detour(WorldConfig.model_validate(data))

    assert result.final_status == "rejected"
    assert len(result.steps) == 6
    assert result.steps[-1].observation.status is ObservationStatus.BLOCKED
    assert result.steps[-1].observation.error_code == "front_obstacle"
    assert "再次受阻" in result.final_message
    assert frame.robot.x_m < 2.0
    assert frame.robot.y_m == pytest.approx(1.2)
    assert adapter.executed_motion_count == 5
