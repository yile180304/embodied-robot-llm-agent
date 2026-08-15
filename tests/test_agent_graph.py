from __future__ import annotations

import threading
from dataclasses import dataclass

from embodied_agent import (
    AgentGraph,
    CommandMessage,
    DeviceSimulator,
    FakePlanner,
    MqttResponseTimeout,
    ObservationMessage,
    ObservationStatus,
    PlannerContext,
    PlannerDecision,
    RobotState,
    ToolExecutor,
)


@dataclass
class DirectSimulatorTransport:
    simulator: DeviceSimulator

    def execute(self, command: CommandMessage, *, timeout_s: float | None = None) -> ObservationMessage:
        return self.simulator.process_command(command, now_ms=command.sent_at_ms + 1)


class LoopingPlanner:
    def plan(self, context: PlannerContext) -> PlannerDecision:
        return PlannerDecision.call("get_robot_state", {}, "继续读取状态。")


class TextPlanner:
    def plan(self, context: PlannerContext):
        return "move forward by running some Python"


class TimeoutTransport:
    def execute(self, command: CommandMessage, *, timeout_s: float | None = None) -> ObservationMessage:
        raise MqttResponseTimeout("simulated MQTT timeout")


class CancelBeforePublishPlanner:
    def __init__(self, cancellation_event: threading.Event) -> None:
        self.cancellation_event = cancellation_event

    def plan(self, context: PlannerContext) -> PlannerDecision:
        self.cancellation_event.set()
        return PlannerDecision.call("move_robot", {"distance_m": 1.0}, "开始移动。")


class EmergencyAfterCancelTransport:
    def __init__(self, cancellation_event: threading.Event) -> None:
        self.cancellation_event = cancellation_event

    def execute(self, command: CommandMessage, *, timeout_s: float | None = None) -> ObservationMessage:
        self.cancellation_event.set()
        return ObservationMessage(
            version=1,
            task_id=command.task_id,
            seq=command.seq,
            status=ObservationStatus.EMERGENCY_STOP,
            observation={"emergency_stopped": True},
            error_code="emergency_cancelled",
            error_message="emergency stop won the race",
            received_at_ms=command.sent_at_ms + 1,
        )


def test_langgraph_completes_single_tool_task():
    simulator = DeviceSimulator(
        initial_state=RobotState(front_distance_cm=100.0),
        obstacle_on_first_move=False,
    )
    graph = AgentGraph(FakePlanner(), ToolExecutor(DirectSimulatorTransport(simulator)))
    result = graph.run("前进 1 米", task_id="agent-simple", max_steps=4)
    assert result.final_status == "success"
    assert [step.tool_call.name for step in result.steps] == ["move_robot"]
    assert simulator.state.x_m == 1.0


def test_langgraph_replans_from_blocked_observation():
    simulator = DeviceSimulator()
    graph = AgentGraph(FakePlanner(), ToolExecutor(DirectSimulatorTransport(simulator)))
    result = graph.run(
        "前进 2 米，如果遇到障碍就从更宽的一侧绕开",
        task_id="agent-blocked",
        max_steps=8,
    )
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
    assert result.steps[2].tool_call.arguments["angle_deg"] == 90.0
    assert result.steps[3].tool_call.arguments["distance_m"] == 0.95
    assert result.steps[4].tool_call.arguments["angle_deg"] == -90.0
    assert result.steps[5].tool_call.arguments["distance_m"] == 2.0
    assert simulator.state.x_m == 2.0
    assert simulator.state.y_m == 0.95


def test_dangerous_goal_is_rejected_without_transport_execution():
    simulator = DeviceSimulator()
    transport = DirectSimulatorTransport(simulator)
    graph = AgentGraph(FakePlanner(), ToolExecutor(transport))
    result = graph.run("以 5 m/s 全速向前冲 10 米", task_id="agent-dangerous")
    assert result.final_status == "rejected"
    assert result.steps[0].published is False
    assert result.steps[0].observation.error_code == "dangerous_parameter"
    assert simulator.executed_command_count == 0


def test_step_limit_prevents_infinite_planner_loop():
    simulator = DeviceSimulator()
    graph = AgentGraph(LoopingPlanner(), ToolExecutor(DirectSimulatorTransport(simulator)))
    result = graph.run("持续读取状态", task_id="agent-limit", max_steps=2)
    assert result.final_status == "step_limit"
    assert len(result.steps) == 2


def test_text_instead_of_tool_call_becomes_planner_error():
    simulator = DeviceSimulator()
    graph = AgentGraph(TextPlanner(), ToolExecutor(DirectSimulatorTransport(simulator)))
    result = graph.run("前进", task_id="agent-text", max_steps=2)
    assert result.final_status == "planner_error"
    assert result.steps == []


def test_transport_timeout_is_an_observable_terminal_state():
    graph = AgentGraph(FakePlanner(), ToolExecutor(TimeoutTransport()))
    result = graph.run("前进 1 米", task_id="agent-timeout", max_steps=2)
    assert result.final_status == "timeout"
    assert result.steps[0].observation.status is ObservationStatus.TIMEOUT
    assert result.steps[0].observation.error_code == "mqtt_response_timeout"


def test_cancelled_before_publish_has_no_tool_or_motion_step():
    cancellation_event = threading.Event()
    graph = AgentGraph(
        CancelBeforePublishPlanner(cancellation_event),
        ToolExecutor(TimeoutTransport()),
    )
    result = graph.run(
        "前进 1 米",
        task_id="agent-cancel-before-publish",
        max_steps=2,
        cancellation_event=cancellation_event,
    )

    assert result.final_status == "cancelled"
    assert result.steps == []


def test_emergency_observation_wins_over_cancellation_signal():
    cancellation_event = threading.Event()
    graph = AgentGraph(
        FakePlanner(),
        ToolExecutor(EmergencyAfterCancelTransport(cancellation_event)),
    )
    result = graph.run(
        "前进 1 米",
        task_id="agent-emergency-race",
        max_steps=2,
        cancellation_event=cancellation_event,
    )

    assert result.final_status == "emergency_stop"
    assert len(result.steps) == 1
    assert result.steps[0].observation.error_code == "emergency_cancelled"
