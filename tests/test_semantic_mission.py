from __future__ import annotations

from dataclasses import dataclass

import pytest

from embodied_agent import AgentGraph, CommandMessage, FakePlanner, ObservationMessage, ToolExecutor
from embodied_agent.simulation import SimulationAdapter, SimulationEngine, WorldConfig, obstacle_world_config
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
        for _ in range(600):
            self.runtime.tick_once(now_ms=now_ms)
            if results:
                return results[0]
            now_ms += self.runtime.tick_ms
        raise AssertionError("semantic command did not complete within the deterministic tick budget")


def run_semantic(world_config: WorldConfig, *, max_steps: int = 16):
    engine = SimulationEngine(demo_mode=False, world_config=world_config)
    adapter = SimulationAdapter(engine)
    runtime = SimulationRuntime(engine, adapter=adapter)
    planner = FakePlanner(linear_speed_mps=0.5, turn_speed_dps=180.0)
    result = AgentGraph(planner, ToolExecutor(RuntimeTransport(runtime, adapter))).run(
        "找到红色瓶子并前往蓝色目标区",
        task_id="semantic-mission",
        max_steps=max_steps,
        deadline_ms=15_000,
    )
    return result, engine.snapshot(), adapter


def test_semantic_mission_confirms_bottle_then_goal_zone() -> None:
    result, frame, adapter = run_semantic(obstacle_world_config())

    assert result.final_status == "success"
    assert [step.tool_call.name for step in result.steps] == [
        "inspect_semantic_world",
        "turn_robot",
        "inspect_semantic_world",
        "move_robot",
        "inspect_semantic_world",
        "inspect_semantic_world",
        "turn_robot",
        "inspect_semantic_world",
        "move_robot",
        "inspect_semantic_world",
        "move_robot",
        "inspect_semantic_world",
    ]
    confirmations = [
        item
        for step in result.steps
        if step.tool_call.name == "inspect_semantic_world"
        for item in step.observation.observation["objects"]
        if item["within_interaction_radius"]
    ]
    assert [(item["kind"], item["color"]) for item in confirmations] == [
        ("bottle", "red"),
        ("goal_zone", "blue"),
    ]
    assert frame.robot.x_m == pytest.approx(2.858, abs=0.02)
    assert frame.robot.y_m == pytest.approx(2.299, abs=0.02)
    assert adapter.executed_motion_count == 5


def test_semantic_mission_missing_target_rejects_without_motion() -> None:
    config = obstacle_world_config()
    without_bottle = config.model_copy(
        update={
            "semantic_objects": tuple(item for item in config.semantic_objects if item.kind != "bottle")
        }
    )

    result, frame, adapter = run_semantic(without_bottle)

    assert result.final_status == "rejected"
    assert len(result.steps) == 1
    assert result.steps[0].observation.observation["objects"] == []
    assert (frame.robot.x_m, frame.robot.y_m, frame.robot.yaw_deg) == (0.0, 0.0, 0.0)
    assert adapter.executed_motion_count == 0


def test_semantic_mission_step_budget_is_bounded() -> None:
    result, _, _ = run_semantic(obstacle_world_config(), max_steps=4)
    assert result.final_status == "step_limit"
    assert len(result.steps) <= 4
