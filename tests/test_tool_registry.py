from __future__ import annotations

from dataclasses import dataclass, field

from embodied_agent import (
    CommandMessage,
    ObservationMessage,
    ObservationStatus,
    RobotState,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
)


@dataclass
class RecordingTransport:
    calls: list[CommandMessage] = field(default_factory=list)

    def execute(self, command: CommandMessage, *, timeout_s: float | None = None) -> ObservationMessage:
        self.calls.append(command)
        return ObservationMessage(
            version=1,
            task_id=command.task_id,
            seq=command.seq,
            status=ObservationStatus.SUCCESS,
            observation={"accepted": True},
            received_at_ms=command.sent_at_ms + 1,
        )


def test_function_call_schema_exposes_only_six_high_level_tools():
    schemas = ToolRegistry().function_schemas()
    names = {item["function"]["name"] for item in schemas}
    assert names == {
        "move_robot",
        "turn_robot",
        "get_robot_state",
        "scan_obstacles",
        "inspect_semantic_world",
        "emergency_stop",
    }
    serialized = str(schemas).lower()
    assert "run_code" not in serialized
    assert "pwm" not in serialized


def test_valid_tool_call_builds_command_and_uses_transport_once():
    transport = RecordingTransport()
    executor = ToolExecutor(transport)
    result = executor.execute(
        ToolCall(name="move_robot", arguments={"distance_m": 1.0, "speed_mps": 0.2}),
        task_id="tool-valid",
        seq=1,
        now_ms=1_000,
    )
    assert result.published is True
    assert result.observation.status is ObservationStatus.SUCCESS
    assert len(transport.calls) == 1
    assert transport.calls[0].tool == "move_robot"


def test_dangerous_parameters_are_rejected_before_mqtt_publish():
    transport = RecordingTransport()
    result = ToolExecutor(transport).execute(
        ToolCall(name="move_robot", arguments={"distance_m": 10.0, "speed_mps": 5.0}),
        task_id="tool-dangerous",
        seq=1,
        now_ms=1_000,
    )
    assert result.published is False
    assert result.observation.status is ObservationStatus.REJECTED
    assert result.observation.error_code == "dangerous_parameter"
    assert transport.calls == []


def test_unregistered_tool_and_extra_pwm_field_never_reach_transport():
    transport = RecordingTransport()
    executor = ToolExecutor(transport)
    unknown = executor.execute(
        ToolCall(name="run_python", arguments={"code": "print('unsafe')"}),
        task_id="tool-unknown",
        seq=1,
        now_ms=1_000,
    )
    extra = executor.execute(
        ToolCall(
            name="move_robot",
            arguments={"distance_m": 1.0, "speed_mps": 0.2, "pwm": 1000},
        ),
        task_id="tool-extra",
        seq=1,
        now_ms=1_000,
    )
    assert unknown.observation.error_code == "unregistered_tool"
    assert extra.observation.status is ObservationStatus.REJECTED
    assert transport.calls == []


def test_known_emergency_stop_state_rejects_motion_before_transport():
    transport = RecordingTransport()
    result = ToolExecutor(transport).execute(
        ToolCall(name="move_robot", arguments={"distance_m": 1.0, "speed_mps": 0.2}),
        task_id="tool-stopped",
        seq=1,
        known_state=RobotState(front_distance_cm=100.0, emergency_stopped=True),
        now_ms=1_000,
    )
    assert result.published is False
    assert result.observation.error_code == "emergency_stopped"
    assert transport.calls == []


def test_function_call_arguments_are_parsed_as_json_not_executed():
    call = ToolCall.from_function_call(
        "emergency_stop",
        '{"reason":"operator request"}',
    )
    assert call.arguments == {"reason": "operator request"}
